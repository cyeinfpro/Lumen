from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys

import pytest
from arq.connections import job_key_prefix, result_key_prefix
from arq.constants import in_progress_key_prefix

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen_core.constants import CompletionStatus, GenerationStatus, MessageStatus
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    MemoryExtractionRun,
    Message,
    OutboxDeadLetter,
    OutboxEvent,
    User,
)
from app import observability, sse_publish
from app import generation_dispatch
from app.outbox import publisher as outbox_publisher
from app.outbox.claims import (
    DeliveryResult,
    claim_outbox_events,
    finalize_outbox_results,
)
from app.outbox.contracts import ClaimedOutboxEvent
from app.tasks import outbox


class FakeRedis:
    def __init__(self, *, fail_enqueue: bool = False) -> None:
        self.fail_enqueue = fail_enqueue
        self.enqueued: list[tuple[str, str]] = []
        self.enqueue_calls: list[tuple[str, str, dict[str, object]]] = []
        self.enqueue_args: list[tuple[str, tuple[str, ...], dict[str, object]]] = []
        self.active_job_ids: set[str] = set()
        self.result_job_ids: set[str] = set()
        self.in_progress_job_ids: set[str] = set()
        self.deduped_job_ids: list[str] = []
        self.keys: dict[str, str] = {}
        self.deleted: list[str] = []
        self.renewed: list[tuple[str, str, str]] = []

    async def set(self, key: str, value: str, **kwargs):
        if kwargs.get("nx") and key in self.keys:
            return False
        self.keys[key] = value
        return True

    async def delete(self, key: str):
        self.deleted.append(key)
        self.keys.pop(key, None)
        return 1

    async def get(self, key: str):
        return self.keys.get(key)

    async def exists(self, *keys: str) -> int:
        found = 0
        for key in keys:
            if key.startswith(job_key_prefix):
                found += key.removeprefix(job_key_prefix) in self.active_job_ids
            elif key.startswith(result_key_prefix):
                found += key.removeprefix(result_key_prefix) in self.result_job_ids
            elif key.startswith(in_progress_key_prefix):
                found += (
                    key.removeprefix(in_progress_key_prefix) in self.in_progress_job_ids
                )
            else:
                found += key in self.keys
        return found

    async def enqueue_job(
        self,
        job_name: str,
        task_id: str,
        *args: str,
        **kwargs,
    ):
        self.enqueue_calls.append((job_name, task_id, dict(kwargs)))
        self.enqueue_args.append((job_name, (task_id, *args), dict(kwargs)))
        if self.fail_enqueue:
            raise RuntimeError("redis unavailable")

        requested_job_id = kwargs.get("_job_id")
        if requested_job_id is not None and not isinstance(requested_job_id, str):
            raise AssertionError("_job_id must be a string")
        if requested_job_id and (
            requested_job_id in self.active_job_ids
            or requested_job_id in self.result_job_ids
        ):
            self.deduped_job_ids.append(requested_job_id)
            return None

        self.enqueued.append((job_name, task_id))
        job_id = requested_job_id or f"job:{task_id}:{len(self.enqueued)}"
        self.active_job_ids.add(job_id)
        return SimpleNamespace(job_id=job_id)

    def complete_job(self, job_id: str) -> None:
        self.active_job_ids.remove(job_id)
        self.result_job_ids.add(job_id)

    async def eval(self, script: str, _keys: int, key: str, *args: str):
        if script == generation_dispatch.BEGIN_DISPATCH_LUA:
            revision_key, attempt, _ttl, _revision_ttl, replace_value = args
            current = self.keys.get(key)
            if current is not None:
                current_attempt = int(current.split("|", 1)[0])
                if current_attempt > int(attempt):
                    return [0, current]
                if current_attempt == int(attempt) and current != replace_value:
                    return [0, current]
            revision = int(self.keys.get(revision_key) or 0) + 1
            self.keys[revision_key] = str(revision)
            value = f"{attempt}|{revision}|reserved|"
            self.keys[key] = value
            return [1, value]
        if script == generation_dispatch.MARK_DISPATCH_ENQUEUED_LUA:
            reserved_value, enqueued_value, _ttl = args
            if self.keys.get(key) != reserved_value:
                return 0
            self.keys[key] = enqueued_value
            return 1
        if script == generation_dispatch.CONSUME_DISPATCH_LUA:
            prefix, worker_id, _ttl = args
            current = self.keys.get(key)
            if current is None or not current.startswith(prefix):
                return 0
            phase = current.split("|", 3)[2]
            if phase not in {"reserved", "enqueued"}:
                return 0
            self.keys[key] = f"{prefix}consumed|{worker_id}"
            return 1
        if script == generation_dispatch.FINISH_DISPATCH_LUA:
            prefix = args[0]
            current = self.keys.get(key)
            if current is None or not current.startswith(prefix):
                return 0
            self.keys.pop(key, None)
            return 1
        if script == outbox._RELEASE_OWNED_LOCK_LUA:  # noqa: SLF001
            token = args[0]
            if self.keys.get(key) != token:
                return 0
            self.keys.pop(key, None)
            return 1
        if script == outbox._RENEW_OWNED_LOCK_LUA:  # noqa: SLF001
            token, ttl = args
            if self.keys.get(key) != token:
                return 0
            self.renewed.append((key, token, ttl))
            return 1
        field, ttl = args
        value = int(self.keys.get(f"{key}:{field}") or 0) + 1
        self.keys[f"{key}:{field}"] = str(value)
        self.keys[f"{key}:ttl"] = str(ttl)
        return value

    async def hincrby(self, _key: str, _field: str, _amount: int):
        return 1

    async def expire(self, _key: str, _ttl: int):
        return True

    async def hdel(self, key: str, field: str):
        self.keys.pop(f"{key}:{field}", None)
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


class FakeReturningResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class FakeUpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _fake_claim_rows(
    events: list[OutboxEvent],
    statement: object,
) -> FakeReturningResult:
    params = statement.compile().params
    owner = params["claim_owner"]
    claim_until = params["claim_until"]
    cutoff = params["created_at_1"]
    now = params["claim_until_1"]
    limit = params["param_1"]
    claimed: list[object] = []
    fallback_created_at = datetime.min.replace(tzinfo=timezone.utc)
    for event in sorted(
        events,
        key=lambda row: (row.created_at or fallback_created_at, row.id),
    ):
        if len(claimed) >= limit:
            break
        created_at = event.created_at or fallback_created_at
        if (
            event.published_at is not None
            or created_at >= cutoff
            or (event.claim_until is not None and event.claim_until > now)
            or (
                getattr(event, "next_attempt_at", None) is not None
                and event.next_attempt_at > now
            )
        ):
            continue
        event.claim_owner = owner
        event.claim_until = claim_until
        event.delivery_attempts = int(event.delivery_attempts or 0) + 1
        claimed.append(
            SimpleNamespace(
                id=event.id,
                kind=event.kind,
                payload=event.payload,
                delivery_attempts=event.delivery_attempts,
                claim_until=event.claim_until,
            )
        )
    return FakeReturningResult(claimed)


class FakeTransaction:
    """模拟 `async with session.begin()`：正常退出时提交，异常时回滚。"""

    def __init__(self, session: Any) -> None:
        self.session = session
        self._event_snapshots: list[tuple[OutboxEvent, tuple[object, ...]]] = []

    async def __aenter__(self) -> Any:
        self.session.transaction_depth = (
            getattr(self.session, "transaction_depth", 0) + 1
        )
        for event in getattr(self.session, "events", []):
            self._event_snapshots.append(
                (
                    event,
                    (
                        event.published_at,
                        event.claim_owner,
                        event.claim_until,
                        event.next_attempt_at,
                        event.delivery_attempts,
                        event.last_delivery_error,
                    ),
                )
            )
        return self.session

    async def __aexit__(self, exc_type: Any, *_rest: Any) -> None:
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self._rollback()
        except Exception:
            await self._rollback()
            raise
        finally:
            self.session.transaction_depth -= 1

    async def _rollback(self) -> None:
        for event, values in self._event_snapshots:
            (
                event.published_at,
                event.claim_owner,
                event.claim_until,
                event.next_attempt_at,
                event.delivery_attempts,
                event.last_delivery_error,
            ) = values
        rollback = getattr(self.session, "rollback", None)
        if rollback is not None:
            await rollback()


