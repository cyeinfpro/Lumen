from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.tasks import state
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


@pytest.mark.asyncio
async def test_mark_task_failed_flushes_balance_cache_after_external_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.info = {"lumen_post_commit_balance_cache": {"user-1": 123}}
            self.commits = 0
            self.rollbacks = 0

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            self.rollbacks += 1

    task = SimpleNamespace(status="running", error_code=None, error_message=None, finished_at=None)
    session = Session()
    calls: list[tuple[int, dict[str, object]]] = []

    async def flush_balance_cache_refreshes(flush_session) -> None:
        calls.append((flush_session.commits, dict(flush_session.info)))
        flush_session.info.clear()

    monkeypatch.setattr(
        state.worker_billing,
        "flush_balance_cache_refreshes",
        flush_balance_cache_refreshes,
    )

    await mark_task_failed(
        task,
        error_code="timeout",
        error_message="stuck",
        session=session,
    )

    assert session.commits == 1
    assert session.rollbacks == 0
    assert calls == [(1, {"lumen_post_commit_balance_cache": {"user-1": 123}})]
    assert session.info == {}


@pytest.mark.asyncio
async def test_mark_task_failed_flushes_balance_cache_after_self_managed_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Task:
        __table__ = object()

        def __init__(self, task_id: str) -> None:
            self.id = task_id
            self.status = "running"
            self.error_code = None
            self.error_message = None
            self.finished_at = None

    class Session:
        def __init__(self, db_task: Task) -> None:
            self.db_task = db_task
            self.info = {"lumen_post_commit_balance_cache": {"user-1": 456}}
            self.commits = 0
            self.rollbacks = 0

        async def get(self, task_type, task_id: str):
            assert task_type is Task
            assert task_id == self.db_task.id
            return self.db_task

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            self.rollbacks += 1

    db_task = Task("task-1")
    session = Session(db_task)
    calls: list[tuple[int, dict[str, object]]] = []

    async def flush_balance_cache_refreshes(flush_session) -> None:
        calls.append((flush_session.commits, dict(flush_session.info)))
        flush_session.info.clear()

    @asynccontextmanager
    async def session_factory():
        yield session

    monkeypatch.setattr(
        state.worker_billing,
        "flush_balance_cache_refreshes",
        flush_balance_cache_refreshes,
    )

    task = Task("task-1")
    await mark_task_failed(
        task,
        error_code="timeout",
        error_message="stuck",
        session_factory=session_factory,
    )

    assert session.commits == 1
    assert session.rollbacks == 0
    assert db_task.status == "failed"
    assert db_task.error_code == "timeout"
    assert calls == [(1, {"lumen_post_commit_balance_cache": {"user-1": 456}})]
    assert session.info == {}
