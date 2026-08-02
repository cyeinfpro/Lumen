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


async def _billing_disabled(_db: Any) -> bool:
    return False


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
        cancel_requested_at=object(),
        message_id="msg-1",
    )

    class Db:
        def __init__(self) -> None:
            self.statements: list[Any] = []
            self.added: list[Any] = []
            self.committed = False

        async def execute(self, statement: Any) -> _One:
            self.statements.append(statement)
            if "from users" in str(statement).lower():
                return _One("user-1")
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
    monkeypatch.setattr(tasks, "_billing_enabled", _billing_disabled)

    out = await tasks.retry_generation(
        "gen-1",
        SimpleNamespace(id="user-1"),
        db,  # type: ignore[arg-type]
    )

    assert out == {"status": "queued"}
    assert gen.cancel_requested_at is None
    assert redis.deleted == ["task:gen-1:cancel"]
    assert db.committed is True
    active_user_sql = str(
        db.statements[0].compile(dialect=postgresql.dialect())
    ).upper()
    task_sql = str(db.statements[1].compile(dialect=postgresql.dialect())).upper()
    assert "FROM USERS" in active_user_sql
    assert "FOR UPDATE" in active_user_sql
    assert "FOR UPDATE" in task_sql


@pytest.mark.asyncio
async def test_cancel_running_generation_commits_intent_before_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = SimpleNamespace(
        id="gen-1",
        user_id="user-1",
        status="running",
        cancel_requested_at=None,
    )
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

    assert out == {"status": "canceling", "cancel_requested": True}
    assert gen.cancel_requested_at is not None
    assert order == ["commit", "set:task:gen-1:cancel:1:3600"]


@pytest.mark.asyncio
async def test_release_generation_queue_state_removes_matching_epoch_owner() -> None:
    task_id = "gen-1"
    provider = "provider-1"
    provider_key = "generation:image_queue:provider_active:provider-1"
    global_key = "generation:image_queue:active"
    task_provider_key = "generation:image_queue:task_provider:gen-1"
    lease_key = "task:gen-1:lease"
    reservation_key = "generation:image_queue:reservation:gen-1"

    class Redis:
        def __init__(self) -> None:
            self.values = {
                task_provider_key: provider,
                lease_key: "worker:execution:3:attempt:2",
                reservation_key: "reservation-3",
            }
            self.zsets = {
                provider_key: {task_id},
                global_key: {task_id},
            }

        async def get(self, key: str) -> Any:
            return self.values.get(key)

        async def eval(self, _script: str, numkeys: int, *args: Any) -> int:
            keys = args[:numkeys]
            argv = args[numkeys:]
            (
                expected_provider,
                expected_lease,
                expected_reservation,
                expected_task_id,
                active_member,
                dual_race,
            ) = argv
            assert self.values.get(keys[2]) == expected_provider
            assert self.values.get(keys[3]) == expected_lease
            assert self.values.get(keys[4]) == expected_reservation
            if dual_race == "0":
                self.zsets[keys[0]].discard(expected_task_id)
            self.zsets[keys[1]].discard(active_member)
            for key in keys[2:]:
                self.values.pop(key, None)
            return 1

    redis = Redis()
    ownership_token = await tasks.capture_generation_queue_state(
        redis,
        task_id,
        expected_execution_epoch=3,
    )

    released = await tasks._release_generation_queue_state(
        redis,
        task_id,
        expected_execution_epoch=3,
        ownership_token=ownership_token,
    )

    assert released is True
    assert task_id not in redis.zsets[provider_key]
    assert task_id not in redis.zsets[global_key]
    assert task_provider_key not in redis.values
    assert lease_key not in redis.values
    assert reservation_key not in redis.values