class FakeSession:
    def __init__(self, events: list[OutboxEvent]) -> None:
        self.events = events
        self.added: list[object] = []
        self.dead_letters: list[OutboxDeadLetter] = []
        self.transaction_depth = 0
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        statement_text = str(statement)
        if (
            getattr(statement, "is_update", False)
            and "UPDATE outbox_events" in statement_text
            and "RETURNING" in statement_text
        ):
            return _fake_claim_rows(self.events, statement)
        if statement.is_select:
            if "FROM outbox_dead_letter" in statement_text:
                return FakeScalarResult(
                    [row.id for row in self.dead_letters if row.resolved_at is None]
                )
            return FakeScalarResult(
                [ev for ev in self.events if ev.published_at is None]
            )
        if getattr(statement, "is_update", False):
            if "outbox_dead_letter" in statement_text:
                now = datetime.now(timezone.utc)
                for row in self.dead_letters:
                    if row.resolved_at is None:
                        row.resolved_at = now
            return FakeUpdateResult(1)
        rowcount = 0
        for ev in self.events:
            if ev.published_at is None:
                ev.published_at = datetime.now(timezone.utc)
                rowcount = 1
                break
        return FakeUpdateResult(rowcount)

    async def commit(self) -> None:
        self.commits += 1
        return None

    async def rollback(self) -> None:
        return None

    def add(self, row: object) -> None:
        self.added.append(row)
        if isinstance(row, OutboxDeadLetter):
            if row.id is None:
                row.id = f"dlq-{len(self.dead_letters) + 1}"
            self.dead_letters.append(row)


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
        self.outbox_events: list[OutboxEvent] = []
        self.select_skip_locked: list[bool] = []
        self.info: dict[str, object] = {}
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        text = str(statement)
        arg = getattr(statement, "_for_update_arg", None)
        if (
            getattr(statement, "is_update", False)
            and "UPDATE outbox_events" in text
            and "RETURNING" in text
        ):
            return _fake_claim_rows(self.outbox_events, statement)
        if statement.is_select:
            self.select_skip_locked.append(arg is not None and arg.skip_locked is True)
        if "FROM outbox_events" in text:
            return FakeScalarResult(
                [event for event in self.outbox_events if event.published_at is None]
            )
        if "FROM generations" in text:
            return FakeScalarResult(self.generations)
        if "FROM completions" in text:
            return FakeScalarResult(self.completions)
        if getattr(statement, "is_update", False):
            return FakeUpdateResult(0)
        return FakeScalarResult([])

    async def get(self, model, object_id: str):
        if model is Message:
            return self.messages.get(object_id)
        if model is OutboxEvent:
            return next(
                (event for event in self.outbox_events if event.id == object_id),
                None,
            )
        return None

    async def commit(self) -> None:
        self.commits += 1
        return None

    def add(self, row: object) -> None:
        if isinstance(row, OutboxEvent):
            self.outbox_events.append(row)


class FakeMemoryReconSession:
    def __init__(
        self,
        runs: list[MemoryExtractionRun],
        *,
        users: dict[str, object],
        conversations: dict[str, object],
        messages: dict[str, object],
    ) -> None:
        self.runs = runs
        self.users = users
        self.conversations = conversations
        self.messages = messages
        self.outbox_events: list[OutboxEvent] = []
        self.select_skip_locked: list[bool] = []
        self.statements: list[Any] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return FakeTransaction(self)

    async def execute(self, statement):
        text = str(statement)
        self.statements.append(statement)
        arg = getattr(statement, "_for_update_arg", None)
        if (
            getattr(statement, "is_update", False)
            and "UPDATE outbox_events" in text
            and "RETURNING" in text
        ):
            return _fake_claim_rows(self.outbox_events, statement)
        if statement.is_select:
            self.select_skip_locked.append(arg is not None and arg.skip_locked is True)
            if "FROM memory_extraction_runs" in text:
                return FakeScalarResult(self.runs)
        if getattr(statement, "is_update", False):
            return FakeUpdateResult(0)
        return FakeScalarResult([])

    async def get(self, model, object_id: str):
        if model is User:
            return self.users.get(object_id)
        if model is Conversation:
            return self.conversations.get(object_id)
        if model is Message:
            return self.messages.get(object_id)
        if model is OutboxEvent:
            return next(
                (event for event in self.outbox_events if event.id == object_id),
                None,
            )
        return None

    async def commit(self) -> None:
        self.commits += 1

    def add(self, row: object) -> None:
        if isinstance(row, OutboxEvent):
            self.outbox_events.append(row)


def _patch_session_local(
    monkeypatch: pytest.MonkeyPatch, events: list[OutboxEvent]
) -> FakeSession:
    fake_session = FakeSession(events)

    @asynccontextmanager
    async def session_local():
        yield fake_session

    monkeypatch.setattr(outbox, "SessionLocal", session_local)
    return fake_session


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


def _patch_memory_recon_session_local(
    monkeypatch: pytest.MonkeyPatch,
    runs: list[MemoryExtractionRun],
    *,
    users: dict[str, object],
    conversations: dict[str, object],
    messages: dict[str, object],
) -> FakeMemoryReconSession:
    fake_session = FakeMemoryReconSession(
        runs,
        users=users,
        conversations=conversations,
        messages=messages,
    )

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


def _memory_event(
    *,
    event_id: str,
    assistant_message_id: str,
    source_message_id: str = "user-message-1",
    conversation_id: str = "conversation-1",
) -> OutboxEvent:
    return OutboxEvent(
        id=event_id,
        kind="memory_extract",
        payload={
            "task_id": assistant_message_id,
            "event_id": (f"memory-extract:{source_message_id}:{assistant_message_id}"),
            "conversation_id": conversation_id,
            "source_user_message_id": source_message_id,
            "assistant_message_id": assistant_message_id,
        },
        created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )


def _memory_run(
    *,
    suffix: str,
    status: str,
    attempt: int,
    updated_at: datetime,
    lease_expires_at: datetime | None,
) -> MemoryExtractionRun:
    source_message_id = f"user-message-{suffix}"
    assistant_message_id = f"assistant-{suffix}"
    return MemoryExtractionRun(
        id=f"memory-run-{suffix}",
        event_id=f"memory-extract:{source_message_id}:{assistant_message_id}",
        user_id=f"user-{suffix}",
        conversation_id=f"conversation-{suffix}",
        source_message_id=source_message_id,
        assistant_message_id=assistant_message_id,
        status=status,
        owner=f"owner-{suffix}",
        job_id=f"job-{suffix}",
        fence=3,
        attempt=attempt,
        recovery_count=1,
        claimed_at=updated_at,
        lease_expires_at=lease_expires_at,
        retry_reason="upstream unavailable",
        memory_writes=[],
        undo_operations=[],
        undo_status="none",
        created_at=updated_at - timedelta(minutes=1),
        updated_at=updated_at,
    )


