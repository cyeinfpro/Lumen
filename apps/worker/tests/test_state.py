from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.tasks.state import (
    is_completion_terminal,
    is_generation_terminal,
    mark_task_failed,
)


def test_terminal_helpers_use_string_statuses() -> None:
    assert is_generation_terminal("succeeded")
    assert is_generation_terminal("failed")
    assert is_generation_terminal("canceled")
    assert not is_generation_terminal("running")
    assert is_completion_terminal("succeeded")
    assert is_completion_terminal("failed")
    assert is_completion_terminal("canceled")
    assert not is_completion_terminal("streaming")


@pytest.mark.asyncio
async def test_mark_task_failed_sets_common_failure_fields() -> None:
    task = SimpleNamespace(status="running", error_code=None, error_message=None, finished_at=None)

    await mark_task_failed(task, error_code="timeout", error_message="stuck")

    assert task.status == "failed"
    assert task.error_code == "timeout"
    assert task.error_message == "stuck"
    assert task.finished_at is not None