@pytest.mark.asyncio
async def test_release_generation_queue_state_cas_preserves_concurrent_retry() -> None:
    task_id = "gen-1"
    provider = "provider-1"
    provider_key = "generation:image_queue:provider_active:provider-1"
    global_key = "generation:image_queue:active"
    task_provider_key = "generation:image_queue:task_provider:gen-1"
    lease_key = "task:gen-1:lease"
    reservation_key = "generation:image_queue:reservation:gen-1"

    class Redis:
        def __init__(self) -> None:
            self.values = {
                task_provider_key: provider,
                reservation_key: "reservation-old",
            }
            self.zsets = {
                provider_key: {task_id: 30.0},
                global_key: {task_id: 30.0},
            }

        async def get(self, key: str) -> Any:
            return self.values.get(key)

        async def eval(self, _script: str, numkeys: int, *args: Any) -> int:
            keys = args[:numkeys]
            argv = args[numkeys:]
            self.values[reservation_key] = "reservation-new"
            self.zsets[provider_key][task_id] = 90.0
            self.zsets[global_key][task_id] = 90.0
            expected_provider, expected_lease, expected_reservation = argv[:3]
            if self.values.get(keys[2]) != expected_provider:
                return 0
            if (self.values.get(keys[3]) or "") != expected_lease:
                return 0
            if (self.values.get(keys[4]) or "") != expected_reservation:
                return 0
            raise AssertionError("stale cleanup must not pass the ownership CAS")

    redis = Redis()
    ownership_token = await tasks.capture_generation_queue_state(
        redis,
        task_id,
        expected_execution_epoch=3,
    )

    released = await tasks._release_generation_queue_state(
        redis,
        task_id,
        expected_execution_epoch=3,
        ownership_token=ownership_token,
    )

    assert released is False
    assert redis.values[task_provider_key] == provider
    assert lease_key not in redis.values
    assert redis.values[reservation_key] == "reservation-new"
    assert redis.zsets[provider_key][task_id] == 90.0
    assert redis.zsets[global_key][task_id] == 90.0


@pytest.mark.asyncio
async def test_release_generation_queue_state_fails_closed_without_atomic_cas() -> None:
    class Redis:
        async def get(self, key: str) -> str | None:
            values = {
                "generation:image_queue:task_provider:gen-1": "provider-1",
                "task:gen-1:lease": "worker:execution:2:attempt:1",
                "generation:image_queue:reservation:gen-1": "reservation-2",
            }
            return values.get(key)

    redis = Redis()
    ownership_token = await tasks.capture_generation_queue_state(
        redis,
        "gen-1",
        expected_execution_epoch=2,
    )
    released = await tasks._release_generation_queue_state(
        redis,
        "gen-1",
        expected_execution_epoch=2,
        ownership_token=ownership_token,
    )

    assert released is False


def test_cancel_billing_evidence_is_scoped_to_current_execution() -> None:
    stale_generation = SimpleNamespace(
        execution_epoch=4,
        upstream_request={
            "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
        },
    )
    proven_undelivered = SimpleNamespace(
        execution_epoch=4,
        upstream_request={
            "upstream_dispatch_started_at": "2026-07-30T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 4,
            "upstream_dispatch_delivery": "proven_undelivered",
        },
    )
    stale_completion_usage = SimpleNamespace(
        execution_epoch=5,
        tokens_out=64,
        upstream_request={"completion_usage_execution_epoch": 4},
    )

    assert (
        tasks.generation_cancel_requires_durable_settlement(stale_generation) is False
    )
    assert (
        tasks.generation_cancel_requires_durable_settlement(proven_undelivered) is False
    )
    assert (
        tasks.completion_cancel_requires_durable_settlement(stale_completion_usage)
        is False
    )


@pytest.mark.asyncio
async def test_list_tasks_scopes_generation_and_completion_queries_to_user() -> None:
    db = _CapturingDb()
    user = SimpleNamespace(id="user-1")

    await tasks.list_tasks(
        user=user,
        db=db,
        query=tasks.TaskListQuery(limit=10),
    )

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
    compiled = [
        statement.compile(dialect=postgresql.dialect()) for statement in db.statements
    ]
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