def _memory_entities(
    run: MemoryExtractionRun,
    *,
    assistant_deleted: bool = False,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    user = SimpleNamespace(
        id=run.user_id,
        deleted_at=None,
        memory_disabled=False,
        memory_paused=False,
    )
    conversation = SimpleNamespace(
        id=run.conversation_id,
        user_id=run.user_id,
        deleted_at=None,
        memory_disabled=False,
    )
    source_message = SimpleNamespace(
        id=run.source_message_id,
        conversation_id=run.conversation_id,
        role="user",
        deleted_at=None,
        status="succeeded",
        parent_message_id=None,
    )
    assistant_message = SimpleNamespace(
        id=run.assistant_message_id,
        conversation_id=run.conversation_id,
        role="assistant",
        deleted_at=(datetime.now(timezone.utc) if assistant_deleted else None),
        status="succeeded",
        parent_message_id=run.source_message_id,
    )
    return (
        {run.user_id: user},
        {run.conversation_id: conversation},
        {
            run.source_message_id: source_message,
            run.assistant_message_id: assistant_message,
        },
    )


@pytest.mark.asyncio
async def test_publish_outbox_marks_published_only_after_enqueue_success(monkeypatch):
    events = [_event(task_id="gen-1")]
    session = _patch_session_local(monkeypatch, events)

    class TransactionAwareRedis(FakeRedis):
        async def enqueue_job(self, *args, **kwargs):
            assert session.transaction_depth == 0
            return await super().enqueue_job(*args, **kwargs)

    redis = TransactionAwareRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert redis.enqueued == [("run_generation", "gen-1")]
    assert redis.enqueue_calls == [
        (
            "run_generation",
            "gen-1",
            {
                "_job_id": "lumen:generation:gen-1:attempt:1:dispatch:1",
                "_job_try": 1,
            },
        )
    ]
    assert events[0].published_at is not None
    assert session.commits == 2


@pytest.mark.asyncio
async def test_generation_outbox_accepts_enqueue_that_raises_after_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(event_id="event-accepted", task_id="gen-accepted")
    _patch_session_local(monkeypatch, [event])

    class AcceptThenRaiseRedis(FakeRedis):
        async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
            await super().enqueue_job(*args, **kwargs)
            raise TimeoutError("enqueue acknowledgement lost")

    redis = AcceptThenRaiseRedis()
    processed = await outbox.publish_outbox({"redis": redis})

    job_id = "lumen:generation:gen-accepted:attempt:1:dispatch:1"
    assert processed == 1
    assert event.published_at is not None
    assert redis.enqueued == [("run_generation", "gen-accepted")]
    assert redis.active_job_ids == {job_id}
    assert redis.keys[
        generation_dispatch.dispatch_active_key("gen-accepted")
    ].startswith("1|1|enqueued|")


@pytest.mark.asyncio
async def test_outbox_claim_crash_is_reclaimable_only_after_ttl() -> None:
    event = _event(event_id="event-claim-crash", task_id="gen-claim-crash")
    session = FakeSession([event])

    @asynccontextmanager
    async def session_factory():
        yield session

    now = datetime.now(timezone.utc)
    first = await claim_outbox_events(
        session_factory=session_factory,
        cutoff=now,
        limit=1,
        owner="owner-a",
        now=now,
        claim_ttl_s=30,
    )
    assert [claimed.id for claimed in first] == [event.id]
    assert event.claim_owner == "owner-a"
    assert event.delivery_attempts == 1

    blocked = await claim_outbox_events(
        session_factory=session_factory,
        cutoff=now,
        limit=1,
        owner="owner-b",
        now=now + timedelta(seconds=29),
        claim_ttl_s=30,
    )
    assert blocked == []
    assert event.claim_owner == "owner-a"

    reclaimed = await claim_outbox_events(
        session_factory=session_factory,
        cutoff=now,
        limit=1,
        owner="owner-b",
        now=now + timedelta(seconds=31),
        claim_ttl_s=30,
    )
    assert [claimed.id for claimed in reclaimed] == [event.id]
    assert event.claim_owner == "owner-b"
    assert event.delivery_attempts == 2


@pytest.mark.asyncio
async def test_outbox_finalize_is_conditioned_on_claim_owner() -> None:
    event = _event(event_id="event-owner-race", task_id="gen-owner-race")
    now = datetime.now(timezone.utc)
    event.claim_owner = "owner-b"
    event.claim_until = now + timedelta(seconds=30)
    claimed = ClaimedOutboxEvent(
        id=event.id,
        kind=event.kind,
        payload=event.payload,
        delivery_attempts=1,
        claim_until=event.claim_until,
    )
    session = FakeSession([event])

    @asynccontextmanager
    async def session_factory():
        yield session

    finalized = await finalize_outbox_results(
        session_factory=session_factory,
        owner="owner-a",
        results=[DeliveryResult(event=claimed, state="published")],
        now=now,
        max_fail_count=outbox._OUTBOX_MAX_FAIL_COUNT,  # noqa: SLF001
    )

    assert finalized is not None
    assert finalized.published == 0
    assert finalized.lost_event_ids == (event.id,)
    assert event.published_at is None
    assert event.claim_owner == "owner-b"


@pytest.mark.asyncio
async def test_publish_outbox_bounds_delivery_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _event(event_id=f"event-{index}", task_id=f"gen-{index}")
        for index in range(outbox._OUTBOX_DELIVERY_CONCURRENCY * 2 + 1)  # noqa: SLF001
    ]
    _patch_session_local(monkeypatch, events)

    class SlowRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def enqueue_job(self, *args, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.005)
                return await super().enqueue_job(*args, **kwargs)
            finally:
                self.active -= 1

    redis = SlowRedis()
    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == len(events)
    assert 1 < redis.max_active <= outbox._OUTBOX_DELIVERY_CONCURRENCY  # noqa: SLF001


@pytest.mark.asyncio
async def test_publish_outbox_times_out_one_delivery_without_blocking_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _event(event_id="event-slow", task_id="gen-slow"),
        _event(event_id="event-fast", task_id="gen-fast"),
    ]
    _patch_session_local(monkeypatch, events)
    original_timeout = outbox_publisher.OUTBOX_DELIVERY_TIMEOUT_S
    monkeypatch.setattr(outbox_publisher, "OUTBOX_DELIVERY_TIMEOUT_S", 0.01)

    class PartiallySlowRedis(FakeRedis):
        async def enqueue_job(self, job_name: str, task_id: str, *args, **kwargs):
            if task_id == "gen-slow":
                await asyncio.sleep(1)
            return await super().enqueue_job(job_name, task_id, *args, **kwargs)

    redis = PartiallySlowRedis()
    processed = await outbox.publish_outbox({"redis": redis})

    assert original_timeout > 0.01
    assert processed == 1
    assert events[0].published_at is None
    assert events[0].claim_owner is None
    assert events[0].last_delivery_error is not None
    assert events[0].last_delivery_error.startswith("TimeoutError:")
    assert events[1].published_at is not None


@pytest.mark.asyncio
async def test_fast_path_and_publisher_replay_share_job_id_and_enqueue_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(event_id="event-replay", task_id="gen-replay")
    event.payload = {**event.payload, "outbox_id": event.id}
    redis = FakeRedis()

    async def leave_unpublished(event_id: str) -> bool:
        assert event_id == event.id
        return False

    monkeypatch.setattr(
        outbox,
        "_mark_staged_outbox_published",
        leave_unpublished,
    )

    await outbox._deliver_staged_outbox_events(  # noqa: SLF001
        redis,
        [(event.id, event.kind, event.payload)],
    )

    assert event.published_at is None
    assert not any(
        key.startswith(outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX) for key in redis.keys
    )

    _patch_session_local(monkeypatch, [event])
    processed = await outbox.publish_outbox({"redis": redis})

    expected_job_id = "lumen:generation:gen-replay:attempt:1:dispatch:1"
    assert processed == 1
    assert redis.enqueue_calls == [
        (
            "run_generation",
            "gen-replay",
            {"_job_id": expected_job_id, "_job_try": 1},
        ),
    ]
    assert redis.enqueued == [("run_generation", "gen-replay")]
    assert redis.deduped_job_ids == []
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_memory_extract_enqueue_failure_stays_unpublished_then_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _memory_event(
        event_id="memory-outbox-1",
        assistant_message_id="assistant-1",
    )
    _patch_session_local(monkeypatch, [event])
    redis = FakeRedis(fail_enqueue=True)

    first_processed = await outbox.publish_outbox({"redis": redis})

    assert first_processed == 0
    assert event.published_at is None

    redis.fail_enqueue = False
    event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    second_processed = await outbox.publish_outbox({"redis": redis})

    expected_job_id = "lumen:memory_extract:assistant-1:outbox:memory-outbox-1"
    assert second_processed == 1
    assert event.published_at is not None
    assert redis.enqueue_args == [
        (
            "memory_extract",
            ("conversation-1", "user-message-1", "assistant-1"),
            {"_job_id": expected_job_id},
        ),
        (
            "memory_extract",
            ("conversation-1", "user-message-1", "assistant-1"),
            {"_job_id": expected_job_id},
        ),
    ]


