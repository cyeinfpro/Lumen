from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.routes import tasks


class _EmptyScalars:
    def scalars(self) -> "_EmptyScalars":
        return self

    def all(self) -> list[Any]:
        return []


class _CapturingDb:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _EmptyScalars:
        self.statements.append(statement)
        return _EmptyScalars()


@pytest.mark.asyncio
async def test_list_tasks_scopes_generation_and_completion_queries_to_user() -> None:
    db = _CapturingDb()
    user = SimpleNamespace(id="user-1")

    await tasks.list_tasks(user=user, db=db, limit=10)

    rendered = [str(statement) for statement in db.statements]
    assert len(rendered) == 2
    assert "generations.user_id" in rendered[0]
    assert "completions.user_id" in rendered[1]


@pytest.mark.asyncio
async def test_list_my_active_tasks_scopes_queries_to_user() -> None:
    db = _CapturingDb()
    user = SimpleNamespace(id="user-1")

    await tasks.list_my_active_tasks(user=user, db=db, limit=25)

    rendered = [str(statement) for statement in db.statements]
    assert len(rendered) == 2
    assert "generations.user_id" in rendered[0]
    assert "completions.user_id" in rendered[1]
    compiled = [statement.compile(dialect=postgresql.dialect()) for statement in db.statements]
    assert all(" LIMIT " in str(statement).upper() for statement in compiled)
    assert all(25 in statement.params.values() for statement in compiled)


@pytest.mark.asyncio
async def test_publish_queued_failure_is_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Counter:
        def __init__(self) -> None:
            self.labels_seen: list[dict[str, str]] = []
            self.count = 0

        def labels(self, **kwargs: str) -> "Counter":
            self.labels_seen.append(kwargs)
            return self

        def inc(self) -> None:
            self.count += 1

    async def fail_pool() -> None:
        raise RuntimeError("arq unavailable")

    counter = Counter()
    monkeypatch.setattr(tasks, "task_publish_errors_total", counter)
    monkeypatch.setattr(tasks, "get_redis", lambda: object())
    monkeypatch.setattr(tasks, "get_arq_pool", fail_pool)

    await tasks._publish_queued(
        {"task_id": "task-1", "user_id": "user-1", "kind": "generation"},
        "message-1",
    )

    assert counter.count == 1
    assert counter.labels_seen == [{"kind": "generation"}]
