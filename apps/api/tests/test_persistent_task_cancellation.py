from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.routes import tasks
from lumen_core.constants import CompletionStatus, GenerationStatus


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Db:
    def __init__(self, row: Any) -> None:
        self.row = row
        self.commit_count = 0

    async def execute(self, _statement: Any) -> _Result:
        return _Result(self.row)

    async def commit(self) -> None:
        self.commit_count += 1


class _UnavailableRedis:
    async def set(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("redis unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "status"),
    [
        (tasks.cancel_generation, GenerationStatus.RUNNING.value),
        (tasks.cancel_completion, CompletionStatus.STREAMING.value),
    ],
)
async def test_active_cancel_is_accepted_when_redis_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    route: Any,
    status: str,
) -> None:
    row = SimpleNamespace(
        id="task-1",
        user_id="user-1",
        status=status,
        cancel_requested_at=None,
    )
    db = _Db(row)
    monkeypatch.setattr(tasks, "get_redis", lambda: _UnavailableRedis())

    out = await route(
        "task-1",
        SimpleNamespace(id="user-1"),
        db,
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert isinstance(row.cancel_requested_at, datetime)
    assert row.cancel_requested_at.tzinfo == timezone.utc
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_repeated_active_cancel_preserves_first_intent_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_at = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status=GenerationStatus.RUNNING.value,
        cancel_requested_at=requested_at,
    )
    db = _Db(row)

    class Redis:
        async def set(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(tasks, "get_redis", lambda: Redis())

    out = await tasks.cancel_generation(
        "gen-1",
        SimpleNamespace(id="user-1"),
        db,
    )

    assert out == {"status": "canceling", "cancel_requested": True}
    assert row.cancel_requested_at is requested_at
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_terminal_canceled_request_is_idempotent_without_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        id="comp-1",
        user_id="user-1",
        status=CompletionStatus.CANCELED.value,
    )
    db = _Db(row)

    def unexpected_redis() -> Any:
        raise AssertionError("terminal idempotency must not require Redis")

    monkeypatch.setattr(tasks, "get_redis", unexpected_redis)

    out = await tasks.cancel_completion(
        "comp-1",
        SimpleNamespace(id="user-1"),
        db,
    )

    assert out == {
        "status": CompletionStatus.CANCELED.value,
        "cancel_requested": True,
    }
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_terminal_success_wins_cancel_race_under_row_lock() -> None:
    row = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status=GenerationStatus.SUCCEEDED.value,
    )

    with pytest.raises(Exception) as exc_info:
        await tasks.cancel_generation(
            "gen-1",
            SimpleNamespace(id="user-1"),
            _Db(row),
        )

    assert getattr(exc_info.value, "status_code", None) == 409
