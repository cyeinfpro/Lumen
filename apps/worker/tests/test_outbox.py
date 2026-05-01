from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen_core.constants import CompletionStatus, GenerationStatus, MessageStatus
from lumen_core.models import Completion, Generation, Message, OutboxEvent
from app.tasks import outbox


class FakeRedis:
    def __init__(self, *, fail_enqueue: bool = False) -> None:
        self.fail_enqueue = fail_enqueue
        self.enqueued: list[tuple[str, str]] = []
        self.keys: dict[str, str] = {}
        self.deleted: list[str] = []

    async def set(self, key: str, value: str, **_kwargs):
        self.keys[key] = value
        return True

    async def delete(self, key: str):
        self.deleted.append(key)
        self.keys.pop(key, None)
        return 1

    async def get(self, key: str):
        return self.keys.get(key)

    async def enqueue_job(self, job_name: str, task_id: str):
        if self.fail_enqueue:
            raise RuntimeError("redis unavailable")
        self.enqueued.append((job_name, task_id))
        return SimpleNamespace(job_id=f"job:{task_id}")

    async def hincrby(self, _key: str, _field: str, _amount: int):
        return 1

    async def expire(self, _key: str, _ttl: int):
        return True

    async def hdel(self, _key: str, _field: str):
        return 1

    async def lpush(self, _key: str, _payload: str):
        return 1

    async def ltrim(self, *_args):
        return 1


class FakeScalarResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeUpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class FakeSession:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self

    async def execute(self, statement):
        if statement.is_select:
            return FakeScalarResult([ev for ev in self.events if ev.published_at is None])
        rowcount = 0
        for ev in self.events:
            if ev.published_at is None:
                ev.published_at = datetime.now(timezone.utc)
                rowcount = 1
                break
        return FakeUpdateResult(rowcount)

    async def commit(self) -> None:
        return None


class FakeReconSession:
    def __init__(
        self,
        generations: list[Generation],
        completions: list[Completion],
        messages: dict[str, Message] | None = None,
    ) -> None:
        self.generations = generations
        self.completions = completions
        self.messages = messages or {}
        self.select_skip_locked: list[bool] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        text = str(statement)
        arg = getattr(statement, "_for_update_arg", None)
        if statement.is_select:
            self.select_skip_locked.append(arg is not None and arg.skip_locked is True)
        if "FROM generations" in text:
            return FakeScalarResult(self.generations)
        if "FROM completions" in text:
            return FakeScalarResult(self.completions)
        return FakeScalarResult([])

    async def get(self, model, object_id: str):
        if model is Message:
            return self.messages.get(object_id)
        return None

    async def commit(self) -> None:
        return None


def _patch_session_local(monkeypatch: pytest.MonkeyPatch, events: list[OutboxEvent]) -> None:
    @asynccontextmanager
    async def session_local():
        yield FakeSession(events)

    monkeypatch.setattr(outbox, "SessionLocal", session_local)


def _patch_recon_session_local(
    monkeypatch: pytest.MonkeyPatch,
    generations: list[Generation],
    completions: list[Completion],
    messages: dict[str, Message] | None = None,
) -> FakeReconSession:
    fake_session = FakeReconSession(generations, completions, messages)

    @asynccontextmanager
    async def session_local():
        yield fake_session

    monkeypatch.setattr(outbox, "SessionLocal", session_local)
    return fake_session


