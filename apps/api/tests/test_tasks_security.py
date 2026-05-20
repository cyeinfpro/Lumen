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


class _One:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


@pytest.mark.asyncio
async def test_retry_generation_locks_row_and_clears_cancel_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status="canceled",
        progress_stage="canceled",
        attempt=1,
        error_code="cancelled",
        error_message="cancelled",
        started_at=object(),
        finished_at=object(),
        message_id="msg-1",
    )

    class Db:
        def __init__(self) -> None:
            self.statements: list[Any] = []
            self.added: list[Any] = []
            self.committed = False

        async def execute(self, statement: Any) -> _One:
            self.statements.append(statement)
            return _One(gen)

        def add(self, row: Any) -> None:
            self.added.append(row)

        async def flush(self) -> None:
            for item in self.added:
                if getattr(item, "id", None) is None:
                    item.id = "outbox-1"

        async def commit(self) -> None:
            self.committed = True

    class Redis:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def delete(self, key: str) -> None:
            self.deleted.append(key)

    async def noop_publish(_payload: dict, _message_id: str) -> None:
        return None

    redis = Redis()
    db = Db()
    monkeypatch.setattr(tasks, "get_redis", lambda: redis)
    monkeypatch.setattr(tasks, "_publish_queued", noop_publish)

    out = await tasks.retry_generation(
        "gen-1",
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "queued"}
    assert redis.deleted == ["task:gen-1:cancel"]
    assert db.committed is True
    assert "FOR UPDATE" in str(
        db.statements[0].compile(dialect=postgresql.dialect())
    ).upper()


@pytest.mark.asyncio
async def test_cancel_running_generation_sets_cancel_without_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUNNING-branch contract: write redis cancel flag, do NOT commit.

    The branch makes no field mutation on the generation row, so an
    explicit commit would just round-trip without releasing any work — the
    SELECT FOR UPDATE row lock is released by the FastAPI session
    context manager at request exit. This test pins the contract so a
    future regression that re-adds the commit is caught.
    """
    gen = SimpleNamespace(id="gen-1", user_id="user-1", status="running")
    order: list[str] = []

    class Db:
        async def execute(self, _statement: Any) -> _One:
            return _One(gen)

        async def commit(self) -> None:
            order.append("commit")

    class Redis:
        async def set(self, key: str, value: str, *, ex: int) -> None:
            order.append(f"set:{key}:{value}:{ex}")

    monkeypatch.setattr(tasks, "get_redis", lambda: Redis())

    out = await tasks.cancel_generation(
        "gen-1",
        SimpleNamespace(id="user-1"),
        Db(),  # type: ignore[arg-type]
    )

    assert out == {"status": "running"}
    assert order == ["set:task:gen-1:cancel:1:3600"]


@pytest.mark.asyncio
async def test_release_generation_queue_state_removes_task_members() -> None:
    class Pipe:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def zrem(self, key: str, member: str) -> None:
            self.calls.append(("zrem", key, member))

        def delete(self, key: str) -> None:
            self.calls.append(("delete", key))

        async def execute(self) -> None:
            self.calls.append(("execute",))

    class Redis:
        def __init__(self) -> None:
            self.pipe = Pipe()

        async def get(self, key: str) -> str:
            assert key == "generation:image_queue:task_provider:gen-1"
            return "provider-1"

        def pipeline(self, *, transaction: bool = False) -> Pipe:
            assert transaction is False
            return self.pipe

    redis = Redis()

    await tasks._release_generation_queue_state(redis, "gen-1")

    assert ("zrem", "generation:image_queue:active", "gen-1") in redis.pipe.calls
    assert (
        "zrem",
        "generation:image_queue:provider_active:provider-1",
        "gen-1",
    ) in redis.pipe.calls
    assert ("delete", "generation:image_queue:task_provider:gen-1") in redis.pipe.calls
    assert ("delete", "task:gen-1:lease") in redis.pipe.calls


@pytest.mark.asyncio
async def test_release_generation_queue_state_without_pipeline() -> None:
    class Redis:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def get(self, key: str) -> str:
            self.calls.append(("get", key))
            return "provider-1"

        async def zrem(self, key: str, member: str) -> None:
            self.calls.append(("zrem", key, member))

        async def delete(self, key: str) -> None:
            self.calls.append(("delete", key))

    redis = Redis()

    await tasks._release_generation_queue_state(redis, "gen-1")

    assert (
        "zrem",
        "generation:image_queue:provider_active:provider-1",
        "gen-1",
    ) in redis.calls
    assert ("delete", "task:gen-1:lease") in redis.calls


@pytest.mark.asyncio
async def test_list_tasks_scopes_generation_and_completion_queries_to_user() -> None:
    db = _CapturingDb()
    user = SimpleNamespace(id="user-1")

    await tasks.list_tasks(user=user, db=db, limit=10)

    rendered = [str(statement) for statement in db.statements]
    assert len(rendered) == 3
    assert "generations.user_id" in rendered[0]
    assert "completions.user_id" in rendered[1]
    assert "generations.user_id" in rendered[2]


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
    assert all(50 in statement.params.values() for statement in compiled)


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
