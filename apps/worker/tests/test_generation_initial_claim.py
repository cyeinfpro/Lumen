from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.tasks.generation_parts import runner as generation_runner
from app.tasks.generation_parts.default_runtime import build_generation_runtime


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


class _SessionStore:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def session(self) -> _Session:
        return self._session


class _UnexpectedProvider:
    async def resolve_primary_route(self) -> str:
        raise AssertionError("initial claim miss must not touch runtime resources")


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
    caplog: pytest.LogCaptureFixture,
    existence_probe: str | None,
    expected_level: int,
    expected_log: str,
    unexpected_log: str,
) -> None:
    session = _Session([None, existence_probe])
    default_runtime = build_generation_runtime()
    runtime = replace(
        default_runtime,
        deps=replace(
            default_runtime.deps,
            store=_SessionStore(session),
            provider=_UnexpectedProvider(),
        ),
    )

    redis = object()
    with caplog.at_level(logging.INFO, logger=generation_runner.logger.name):
        await runtime.run(
            {"redis": redis, "worker_id": "worker-test"},
            "gen-1",
        )

    assert len(session.statements) == 2
    assert "FOR UPDATE SKIP LOCKED" in _render(session.statements[0])
    assert "FOR UPDATE" not in _render(session.statements[1])
    assert expected_log in caplog.text
    assert unexpected_log not in caplog.text
    assert any(
        record.levelno == expected_level and record.getMessage() == expected_log
        for record in caplog.records
    )