@pytest.mark.asyncio
async def test_memory_extract_fast_path_and_redrive_dedupe_same_outbox_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _memory_event(
        event_id="memory-outbox-dedupe",
        assistant_message_id="assistant-dedupe",
    )
    event.payload = {**event.payload, "outbox_id": event.id}
    redis = FakeRedis()

    async def leave_unpublished(event_id: str) -> bool:
        assert event_id == event.id
        return False

    monkeypatch.setattr(
        outbox,
        "_mark_staged_outbox_published",
        leave_unpublished,
    )
    await outbox._deliver_staged_outbox_events(  # noqa: SLF001
        redis,
        [(event.id, event.kind, event.payload)],
    )

    _patch_session_local(monkeypatch, [event])
    processed = await outbox.publish_outbox({"redis": redis})

    expected_job_id = (
        "lumen:memory_extract:assistant-dedupe:outbox:memory-outbox-dedupe"
    )
    assert processed == 1
    assert redis.enqueued == [("memory_extract", "conversation-1")]
    assert redis.deduped_job_ids == [expected_job_id]
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_memory_extract_regenerated_assistants_use_distinct_job_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _memory_event(
            event_id="memory-outbox-first",
            assistant_message_id="assistant-first",
        ),
        _memory_event(
            event_id="memory-outbox-second",
            assistant_message_id="assistant-second",
        ),
    ]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 2
    job_ids = [call[2]["_job_id"] for call in redis.enqueue_args]
    assert job_ids == [
        "lumen:memory_extract:assistant-first:outbox:memory-outbox-first",
        "lumen:memory_extract:assistant-second:outbox:memory-outbox-second",
    ]
    assert len(set(job_ids)) == 2
    assert all(event.published_at is not None for event in events)


@pytest.mark.asyncio
async def test_memory_reconciler_requeues_due_retryable_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    attempt = 3
    run = _memory_run(
        suffix="retryable",
        status="retryable",
        attempt=attempt,
        updated_at=now
        - timedelta(seconds=outbox._memory_retry_backoff_seconds(attempt) + 1),
        lease_expires_at=now - timedelta(seconds=1),
    )
    users, conversations, messages = _memory_entities(run)
    session = _patch_memory_recon_session_local(
        monkeypatch,
        [run],
        users=users,
        conversations=conversations,
        messages=messages,
    )

    class CommitAwareRedis(FakeRedis):
        async def enqueue_job(
            self,
            job_name: str,
            task_id: str,
            *args: str,
            **kwargs,
        ):
            assert session.commits >= 1
            assert run.status == "pending"
            assert session.outbox_events
            return await super().enqueue_job(
                job_name,
                task_id,
                *args,
                **kwargs,
            )

    redis = CommitAwareRedis()
    touched = await outbox.reconcile_memory_extractions({"redis": redis})

    event = session.outbox_events[0]
    assert touched == 1
    assert run.status == "pending"
    assert run.owner is None
    assert run.job_id is None
    assert run.lease_expires_at is None
    assert run.fence == 4
    assert run.recovery_count == 2
    assert event.payload["event_id"] == run.event_id
    assert event.published_at is not None
    assert redis.enqueue_args == [
        (
            "memory_extract",
            (
                run.conversation_id,
                run.source_message_id,
                run.assistant_message_id,
            ),
            {
                "_job_id": (
                    f"lumen:memory_extract:{run.assistant_message_id}:outbox:{event.id}"
                )
            },
        )
    ]
    assert session.select_skip_locked == [True]


@pytest.mark.asyncio
async def test_memory_reconciler_takes_over_expired_running_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    run = _memory_run(
        suffix="running",
        status="running",
        attempt=1,
        updated_at=now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    users, conversations, messages = _memory_entities(run)
    session = _patch_memory_recon_session_local(
        monkeypatch,
        [run],
        users=users,
        conversations=conversations,
        messages=messages,
    )

    touched = await outbox.reconcile_memory_extractions({"redis": FakeRedis()})

    assert touched == 1
    assert run.status == "pending"
    assert run.fence == 4
    assert run.recovery_count == 2
    assert session.outbox_events[0].payload["recovered_from"] == "running"


@pytest.mark.asyncio
async def test_memory_reconciler_skips_pending_and_retryable_before_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    pending = _memory_run(
        suffix="pending",
        status="pending",
        attempt=2,
        updated_at=now - timedelta(hours=1),
        lease_expires_at=now - timedelta(minutes=1),
    )
    waiting = _memory_run(
        suffix="waiting",
        status="retryable",
        attempt=4,
        updated_at=now,
        lease_expires_at=now - timedelta(seconds=1),
    )
    users: dict[str, object] = {}
    conversations: dict[str, object] = {}
    messages: dict[str, object] = {}
    for run in (pending, waiting):
        run_users, run_conversations, run_messages = _memory_entities(run)
        users.update(run_users)
        conversations.update(run_conversations)
        messages.update(run_messages)
    session = _patch_memory_recon_session_local(
        monkeypatch,
        [pending, waiting],
        users=users,
        conversations=conversations,
        messages=messages,
    )
    redis = FakeRedis()

    touched = await outbox.reconcile_memory_extractions({"redis": redis})

    assert touched == 0
    assert pending.status == "pending"
    assert waiting.status == "retryable"
    assert session.outbox_events == []
    assert redis.enqueue_args == []
    candidate_sql = str(session.statements[0])
    assert "memory_extraction_runs.attempt <= " in candidate_sql
    assert "memory_extraction_runs.attempt >= " in candidate_sql
    assert candidate_sql.index("memory_extraction_runs.attempt") < candidate_sql.index(
        "LIMIT"
    )


@pytest.mark.asyncio
async def test_memory_reconciler_enqueue_failure_recovers_via_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    run = _memory_run(
        suffix="redrive",
        status="retryable",
        attempt=1,
        updated_at=now - timedelta(minutes=2),
        lease_expires_at=now - timedelta(minutes=1),
    )
    users, conversations, messages = _memory_entities(run)
    session = _patch_memory_recon_session_local(
        monkeypatch,
        [run],
        users=users,
        conversations=conversations,
        messages=messages,
    )
    redis = FakeRedis(fail_enqueue=True)

    touched = await outbox.reconcile_memory_extractions({"redis": redis})

    event = session.outbox_events[0]
    assert touched == 1
    assert run.status == "pending"
    assert event.published_at is None

    _patch_session_local(monkeypatch, [event])
    redis.fail_enqueue = False
    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert event.published_at is not None
    assert redis.enqueued == [("memory_extract", run.conversation_id)]


@pytest.mark.asyncio
async def test_memory_reconciler_does_not_requeue_canceled_or_deleted_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    canceled = _memory_run(
        suffix="canceled",
        status="canceled",
        attempt=2,
        updated_at=now - timedelta(hours=1),
        lease_expires_at=now - timedelta(minutes=1),
    )
    deleted = _memory_run(
        suffix="deleted",
        status="retryable",
        attempt=2,
        updated_at=now - timedelta(hours=1),
        lease_expires_at=now - timedelta(minutes=1),
    )
    users: dict[str, object] = {}
    conversations: dict[str, object] = {}
    messages: dict[str, object] = {}
    for run, assistant_deleted in ((canceled, False), (deleted, True)):
        run_users, run_conversations, run_messages = _memory_entities(
            run,
            assistant_deleted=assistant_deleted,
        )
        users.update(run_users)
        conversations.update(run_conversations)
        messages.update(run_messages)
    session = _patch_memory_recon_session_local(
        monkeypatch,
        [canceled, deleted],
        users=users,
        conversations=conversations,
        messages=messages,
    )
    redis = FakeRedis()

    touched = await outbox.reconcile_memory_extractions({"redis": redis})

    assert touched == 1
    assert canceled.status == "canceled"
    assert deleted.status == "canceled"
    assert deleted.cancel_reason == "assistant_message_deleted"
    assert session.outbox_events == []
    assert redis.enqueue_args == []


def test_memory_reconciler_is_registered_as_cron() -> None:
    job = next(
        item
        for item in outbox.cron_jobs
        if item.coroutine is outbox.reconcile_memory_extractions
    )

    assert job.second == {20}
    assert job.run_at_startup is True


@pytest.mark.asyncio
async def test_same_task_new_outbox_event_reuses_active_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _event(event_id="event-first", task_id="gen-shared"),
        _event(event_id="event-second", task_id="gen-shared"),
    ]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 2
    assert redis.enqueue_calls == [
        (
            "run_generation",
            "gen-shared",
            {
                "_job_id": "lumen:generation:gen-shared:attempt:1:dispatch:1",
                "_job_try": 1,
            },
        ),
    ]
    assert redis.enqueued == [("run_generation", "gen-shared")]
    assert redis.deduped_job_ids == []


