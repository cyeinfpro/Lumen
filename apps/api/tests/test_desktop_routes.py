from __future__ import annotations

from typing import Any

import pytest

from lumen_core.constants import CompletionStatus, GenerationStatus


class _ScalarDb:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.params: list[dict[str, Any]] = []

    async def scalar(self, stmt: Any) -> int:
        compiled = stmt.compile()
        self.params.append(dict(compiled.params))
        if "sqlite_master" in str(compiled):
            return 2
        return self.values.pop(0)


@pytest.mark.asyncio
async def test_desktop_activity_counts_running_generation_and_streaming_completion() -> None:
    from app.routes.desktop import desktop_activity
    from fastapi import Response

    db = _ScalarDb([2, 1])
    response = Response()
    out = await desktop_activity(db, response)  # type: ignore[arg-type]

    assert response.headers["Cache-Control"] == "no-store"
    assert out.active is True
    assert out.active_tasks == 3
    assert out.generation_running == 2
    assert out.completion_streaming == 1
    assert GenerationStatus.RUNNING.value in db.params[1].values()
    assert CompletionStatus.STREAMING.value in db.params[2].values()


@pytest.mark.asyncio
async def test_desktop_activity_is_inactive_when_no_work_is_running() -> None:
    from app.routes.desktop import desktop_activity
    from fastapi import Response

    out = await desktop_activity(_ScalarDb([0, 0]), Response())  # type: ignore[arg-type]

    assert out.active is False
    assert out.active_tasks == 0


class _MissingTablesDb:
    async def scalar(self, stmt: Any) -> int:
        return 0


@pytest.mark.asyncio
async def test_desktop_activity_returns_zero_before_tables_exist() -> None:
    from app.routes.desktop import desktop_activity
    from fastapi import Response

    out = await desktop_activity(_MissingTablesDb(), Response())  # type: ignore[arg-type]

    assert out.active is False
    assert out.generation_running == 0
    assert out.completion_streaming == 0
