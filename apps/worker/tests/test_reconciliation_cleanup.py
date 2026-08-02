from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.reconciliation.cleanup import (
    DUAL_RACE_SENTINEL_PREFIX,
    IMAGE_QUEUE_ACTIVE_KEY,
    IMAGE_QUEUE_TASK_PROVIDER_PREFIX,
    cleanup_terminal_sentinels,
)


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Session:
    def __init__(self, generation: Any) -> None:
        self.generation = generation
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self.generation)


class _Redis:
    def __init__(self, *, task_id: str, provider: str | None) -> None:
        self.task_id = task_id
        self.sentinel = f"{DUAL_RACE_SENTINEL_PREFIX}{task_id}"
        self.provider = provider
        self.active = {self.sentinel}
        self.lease = "lease-token"
        self.reservation = "reservation-token"
        self.eval_calls = 0

    async def zrange(self, key: str, _start: int, _end: int) -> list[str]:
        assert key == IMAGE_QUEUE_ACTIVE_KEY
        return list(self.active)

    async def eval(
        self,
        _script: str,
        key_count: int,
        active_key: str,
        provider_key: str,
        lease_key: str,
        reservation_key: str,
        expected_sentinel: str,
    ) -> int:
        self.eval_calls += 1
        assert key_count == 4
        assert active_key == IMAGE_QUEUE_ACTIVE_KEY
        assert provider_key == f"{IMAGE_QUEUE_TASK_PROVIDER_PREFIX}{self.task_id}"
        assert lease_key == f"task:{self.task_id}:lease"
        assert reservation_key == f"generation:image_queue:reservation:{self.task_id}"
        if self.provider is not None and self.provider != expected_sentinel:
            return 0
        self.active.discard(expected_sentinel)
        if self.provider == expected_sentinel:
            self.provider = None
        self.lease = None
        self.reservation = None
        return 1


def _session_factory(session: _Session) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        yield session

    return factory


@pytest.mark.asyncio
async def test_cleanup_rechecks_terminal_state_under_row_lock() -> None:
    task_id = "generation-1"
    redis = _Redis(
        task_id=task_id,
        provider=f"{DUAL_RACE_SENTINEL_PREFIX}{task_id}",
    )
    session = _Session(SimpleNamespace(id=task_id, status="running"))

    await cleanup_terminal_sentinels(
        redis,
        session_factory=_session_factory(session),
    )

    assert redis.eval_calls == 0
    assert redis.sentinel in redis.active
    sql = str(session.statements[0])
    assert "FOR UPDATE" in sql


@pytest.mark.asyncio
async def test_cleanup_keeps_new_queue_owner_after_terminal_snapshot() -> None:
    task_id = "generation-2"
    redis = _Redis(task_id=task_id, provider="new-provider")
    session = _Session(SimpleNamespace(id=task_id, status="failed"))

    await cleanup_terminal_sentinels(
        redis,
        session_factory=_session_factory(session),
    )

    assert redis.eval_calls == 1
    assert redis.sentinel in redis.active
    assert redis.provider == "new-provider"
    assert redis.lease == "lease-token"
    assert redis.reservation == "reservation-token"


@pytest.mark.asyncio
async def test_cleanup_atomically_removes_terminal_sentinel_state() -> None:
    task_id = "generation-3"
    sentinel = f"{DUAL_RACE_SENTINEL_PREFIX}{task_id}"
    redis = _Redis(task_id=task_id, provider=sentinel)
    session = _Session(SimpleNamespace(id=task_id, status="succeeded"))

    await cleanup_terminal_sentinels(
        redis,
        session_factory=_session_factory(session),
    )

    assert redis.eval_calls == 1
    assert sentinel not in redis.active
    assert redis.provider is None
    assert redis.lease is None
    assert redis.reservation is None