@pytest.mark.asyncio
async def test_explicit_retry_is_not_suppressed_by_prior_arq_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_event = _event(event_id="event-original", task_id="gen-retry")
    session = _patch_session_local(monkeypatch, [first_event])
    redis = FakeRedis()

    first_processed = await outbox.publish_outbox({"redis": redis})
    original_job_id = "lumen:generation:gen-retry:attempt:1:dispatch:1"
    redis.complete_job(original_job_id)
    await generation_dispatch.finish_generation_dispatch(
        redis,
        generation_dispatch.DispatchIdentity("gen-retry", 1, 1),
    )

    retry_event = _event(event_id="event-explicit-retry", task_id="gen-retry")
    session.events.append(retry_event)
    retry_processed = await outbox.publish_outbox({"redis": redis})

    retry_job_id = "lumen:generation:gen-retry:attempt:1:dispatch:2"
    assert first_processed == 1
    assert retry_processed == 1
    assert redis.result_job_ids == {original_job_id}
    assert redis.active_job_ids == {retry_job_id}
    assert redis.enqueue_calls == [
        (
            "run_generation",
            "gen-retry",
            {"_job_id": original_job_id, "_job_try": 1},
        ),
        (
            "run_generation",
            "gen-retry",
            {"_job_id": retry_job_id, "_job_try": 1},
        ),
    ]
    assert redis.enqueued == [
        ("run_generation", "gen-retry"),
        ("run_generation", "gen-retry"),
    ]
    assert redis.deduped_job_ids == []
    assert retry_event.published_at is not None


@pytest.mark.asyncio
async def test_publish_outbox_lock_release_does_not_delete_successor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    observed_token: str | None = None

    async def process_batch(_redis, _cutoff, _limit):
        nonlocal observed_token
        observed_token = redis.keys[outbox._OUTBOX_LOCK_KEY]  # noqa: SLF001
        redis.keys[outbox._OUTBOX_LOCK_KEY] = "successor-token"  # noqa: SLF001
        return 0

    monkeypatch.setattr(outbox, "_process_outbox_batch", process_batch)

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 0
    assert observed_token is not None
    assert observed_token != "1"
    assert redis.keys[outbox._OUTBOX_LOCK_KEY] == "successor-token"  # noqa: SLF001


@pytest.mark.asyncio
async def test_owned_redis_lock_renews_only_while_token_is_owner() -> None:
    redis = FakeRedis()

    async with outbox._owned_redis_lock(  # noqa: SLF001
        redis,
        key="lock:test",
        ttl_s=1,
    ) as acquired:
        assert acquired is True
        token = redis.keys["lock:test"]
        await asyncio.sleep(0.4)
        assert redis.renewed
        assert all(call == ("lock:test", token, "1") for call in redis.renewed)
        redis.keys["lock:test"] = "new-owner"
        assert (
            await outbox._renew_owned_lock(  # noqa: SLF001
                redis,
                key="lock:test",
                token=token,
                ttl_s=1,
            )
            is False
        )

    assert redis.keys["lock:test"] == "new-owner"


