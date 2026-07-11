from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app import provider_pool
from app.tasks import generation


class _Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _Session:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.results.pop(0))

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.attached: list[Any] = []

    def attach_redis(self, redis: Any) -> None:
        self.attached.append(redis)


def _render(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existence_probe", "expected_level", "expected_log", "unexpected_log"),
    [
        (
            "gen-1",
            logging.INFO,
            "generation initial claim skipped locked row task_id=gen-1",
            "generation not found task_id=gen-1",
        ),
        (
            None,
            logging.WARNING,
            "generation not found task_id=gen-1",
            "generation initial claim skipped locked row task_id=gen-1",
        ),
    ],
)
async def test_initial_claim_distinguishes_locked_row_from_missing_task(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    existence_probe: str | None,
    expected_level: int,
    expected_log: str,
    unexpected_log: str,
) -> None:
    session = _Session([None, existence_probe])
    pool = _Pool()

    async def get_pool() -> _Pool:
        return pool

    async def unexpected_runtime_resource_call(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("initial claim miss must not touch runtime resources")

    monkeypatch.setattr(generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(provider_pool, "get_pool", get_pool)
    monkeypatch.setattr(
        generation,
        "_reserve_image_queue_slot",
        unexpected_runtime_resource_call,
    )
    monkeypatch.setattr(
        generation,
        "_acquire_lease",
        unexpected_runtime_resource_call,
    )
    monkeypatch.setattr(
        generation,
        "_release_image_queue_slot",
        unexpected_runtime_resource_call,
    )
    monkeypatch.setattr(
        generation,
        "_release_lease",
        unexpected_runtime_resource_call,
    )

    redis = object()
    with caplog.at_level(logging.INFO, logger=generation.logger.name):
        await generation.run_generation(
            {"redis": redis, "worker_id": "worker-test"},
            "gen-1",
        )

    assert pool.attached == []
    assert len(session.statements) == 2
    assert "FOR UPDATE SKIP LOCKED" in _render(session.statements[0])
    assert "FOR UPDATE" not in _render(session.statements[1])
    assert expected_log in caplog.text
    assert unexpected_log not in caplog.text
    assert any(
        record.levelno == expected_level and record.getMessage() == expected_log
        for record in caplog.records
    )