def _patch_publish_event(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    published: list[dict] = []

    async def publish_event(redis, user_id, channel, event_name, data):
        published.append(
            {
                "user_id": user_id,
                "channel": channel,
                "event_name": event_name,
                "data": data,
            }
        )

    monkeypatch.setattr(outbox, "publish_event", publish_event)
    return published


def _event(
    *, event_id: str = "event-1", task_id: str = "task-1", kind: str = "generation"
) -> OutboxEvent:
    return OutboxEvent(
        id=event_id,
        kind=kind,
        payload={"task_id": task_id},
        created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )


@pytest.mark.asyncio
async def test_publish_outbox_marks_published_only_after_enqueue_success(monkeypatch):
    events = [_event(task_id="gen-1")]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert redis.enqueued == [("run_generation", "gen-1")]
    assert events[0].published_at is not None


@pytest.mark.asyncio
async def test_publish_outbox_processes_batch_in_one_pass(monkeypatch):
    events = [
        _event(event_id="event-1", task_id="gen-1", kind="generation"),
        _event(event_id="event-2", task_id="comp-1", kind="completion"),
    ]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 2
    assert redis.enqueued == [
        ("run_generation", "gen-1"),
        ("run_completion", "comp-1"),
    ]
    assert all(ev.published_at is not None for ev in events)


@pytest.mark.asyncio
async def test_publish_outbox_keeps_event_retryable_when_enqueue_fails(monkeypatch):
    events = [_event(task_id="gen-1")]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis(fail_enqueue=True)

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 0
    assert redis.enqueued == []
    assert events[0].published_at is None
    assert not any(
        key.startswith(outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX)
        for key in redis.keys
    )


@pytest.mark.asyncio
async def test_publish_outbox_marks_deduped_event_without_second_enqueue(monkeypatch):
    class _DedupeRedis(FakeRedis):
        async def set(self, key: str, value: str, **kwargs):
            if key.startswith(outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX):
                return False
            return await super().set(key, value, **kwargs)

    events = [_event(task_id="gen-1")]
    _patch_session_local(monkeypatch, events)
    redis = _DedupeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert redis.enqueued == []
    assert events[0].published_at is not None


@pytest.mark.asyncio
async def test_publish_outbox_malformed_payload_goes_to_dlq_and_logs(
    monkeypatch, caplog
):
    event = _event(task_id="gen-1")
    event.payload = ["bad"]
    events = [event]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 0
    assert redis.enqueued == []
    assert events[0].published_at is not None
    assert "malformed payload" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_requeues_stale_generation_with_string_status(monkeypatch):
    generation = Generation(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=1,
    )
    fake_session = _patch_recon_session_local(monkeypatch, [generation], [])
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == [("run_generation", "gen-1")]
    assert generation.status == GenerationStatus.QUEUED.value
    assert isinstance(generation.status, str)
    assert fake_session.select_skip_locked == [True, True]
    assert published == [
        {
            "user_id": "user-1",
            "channel": "user:user-1",
            "event_name": "generation.requeued",
            "data": {
                "generation_id": "gen-1",
                "message_id": "msg-1",
                "attempt": 1,
                "max_attempts": 5,
                "kind": "generation",
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_skips_task_with_active_lease(monkeypatch):
    generation = Generation(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        attempt=1,
    )
    _patch_recon_session_local(monkeypatch, [generation], [])
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()
    redis.keys["task:gen-1:lease"] = "active"

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 0
    assert redis.enqueued == []
    assert generation.status == "running"
    assert published == []


@pytest.mark.asyncio
async def test_reconcile_marks_max_attempt_completion_failed_with_string_status(monkeypatch):
    message = Message(
        id="msg-1",
        conversation_id="conv-1",
        role="assistant",
        content={},
        status=MessageStatus.STREAMING.value,
    )
    completion = Completion(
        id="comp-1",
        user_id="user-1",
        message_id="msg-1",
        status="streaming",
        progress_stage="streaming",
        attempt=3,
    )
    _patch_recon_session_local(monkeypatch, [], [completion], {"msg-1": message})
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == []
    assert completion.status == CompletionStatus.FAILED.value
    assert isinstance(completion.status, str)
    assert completion.error_code == "timeout"
    assert completion.finished_at is not None
    assert message.status == MessageStatus.FAILED.value
    assert published == [
        {
            "user_id": "user-1",
            "channel": "user:user-1",
            "event_name": "completion.failed",
            "data": {
                "completion_id": "comp-1",
                "message_id": "msg-1",
                "attempt": 3,
                "attempt_epoch": 3,
                "code": "timeout",
                "message": "task stuck; reconciler timed out",
                "retriable": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_marks_max_attempt_generation_failed_and_message_failed(monkeypatch):
    message = Message(
        id="msg-1",
        conversation_id="conv-1",
        role="assistant",
        content={},
        status=MessageStatus.STREAMING.value,
    )
    generation = Generation(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=5,
    )
    _patch_recon_session_local(monkeypatch, [generation], [], {"msg-1": message})
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == []
    assert generation.status == GenerationStatus.FAILED.value
    assert generation.error_code == "timeout"
    assert generation.finished_at is not None
    assert message.status == MessageStatus.FAILED.value
    assert published == [
        {
            "user_id": "user-1",
            "channel": "user:user-1",
            "event_name": "generation.failed",
            "data": {
                "generation_id": "gen-1",
                "message_id": "msg-1",
                "code": "timeout",
                "message": "task stuck; reconciler timed out",
                "retriable": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_requeues_stale_completion_and_publishes_event(monkeypatch):
    completion = Completion(
        id="comp-1",
        user_id="user-1",
        message_id="msg-1",
        status="streaming",
        progress_stage="streaming",
        attempt=2,
    )
    _patch_recon_session_local(monkeypatch, [], [completion])
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == [("run_completion", "comp-1")]
    assert completion.status == CompletionStatus.QUEUED.value
    assert published == [
        {
            "user_id": "user-1",
            "channel": "user:user-1",
            "event_name": "completion.requeued",
            "data": {
                "completion_id": "comp-1",
                "message_id": "msg-1",
                "attempt": 2,
                "attempt_epoch": 2,
                "max_attempts": 3,
                "kind": "completion",
            },
        }
    ]