@pytest.mark.asyncio
async def test_publish_outbox_delivers_sse_with_stable_outbox_id(monkeypatch):
    event = OutboxEvent(
        id="event-sse-1",
        kind="sse",
        payload={
            "user_id": "user-1",
            "channel": "task:video-1",
            "event_name": "video.failed",
            "data": {
                "video_generation_id": "video-1",
                "status": "failed",
                "submission_epoch": 2,
            },
        },
        created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    _patch_session_local(monkeypatch, [event])
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert redis.enqueued == []
    assert event.published_at is not None
    assert published == [
        {
            "user_id": "user-1",
            "channel": "task:video-1",
            "event_name": "video.failed",
            "data": {
                "video_generation_id": "video-1",
                "status": "failed",
                "submission_epoch": 2,
                "outbox_id": "event-sse-1",
                "event_id": "event-sse-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_sse_outbox_xadd_failure_stays_unpublished_then_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryingSseRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.sse_xadd_calls = 0
            self.sse_event_ids: list[str] = []
            self.stream_entries: list[tuple[str, dict[str, str]]] = []
            self.pubsub_payloads: list[dict] = []
            self.sse_dlq_payloads: list[dict] = []

        async def eval(self, script: str, keys: int, key: str, *args: str):
            if script != sse_publish._XADD_IDEMPOTENT_LUA:  # noqa: SLF001
                return await super().eval(script, keys, key, *args)

            dedupe_key, event_id, event_name, payload_json = args[:4]
            self.sse_xadd_calls += 1
            self.sse_event_ids.append(event_id)
            if self.sse_xadd_calls <= 3:
                raise RuntimeError("redis stream unavailable")

            existing = self.keys.get(dedupe_key)
            if existing:
                return existing
            stream_id = "1710000000000-0"
            self.keys[dedupe_key] = stream_id
            self.stream_entries.append(
                (
                    key,
                    {
                        "event": event_name,
                        "data": payload_json,
                        "event_id": event_id,
                    },
                )
            )
            return stream_id

        async def publish(self, _channel: str, payload: str) -> int:
            self.pubsub_payloads.append(json.loads(payload))
            return 1

        async def lpush(self, key: str, payload: str) -> int:
            if key.endswith(":dlq"):
                self.sse_dlq_payloads.append(json.loads(payload))
            return 1

    async def fake_sleep(_delay: float) -> None:
        return None

    async def fake_persist_sse_dlq(**_kwargs) -> bool:
        return True

    event = OutboxEvent(
        id="event-sse-retry",
        kind="sse",
        payload={
            "user_id": "user-1",
            "channel": "task:video-1",
            "event_name": "video.failed",
            "data": {"video_generation_id": "video-1", "status": "failed"},
        },
        created_at=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    _patch_session_local(monkeypatch, [event])
    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sse_publish, "_persist_sse_dlq", fake_persist_sse_dlq)
    redis = RetryingSseRedis()

    first_processed = await outbox.publish_outbox({"redis": redis})

    assert first_processed == 0
    assert event.published_at is None
    assert len(redis.sse_dlq_payloads) == 1
    assert redis.sse_dlq_payloads[0]["event_id"] == event.id
    assert not any(
        key.startswith(outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX) for key in redis.keys
    )

    event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    second_processed = await outbox.publish_outbox({"redis": redis})

    assert second_processed == 1
    assert event.published_at is not None
    assert redis.sse_event_ids == [event.id, event.id, event.id, event.id]
    assert len(redis.stream_entries) == 1
    assert redis.stream_entries[0][1]["event_id"] == event.id
    assert redis.pubsub_payloads[0]["event_id"] == event.id
    assert redis.pubsub_payloads[0]["sse_id"] == "1710000000000-0"


@pytest.mark.asyncio
async def test_publish_outbox_processes_batch_in_one_pass(monkeypatch):
    events = [
        _event(event_id="event-1", task_id="gen-1", kind="generation"),
        _event(event_id="event-2", task_id="comp-1", kind="completion"),
        _event(event_id="event-3", task_id="video-1", kind="video_generation"),
        _event(event_id="event-4", task_id="agent-run-1", kind="agent_run"),
    ]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 4
    assert redis.enqueued == [
        ("run_generation", "gen-1"),
        ("run_completion", "comp-1"),
        ("run_video_generation", "video-1"),
        ("run_agent", "agent-run-1"),
    ]
    agent_call = redis.enqueue_calls[-1]
    assert agent_call[2]["_job_id"] == (
        "lumen:agent_run:agent-run-1:outbox:event-4"
    )
    assert all(ev.published_at is not None for ev in events)


@pytest.mark.asyncio
async def test_generation_outbox_failed_before_write_recovers_reserved_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [_event(task_id="gen-1")]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis(fail_enqueue=True)

    first_processed = await outbox.publish_outbox({"redis": redis})

    assert first_processed == 0
    assert redis.enqueued == []
    assert events[0].published_at is None
    assert not any(
        key.startswith(outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX) for key in redis.keys
    )

    redis.fail_enqueue = False
    events[0].next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    second_processed = await outbox.publish_outbox({"redis": redis})

    job_id = "lumen:generation:gen-1:attempt:1:dispatch:1"
    assert second_processed == 1
    assert events[0].published_at is not None
    assert redis.enqueued == [("run_generation", "gen-1")]
    assert redis.active_job_ids == {job_id}
    assert [call[2]["_job_id"] for call in redis.enqueue_calls] == [job_id, job_id]


@pytest.mark.asyncio
async def test_permanent_failures_do_not_starve_newer_outbox_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    poison_events = [
        OutboxEvent(
            id=f"poison-event-{index:03d}",
            kind="completion",
            payload={"task_id": f"poison-task-{index:03d}"},
            created_at=now - timedelta(minutes=2) + timedelta(milliseconds=index),
            delivery_attempts=outbox._OUTBOX_MAX_FAIL_COUNT - 1,  # noqa: SLF001
        )
        for index in range(outbox._OUTBOX_BATCH)  # noqa: SLF001
    ]
    healthy_event = OutboxEvent(
        id="healthy-event",
        kind="completion",
        payload={"task_id": "healthy-task"},
        created_at=now - timedelta(minutes=1),
    )
    session = _patch_session_local(
        monkeypatch,
        [*poison_events, healthy_event],
    )

    class SelectiveFailureRedis(FakeRedis):
        async def enqueue_job(
            self,
            job_name: str,
            task_id: str,
            *args: str,
            **kwargs: object,
        ) -> Any:
            if task_id.startswith("poison-task-"):
                raise RuntimeError("permanent enqueue failure")
            return await super().enqueue_job(job_name, task_id, *args, **kwargs)

    redis = SelectiveFailureRedis()
    assert await outbox.publish_outbox({"redis": redis}) == 0
    assert healthy_event.published_at is None

    assert await outbox.publish_outbox({"redis": redis}) == 1
    assert healthy_event.published_at is not None
    assert redis.enqueued == [("run_completion", "healthy-task")]
    assert all(event.published_at is None for event in poison_events)
    assert len(session.dead_letters) >= 1


@pytest.mark.asyncio
async def test_storyboard_outbox_dlq_keeps_redrive_until_enqueue_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(
        event_id="storyboard-event-1",
        task_id="storyboard-run-1",
        kind="storyboard_assembly",
    )
    event.delivery_attempts = outbox._OUTBOX_MAX_FAIL_COUNT - 1  # noqa: SLF001
    session = _patch_session_local(monkeypatch, [event])
    redis = FakeRedis(fail_enqueue=True)

    first_processed = await outbox.publish_outbox({"redis": redis})

    assert first_processed == 0
    assert event.published_at is None
    assert len(session.dead_letters) == 1
    dead_letter = session.dead_letters[0]
    assert dead_letter.event_type == "outbox.storyboard_assembly"
    assert dead_letter.error_class == "OutboxEnqueueFailed"
    assert dead_letter.resolved_at is None

    event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    second_processed = await outbox.publish_outbox({"redis": redis})

    assert second_processed == 0
    assert event.published_at is None
    assert len(session.dead_letters) == 1

    redis.fail_enqueue = False
    event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    recovered_processed = await outbox.publish_outbox({"redis": redis})

    assert recovered_processed == 1
    assert redis.enqueued == [("run_storyboard_assembly", "storyboard-run-1")]
    assert event.published_at is not None
    assert dead_letter.resolved_at is not None


@pytest.mark.asyncio
async def test_pg_attempt_threshold_creates_one_dlq_when_redis_counter_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CounterDownRedis(FakeRedis):
        async def eval(
            self,
            script: str,
            keys: int,
            key: str,
            *args: str,
        ) -> int:
            if script == outbox._INCR_FAIL_COUNT_LUA:  # noqa: SLF001
                raise RuntimeError("redis counter unavailable")
            return await super().eval(script, keys, key, *args)

    event = _event(event_id="event-pg-threshold", task_id="gen-pg-threshold")
    event.delivery_attempts = outbox._OUTBOX_MAX_FAIL_COUNT - 1  # noqa: SLF001
    session = _patch_session_local(monkeypatch, [event])
    redis = CounterDownRedis(fail_enqueue=True)

    assert await outbox.publish_outbox({"redis": redis}) == 0
    assert event.delivery_attempts == outbox._OUTBOX_MAX_FAIL_COUNT  # noqa: SLF001
    assert len(session.dead_letters) == 1
    assert session.dead_letters[0].retry_count == event.delivery_attempts

    event.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await outbox.publish_outbox({"redis": redis}) == 0
    assert len(session.dead_letters) == 1


@pytest.mark.asyncio
async def test_redis_counter_cannot_force_early_durable_dlq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HugeCounterRedis(FakeRedis):
        async def eval(
            self,
            script: str,
            keys: int,
            key: str,
            *args: str,
        ) -> int:
            if script == outbox._INCR_FAIL_COUNT_LUA:  # noqa: SLF001
                return 10_000
            return await super().eval(script, keys, key, *args)

    event = _event(event_id="event-redis-huge", task_id="gen-redis-huge")
    session = _patch_session_local(monkeypatch, [event])

    assert (
        await outbox.publish_outbox({"redis": HugeCounterRedis(fail_enqueue=True)}) == 0
    )
    assert event.delivery_attempts == 1
    assert session.dead_letters == []


@pytest.mark.asyncio
async def test_increment_outbox_fail_count_sets_expiry_atomically() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[object, ...] | None = None

        async def eval(self, *args: object) -> int:
            self.eval_args = args
            return 2

        async def hincrby(self, *_args: object) -> int:
            raise AssertionError("fail count must not use non-atomic HINCRBY")

        async def expire(self, *_args: object) -> bool:
            raise AssertionError("fail count must not use separate EXPIRE")

    redis = Redis()

    count = await outbox._increment_outbox_fail_count(redis, "event-1")  # noqa: SLF001

    assert count == 2
    assert redis.eval_args == (
        outbox._INCR_FAIL_COUNT_LUA,  # noqa: SLF001
        1,
        outbox._OUTBOX_FAIL_COUNT_HASH,  # noqa: SLF001
        "event-1",
        str(outbox._OUTBOX_FAIL_COUNT_TTL_S),  # noqa: SLF001
    )


@pytest.mark.asyncio
async def test_publish_outbox_replays_after_finalize_commit_failure(monkeypatch):
    class _CommitFailSession(FakeSession):
        async def commit(self) -> None:
            self.commits += 1
            if self.commits == 2:
                raise RuntimeError("commit failed")

    events = [_event(task_id="gen-1")]
    session = _CommitFailSession(events)

    @asynccontextmanager
    async def session_local():
        yield session

    monkeypatch.setattr(outbox, "SessionLocal", session_local)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 0
    assert redis.enqueued == [("run_generation", "gen-1")]
    assert not any(
        key.startswith(outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX) for key in redis.keys
    )
    assert events[0].published_at is None
    assert events[0].claim_owner is not None

    events[0].claim_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert redis.enqueued == [("run_generation", "gen-1")]
    assert redis.deduped_job_ids == []
    assert events[0].published_at is not None


@pytest.mark.asyncio
async def test_publish_outbox_marks_deduped_event_without_second_enqueue(monkeypatch):
    events = [_event(task_id="gen-1")]
    _patch_session_local(monkeypatch, events)
    redis = FakeRedis()
    redis.keys[f"{outbox._OUTBOX_ENQUEUE_DEDUPE_PREFIX}{events[0].id}"] = "gen-1"

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
    session = _patch_session_local(monkeypatch, events)
    redis = FakeRedis()

    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 0
    assert redis.enqueued == []
    assert events[0].published_at is not None
    assert len(session.added) == 1
    dead_letter = session.added[0]
    assert isinstance(dead_letter, OutboxDeadLetter)
    assert dead_letter.outbox_id == event.id
    assert dead_letter.error_class == "OutboxMalformedPayload"
    assert dead_letter.error_message == "malformed_payload"
    assert redis.keys == {}
    assert "malformed payload" in caplog.text


@pytest.mark.asyncio
async def test_publish_outbox_dlq_uses_claim_and_finalize_sessions_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(task_id="gen-1")
    event.payload = []
    session = FakeSession([event])
    session_local_calls = 0

    @asynccontextmanager
    async def session_local():
        nonlocal session_local_calls
        session_local_calls += 1
        if session_local_calls > 2:
            raise AssertionError("DLQ persistence must use the finalize transaction")
        yield session

    monkeypatch.setattr(outbox, "SessionLocal", session_local)

    await outbox.publish_outbox({"redis": FakeRedis()})

    assert session_local_calls == 2
    assert len(session.added) == 1
    assert event.published_at is not None


@pytest.mark.asyncio
async def test_publish_outbox_redis_dlq_failure_does_not_roll_back_pg(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenDlqRedis(FakeRedis):
        async def lpush(self, _key: str, _payload: str) -> int:
            raise RuntimeError("redis unavailable")

    event = _event(task_id="gen-1")
    event.payload = ["bad"]
    session = _patch_session_local(monkeypatch, [event])

    processed = await outbox.publish_outbox({"redis": BrokenDlqRedis()})

    assert processed == 0
    assert event.published_at is not None
    assert len(session.added) == 1
    assert "Redis DLQ mirror failed" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_persists_outbox_and_commits_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = Generation(
        id="gen-commit-order",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=1,
    )
    fake_session = _patch_recon_session_local(monkeypatch, [generation], [])
    _patch_publish_event(monkeypatch)

    class CommitAwareRedis(FakeRedis):
        async def enqueue_job(
            self,
            job_name: str,
            task_id: str,
            *args: str,
            **kwargs: Any,
        ):
            assert fake_session.commits >= 1
            assert any(
                event.kind == "generation" and event.payload.get("task_id") == task_id
                for event in fake_session.outbox_events
            )
            return await super().enqueue_job(job_name, task_id, *args, **kwargs)

    redis = CommitAwareRedis()

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == [("run_generation", "gen-commit-order")]
    assert {event.kind for event in fake_session.outbox_events} == {
        "generation",
        "sse",
    }
    assert all(event.published_at is not None for event in fake_session.outbox_events)


@pytest.mark.asyncio
async def test_reconcile_enqueue_failure_leaves_durable_outbox_for_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = Generation(
        id="gen-redrive",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=1,
    )
    fake_session = _patch_recon_session_local(monkeypatch, [generation], [])
    _patch_publish_event(monkeypatch)
    redis = FakeRedis(fail_enqueue=True)

    touched = await outbox.reconcile_tasks({"redis": redis})

    task_event = next(
        event for event in fake_session.outbox_events if event.kind == "generation"
    )
    assert touched == 1
    assert generation.status == GenerationStatus.QUEUED.value
    assert redis.enqueued == []
    assert task_event.published_at is None
    assert fake_session.commits >= 1

    redis.fail_enqueue = False
    processed = await outbox.publish_outbox({"redis": redis})

    assert processed == 1
    assert redis.enqueued == [("run_generation", "gen-redrive")]
    assert task_event.published_at is not None


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
    assert fake_session.select_skip_locked == [False, True, False, True, True]
    published_outbox_id = published[0]["data"].pop("outbox_id")
    assert published[0]["data"].pop("event_id") == published_outbox_id
    assert published == [
        {
            "user_id": "user-1",
            "channel": "user:user-1",
            "event_name": "generation.requeued",
            "data": {
                "generation_id": "gen-1",
                "message_id": "msg-1",
                "attempt": 1,
                "execution_epoch": 0,
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
async def test_reconcile_lease_read_states_fail_closed() -> None:
    class Redis:
        async def get(self, key: str):
            if key == "task:active:lease":
                return "worker-1:token-1"
            if key == "task:expired:lease":
                return None
            raise RuntimeError("redis unavailable")

    redis = Redis()

    assert (
        await outbox._read_lease_state(redis, "active")  # noqa: SLF001
        is outbox._LeaseState.ACTIVE  # noqa: SLF001
    )
    assert (
        await outbox._read_lease_state(redis, "expired")  # noqa: SLF001
        is outbox._LeaseState.EXPIRED  # noqa: SLF001
    )
    assert (
        await outbox._read_lease_state(redis, "unknown")  # noqa: SLF001
        is outbox._LeaseState.UNKNOWN  # noqa: SLF001
    )
    assert await outbox._lease_expired(redis, "unknown") is False  # noqa: SLF001


@pytest.mark.asyncio
async def test_reconcile_unknown_generation_lease_requeues_only_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    generation = Generation(
        id="gen-unknown",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=1,
        updated_at=updated_at,
    )
    fake_session = _patch_recon_session_local(monkeypatch, [generation], [])
    published = _patch_publish_event(monkeypatch)

    class BrokenLeaseRedis(FakeRedis):
        lease_reads = 0

        async def get(self, key: str):
            if key == "task:gen-unknown:lease":
                self.lease_reads += 1
                if self.lease_reads == 1:
                    raise RuntimeError("redis unavailable")
            return await super().get(key)

    redis = BrokenLeaseRedis()
    released: list[str] = []

    async def release_generation(*_args, **_kwargs) -> None:
        released.append("generation")

    monkeypatch.setattr(
        outbox.worker_billing,
        "release_generation",
        release_generation,
    )

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 0
    assert generation.status == "running"
    assert generation.progress_stage == "rendering"
    assert generation.attempt == 1
    assert generation.updated_at == updated_at
    assert generation.error_code is None
    assert generation.finished_at is None
    assert fake_session.outbox_events == []
    assert released == []
    assert published == []

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert generation.status == GenerationStatus.QUEUED.value
    assert generation.progress_stage == "queued"
    assert generation.attempt == 1
    assert generation.updated_at != updated_at
    assert redis.enqueued == [("run_generation", "gen-unknown")]
    assert {event.kind for event in fake_session.outbox_events} == {
        "generation",
        "sse",
    }
    assert released == []
    assert len(published) == 1


@pytest.mark.asyncio
async def test_reconcile_aggregates_unknown_lease_metrics_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    generations = [
        Generation(
            id=f"gen-unknown-{index}",
            user_id="user-1",
            message_id=f"msg-gen-{index}",
            status="running",
            attempt=1,
        )
        for index in range(4)
    ]
    completions = [
        Completion(
            id=f"comp-unknown-{index}",
            user_id="user-1",
            message_id=f"msg-comp-{index}",
            status="streaming",
            attempt=1,
        )
        for index in range(2)
    ]
    _patch_recon_session_local(monkeypatch, generations, completions)
    _patch_publish_event(monkeypatch)

    class BrokenLeaseRedis(FakeRedis):
        async def get(self, key: str):
            if key.startswith("task:") and key.endswith(":lease"):
                raise RuntimeError("redis unavailable")
            return await super().get(key)

    generation_counter = observability.task_reconcile_lease_unknown_total.labels(
        kind="generation"
    )
    completion_counter = observability.task_reconcile_lease_unknown_total.labels(
        kind="completion"
    )
    generation_before = float(generation_counter._value.get())
    completion_before = float(completion_counter._value.get())

    with caplog.at_level("WARNING", logger=outbox.logger.name):
        touched = await outbox.reconcile_tasks({"redis": BrokenLeaseRedis()})

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == outbox.logger.name
        and "reconcile lease state unknown" in record.getMessage()
    ]
    assert touched == 0
    assert float(generation_counter._value.get()) == generation_before + 4
    assert float(completion_counter._value.get()) == completion_before + 2
    assert len(messages) == 1
    assert "total=6 generations=4 completions=2" in messages[0]
    assert "gen-unknown-0:RuntimeError" in messages[0]
    assert "gen-unknown-2:RuntimeError" in messages[0]
    assert "gen-unknown-3" not in messages[0]
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_reconcilers_do_not_duplicate_enqueue_while_owner_holds_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = Generation(
        id="gen-owned",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=1,
    )
    fake_session = _patch_recon_session_local(monkeypatch, [generation], [])
    _patch_publish_event(monkeypatch)

    class BlockingLeaseRedis(FakeRedis):
        def __init__(self) -> None:
            super().__init__()
            self.lease_read_started = asyncio.Event()
            self.allow_lease_read = asyncio.Event()

        async def get(self, key: str):
            if key == "task:gen-owned:lease":
                self.lease_read_started.set()
                await self.allow_lease_read.wait()
            return await super().get(key)

    redis = BlockingLeaseRedis()
    owner = asyncio.create_task(outbox.reconcile_tasks({"redis": redis}))
    await asyncio.wait_for(redis.lease_read_started.wait(), timeout=1)

    contender_result = await outbox.reconcile_tasks({"redis": redis})
    redis.allow_lease_read.set()
    owner_result = await asyncio.wait_for(owner, timeout=1)

    assert contender_result == 0
    assert owner_result == 1
    assert redis.enqueued == [("run_generation", "gen-owned")]
    assert [event.kind for event in fake_session.outbox_events] == ["generation", "sse"]


@pytest.mark.asyncio
async def test_reconcile_unknown_completion_does_not_block_expired_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion_updated_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    generation = Generation(
        id="gen-expired",
        user_id="user-1",
        message_id="msg-gen",
        status="running",
        progress_stage="rendering",
        attempt=1,
    )
    completion = Completion(
        id="comp-unknown",
        user_id="user-1",
        message_id="msg-comp",
        status="streaming",
        progress_stage="streaming",
        attempt=3,
        updated_at=completion_updated_at,
    )
    fake_session = _patch_recon_session_local(
        monkeypatch,
        [generation],
        [completion],
    )
    _patch_publish_event(monkeypatch)

    class PartiallyBrokenLeaseRedis(FakeRedis):
        async def get(self, key: str):
            if key == "task:comp-unknown:lease":
                raise RuntimeError("redis unavailable")
            return await super().get(key)

    redis = PartiallyBrokenLeaseRedis()
    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert generation.status == GenerationStatus.QUEUED.value
    assert redis.enqueued == [("run_generation", "gen-expired")]
    assert completion.status == "streaming"
    assert completion.progress_stage == "streaming"
    assert completion.attempt == 3
    assert completion.updated_at == completion_updated_at
    assert completion.error_code is None
    assert completion.finished_at is None
    assert all(
        event.payload.get("task_id") != "comp-unknown"
        and event.payload.get("data", {}).get("completion_id") != "comp-unknown"
        for event in fake_session.outbox_events
    )


@pytest.mark.asyncio
async def test_reconcile_marks_max_attempt_completion_failed_with_string_status(
    monkeypatch,
):
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

    async def release_completion(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        outbox.worker_billing,
        "release_completion",
        release_completion,
    )

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == []
    assert completion.status == CompletionStatus.FAILED.value
    assert isinstance(completion.status, str)
    assert completion.error_code == "timeout"
    assert completion.finished_at is not None
    assert message.status == MessageStatus.FAILED.value
    published_outbox_id = published[0]["data"].pop("outbox_id")
    assert published[0]["data"].pop("event_id") == published_outbox_id
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
                "execution_epoch": 0,
                "code": "timeout",
                "message": "task stuck; reconciler timed out",
                "retriable": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_marks_max_attempt_generation_failed_and_message_failed(
    monkeypatch,
):
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
        upstream_request={"upstream_response_received_at": "2026-07-26T00:00:00+00:00"},
    )
    _patch_recon_session_local(monkeypatch, [generation], [], {"msg-1": message})
    published = _patch_publish_event(monkeypatch)
    redis = FakeRedis()

    async def settle_generation_unknown_upstream(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        outbox.worker_billing,
        "settle_generation_unknown_upstream",
        settle_generation_unknown_upstream,
    )

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert redis.enqueued == []
    assert generation.status == GenerationStatus.FAILED.value
    assert generation.error_code == "timeout"
    assert generation.finished_at is not None
    assert message.status == MessageStatus.FAILED.value
    published_outbox_id = published[0]["data"].pop("outbox_id")
    assert published[0]["data"].pop("event_id") == published_outbox_id
    assert published == [
        {
            "user_id": "user-1",
            "channel": "user:user-1",
            "event_name": "generation.failed",
            "data": {
                "generation_id": "gen-1",
                "message_id": "msg-1",
                "execution_epoch": 0,
                "code": "timeout",
                "message": "task stuck; reconciler timed out",
                "retriable": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_flushes_generation_release_balance_cache_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = Generation(
        id="gen-1",
        user_id="user-1",
        message_id="msg-1",
        status="running",
        progress_stage="rendering",
        attempt=5,
        upstream_request={"upstream_response_received_at": "2026-07-26T00:00:00+00:00"},
    )
    fake_session = _patch_recon_session_local(monkeypatch, [generation], [])
    _patch_publish_event(monkeypatch)
    redis = FakeRedis()
    calls: list[tuple[str, int, dict[str, object]]] = []

    # running + attempt=5 → 上游已被调用，结果不可知 → 结算而非退款（纯转嫁）。
    async def settle_generation_unknown_upstream(
        session, gen, *, reason: str, knowledge: str
    ) -> None:
        assert gen is generation
        assert reason == "timeout"
        assert knowledge == "unknown"
        session.info["pending-balance-refresh"] = {"user-1": 960}
        calls.append(("settle", session.commits, dict(session.info)))

    async def flush_balance_cache_refreshes(session) -> None:
        calls.append(("flush", session.commits, dict(session.info)))
        session.info.clear()

    monkeypatch.setattr(
        outbox.worker_billing,
        "settle_generation_unknown_upstream",
        settle_generation_unknown_upstream,
    )
    monkeypatch.setattr(
        outbox.worker_billing,
        "flush_balance_cache_refreshes",
        flush_balance_cache_refreshes,
    )

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert calls == [
        ("settle", 0, {"pending-balance-refresh": {"user-1": 960}}),
        ("flush", 1, {"pending-balance-refresh": {"user-1": 960}}),
    ]
    assert fake_session.info == {}


@pytest.mark.asyncio
async def test_reconcile_flushes_completion_release_balance_cache_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = Completion(
        id="comp-1",
        user_id="user-1",
        message_id="msg-1",
        status="streaming",
        progress_stage="streaming",
        attempt=3,
        upstream_request={"upstream_response_received_at": "2026-07-26T00:00:00+00:00"},
    )
    fake_session = _patch_recon_session_local(monkeypatch, [], [completion])
    _patch_publish_event(monkeypatch)
    redis = FakeRedis()
    calls: list[tuple[str, int, dict[str, object]]] = []

    # streaming + attempt=3 → 上游已被调用，结果不可知 → 结算而非退款（纯转嫁）。
    async def settle_completion_unknown_upstream(
        session, comp, *, reason: str, knowledge: str
    ) -> None:
        assert comp is completion
        assert reason == "timeout"
        assert knowledge == "unknown"
        session.info["pending-balance-refresh"] = {"user-1": 970}
        calls.append(("settle", session.commits, dict(session.info)))

    async def flush_balance_cache_refreshes(session) -> None:
        calls.append(("flush", session.commits, dict(session.info)))
        session.info.clear()

    monkeypatch.setattr(
        outbox.worker_billing,
        "settle_completion_unknown_upstream",
        settle_completion_unknown_upstream,
    )
    monkeypatch.setattr(
        outbox.worker_billing,
        "flush_balance_cache_refreshes",
        flush_balance_cache_refreshes,
    )

    touched = await outbox.reconcile_tasks({"redis": redis})

    assert touched == 1
    assert calls == [
        ("settle", 0, {"pending-balance-refresh": {"user-1": 970}}),
        ("flush", 1, {"pending-balance-refresh": {"user-1": 970}}),
    ]
    assert fake_session.info == {}


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
    published_outbox_id = published[0]["data"].pop("outbox_id")
    assert published[0]["data"].pop("event_id") == published_outbox_id
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
                "execution_epoch": 0,
                "max_attempts": 3,
                "kind": "completion",
            },
        }
    ]


@pytest.mark.asyncio
async def test_reconcile_terminal_apply_and_release_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = Generation(
        id="gen-idempotent",
        user_id="user-1",
        message_id="msg-gen",
        status="running",
        progress_stage="rendering",
        attempt=5,
        upstream_request={"upstream_response_received_at": "2026-07-26T00:00:00+00:00"},
    )
    completion = Completion(
        id="comp-idempotent",
        user_id="user-1",
        message_id="msg-comp",
        status="streaming",
        progress_stage="streaming",
        attempt=3,
        upstream_request={"upstream_response_received_at": "2026-07-26T00:00:00+00:00"},
    )
    session = _patch_recon_session_local(
        monkeypatch,
        [generation],
        [completion],
    )
    _patch_publish_event(monkeypatch)
    # running/streaming + attempt>0 → 结算而非退款（纯转嫁）。
    settles: list[str] = []

    async def settle_generation_unknown_upstream(*_args, **_kwargs) -> None:
        settles.append("generation")

    async def settle_completion_unknown_upstream(*_args, **_kwargs) -> None:
        settles.append("completion")

    monkeypatch.setattr(
        outbox.worker_billing,
        "settle_generation_unknown_upstream",
        settle_generation_unknown_upstream,
    )
    monkeypatch.setattr(
        outbox.worker_billing,
        "settle_completion_unknown_upstream",
        settle_completion_unknown_upstream,
    )

    redis = FakeRedis()
    assert await outbox.reconcile_tasks({"redis": redis}) == 2
    assert await outbox.reconcile_tasks({"redis": redis}) == 0

    assert settles == ["generation", "completion"]
    assert [event.kind for event in session.outbox_events] == ["sse", "sse"]
