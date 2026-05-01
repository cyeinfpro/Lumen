from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import tasks
from lumen_core.constants import GenerationStage, GenerationStatus


class _Result:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    def __init__(self, results: list[_Result]) -> None:
        self.results = results
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.committed = False

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else _Result()

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


class _Redis:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}
        self.calls: list[tuple[Any, ...]] = []

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.calls.append(("set", key, value, ex))

    async def get(self, key: str) -> Any:
        self.calls.append(("get", key))
        return self.values.get(key)

    async def zrem(self, key: str, member: str) -> None:
        self.calls.append(("zrem", key, member))

    async def delete(self, *keys: str) -> None:
        self.calls.append(("delete", *keys))


def _user() -> SimpleNamespace:
    return SimpleNamespace(id="user-1")


@pytest.mark.asyncio
async def test_retry_generation_requeues_same_row_without_rebuilding_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[dict[str, Any], str]] = []

    async def fake_publish_queued(payload: dict[str, Any], message_id: str) -> None:
        published.append((payload, message_id))

    monkeypatch.setattr(tasks, "_publish_queued", fake_publish_queued)

    upstream_request = {
        "fast": True,
        "render_quality": "high",
        "output_format": "webp",
        "output_compression": 95,
        "background": "auto",
        "moderation": "low",
    }
    old_time = datetime(2026, 4, 28, tzinfo=timezone.utc)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        message_id="assistant-1",
        status=GenerationStatus.FAILED.value,
        progress_stage=GenerationStage.FINALIZING.value,
        attempt=2,
        error_code="upstream_timeout",
        error_message="timeout",
        started_at=old_time,
        finished_at=old_time,
        prompt="render a wide hero image",
        size_requested="3840x2160",
        aspect_ratio="16:9",
        upstream_request=upstream_request,
    )
    db = _Db([_Result(gen)])

    out = await tasks.retry_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.QUEUED.value}
    assert gen.status == GenerationStatus.QUEUED.value
    assert gen.progress_stage == GenerationStage.QUEUED.value
    assert gen.attempt == 0
    assert gen.error_code is None
    assert gen.error_message is None
    assert gen.started_at is None
    assert gen.finished_at is None

    assert gen.prompt == "render a wide hero image"
    assert gen.size_requested == "3840x2160"
    assert gen.aspect_ratio == "16:9"
    assert gen.upstream_request is upstream_request

    assert db.committed is True
    assert len(db.added) == 1
    assert published == [
        (
            {"task_id": "gen-1", "user_id": "user-1", "kind": "generation"},
            "assistant-1",
        )
    ]


@pytest.mark.asyncio
async def test_cancel_running_generation_keeps_row_active_until_worker_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis(
        {"generation:image_queue:task_provider:gen-1": b"provider-a"}
    )
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status=GenerationStatus.RUNNING.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.RUNNING.value}
    assert gen.status == GenerationStatus.RUNNING.value
    assert gen.finished_at is None
    assert db.committed is True
    assert redis.calls == [("set", "task:gen-1:cancel", "1", 3600)]


@pytest.mark.asyncio
async def test_cancel_queued_generation_marks_terminal_and_clears_queue_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis(
        {"generation:image_queue:task_provider:gen-1": b"provider-a"}
    )
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status=GenerationStatus.QUEUED.value,
        finished_at=None,
    )
    db = _Db([_Result(gen)])

    out = await tasks.cancel_generation(
        "gen-1",
        _user(),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": GenerationStatus.CANCELED.value}
    assert gen.status == GenerationStatus.CANCELED.value
    assert gen.finished_at is not None
    assert redis.calls == [
        ("set", "task:gen-1:cancel", "1", 3600),
        ("get", "generation:image_queue:task_provider:gen-1"),
        ("zrem", "generation:image_queue:active", "provider-a"),
        ("delete", "generation:image_queue:provider:provider-a"),
        ("delete", "generation:image_queue:task_provider:gen-1"),
        ("delete", "task:gen-1:lease"),
    ]
