from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.tasks import generation as generation_task
from app import generation_dispatch as dispatch
from app.tasks.generation_parts.runtime import GenerationRuntime


class DispatchRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.accept_then_raise = False
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def enqueue_job(
        self,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.enqueued.append((name, args, kwargs))
        job = SimpleNamespace(job_id=kwargs["_job_id"])
        if self.accept_then_raise:
            raise TimeoutError("enqueue result unknown")
        return job

    async def eval(
        self,
        script: str,
        _key_count: int,
        *args: str,
    ) -> Any:
        async with self._lock:
            if script == dispatch.BEGIN_DISPATCH_LUA:
                return self._begin(*args)
            if script == dispatch.MARK_DISPATCH_ENQUEUED_LUA:
                return self._mark_enqueued(*args)
            if script == dispatch.CONSUME_DISPATCH_LUA:
                return self._consume(*args)
            if script == dispatch.FINISH_DISPATCH_LUA:
                return self._finish(*args)
        raise AssertionError("unknown dispatch script")

    def _begin(
        self,
        active_key: str,
        revision_key: str,
        attempt_raw: str,
        _ttl: str,
        _revision_ttl: str,
        replace_value: str,
    ) -> list[Any]:
        attempt = int(attempt_raw)
        current = self.values.get(active_key)
        if current is not None:
            current_attempt = int(current.split("|", 1)[0])
            if current_attempt > attempt:
                return [0, current]
            if current_attempt == attempt and current != replace_value:
                return [0, current]
        revision = self.counters.get(revision_key, 0) + 1
        self.counters[revision_key] = revision
        value = f"{attempt}|{revision}|reserved|"
        self.values[active_key] = value
        return [1, value]

    def _mark_enqueued(
        self,
        active_key: str,
        reserved_value: str,
        enqueued_value: str,
        _ttl: str,
    ) -> int:
        if self.values.get(active_key) != reserved_value:
            return 0
        self.values[active_key] = enqueued_value
        return 1

    def _consume(
        self,
        active_key: str,
        prefix: str,
        worker_id: str,
        _ttl: str,
    ) -> int:
        current = self.values.get(active_key)
        if current is None or not current.startswith(prefix):
            return 0
        phase = current.split("|", 3)[2]
        if phase not in {"reserved", "enqueued"}:
            return 0
        self.values[active_key] = f"{prefix}consumed|{worker_id}"
        return 1

    def _finish(self, active_key: str, prefix: str) -> int:
        current = self.values.get(active_key)
        if current is None or not current.startswith(prefix):
            return 0
        del self.values[active_key]
        return 1


@pytest.mark.asyncio
async def test_concurrent_kickers_create_one_active_dispatch_revision() -> None:
    redis = DispatchRedis()

    first, second = await asyncio.gather(
        dispatch.enqueue_generation_dispatch(
            redis,
            task_id="gen-1",
            attempt=1,
        ),
        dispatch.enqueue_generation_dispatch(
            redis,
            task_id="gen-1",
            attempt=1,
        ),
    )

    assert sorted((first.created, second.created)) == [False, True]
    assert first.identity == second.identity
    assert len(redis.enqueued) == 1
    assert redis.enqueued[0][1] == ("gen-1", 1, 1)
    assert redis.enqueued[0][2]["_job_id"] == first.identity.job_id


@pytest.mark.asyncio
async def test_unknown_enqueue_result_keeps_active_revision() -> None:
    redis = DispatchRedis()
    redis.accept_then_raise = True

    with pytest.raises(TimeoutError, match="unknown"):
        await dispatch.enqueue_generation_dispatch(
            redis,
            task_id="gen-unknown",
            attempt=1,
        )
    redis.accept_then_raise = False

    retry = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-unknown",
        attempt=1,
    )

    assert retry.created is False
    assert len(redis.enqueued) == 1
    assert redis.values[dispatch.dispatch_active_key("gen-unknown")] == "1|1|reserved|"


@pytest.mark.asyncio
async def test_provider_wait_supersedes_consumed_revision() -> None:
    redis = DispatchRedis()
    first = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-wait",
        attempt=1,
    )
    assert await dispatch.consume_generation_dispatch(
        redis,
        first.identity,
        worker_id="worker-1",
    )

    second = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-wait",
        attempt=1,
        replace=first.identity,
        defer_by=30,
    )

    assert second.created is True
    assert second.identity.revision == 2
    assert len(redis.enqueued) == 2
    assert await dispatch.finish_generation_dispatch(redis, first.identity) is False


@pytest.mark.asyncio
async def test_old_revision_cannot_consume_after_new_revision() -> None:
    redis = DispatchRedis()
    first = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-stale",
        attempt=1,
    )
    assert await dispatch.consume_generation_dispatch(
        redis,
        first.identity,
        worker_id="worker-1",
    )
    second = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-stale",
        attempt=1,
        replace=first.identity,
    )

    assert (
        await dispatch.consume_generation_dispatch(
            redis,
            first.identity,
            worker_id="late-worker",
        )
        is False
    )
    assert (
        await dispatch.consume_generation_dispatch(
            redis,
            second.identity,
            worker_id="worker-2",
        )
        is True
    )


@pytest.mark.asyncio
async def test_arq_entry_consumes_and_finishes_dispatch() -> None:
    redis = DispatchRedis()
    begun = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-entry",
        attempt=1,
    )
    seen: list[tuple[dict[str, Any], str]] = []

    async def runner(ctx: dict[str, Any], task_id: str, _services: Any) -> None:
        seen.append((ctx, task_id))
        assert ctx[dispatch.DISPATCH_CONTEXT_KEY] == begun.identity

    runtime = GenerationRuntime(deps=SimpleNamespace(), runner=runner)
    ctx = {
        "redis": redis,
        "worker_id": "worker-entry",
        "generation_runtime": runtime,
    }

    await generation_task.run_generation(ctx, "gen-entry", 1, 1)

    assert [task_id for _ctx, task_id in seen] == ["gen-entry"]
    assert dispatch.DISPATCH_CONTEXT_KEY not in ctx
    assert dispatch.dispatch_active_key("gen-entry") not in redis.values


@pytest.mark.asyncio
async def test_arq_entry_rejects_late_dispatch_without_running() -> None:
    redis = DispatchRedis()
    first = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-late",
        attempt=1,
    )
    assert await dispatch.consume_generation_dispatch(
        redis,
        first.identity,
        worker_id="worker-1",
    )
    second = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-late",
        attempt=1,
        replace=first.identity,
    )

    async def fail_runner(*_args: Any) -> None:
        raise AssertionError("late dispatch must not enter generation runtime")

    runtime = GenerationRuntime(deps=SimpleNamespace(), runner=fail_runner)
    ctx = {
        "redis": redis,
        "worker_id": "late-worker",
        "generation_runtime": runtime,
    }

    await generation_task.run_generation(
        ctx,
        "gen-late",
        first.identity.attempt,
        first.identity.revision,
    )

    assert redis.values[dispatch.dispatch_active_key("gen-late")].startswith(
        second.identity.value_prefix
    )
