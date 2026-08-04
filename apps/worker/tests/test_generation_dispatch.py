from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from arq.connections import job_key_prefix, result_key_prefix
from arq.constants import in_progress_key_prefix

from app.tasks import generation as generation_task
from app import generation_dispatch as dispatch
from app.tasks.generation_parts import runner_dispatch_phase
from app.tasks.generation_parts.runtime import GenerationRuntime
from lumen_core.providers import ProviderProxyDefinition
from lumen_core.providers_parts import proxy_runtime
from lumen_core.upstream_billing import mark_upstream_dispatch_started


class DispatchRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.enqueued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.active_job_ids: set[str] = set()
        self.result_job_ids: set[str] = set()
        self.in_progress_job_ids: set[str] = set()
        self.fail_before_write = False
        self.accept_then_raise = False
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def exists(self, *keys: str) -> int:
        found = 0
        for key in keys:
            if key.startswith(job_key_prefix):
                found += key.removeprefix(job_key_prefix) in self.active_job_ids
            elif key.startswith(result_key_prefix):
                found += key.removeprefix(result_key_prefix) in self.result_job_ids
            elif key.startswith(in_progress_key_prefix):
                found += (
                    key.removeprefix(in_progress_key_prefix) in self.in_progress_job_ids
                )
        return found

    async def enqueue_job(
        self,
        name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if self.fail_before_write:
            raise ConnectionError("enqueue failed before write")
        job_id = kwargs["_job_id"]
        if job_id in self.active_job_ids or job_id in self.result_job_ids:
            return None
        self.enqueued.append((name, args, kwargs))
        self.active_job_ids.add(job_id)
        job = SimpleNamespace(job_id=job_id)
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
async def test_failed_before_write_retry_reuses_reserved_dispatch() -> None:
    redis = DispatchRedis()
    redis.fail_before_write = True

    with pytest.raises(ConnectionError, match="before write"):
        await dispatch.enqueue_generation_dispatch(
            redis,
            task_id="gen-before-write",
            attempt=1,
        )

    active_key = dispatch.dispatch_active_key("gen-before-write")
    assert redis.values[active_key] == "1|1|reserved|"
    assert redis.enqueued == []

    redis.fail_before_write = False
    retry = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-before-write",
        attempt=1,
    )

    assert retry.created is False
    assert retry.enqueued is True
    assert retry.accepted is True
    assert retry.identity.revision == 1
    assert len(redis.enqueued) == 1
    assert redis.values[active_key].startswith("1|1|enqueued|")


@pytest.mark.asyncio
async def test_accepted_then_exception_uses_arq_evidence_without_duplicate() -> None:
    redis = DispatchRedis()
    redis.accept_then_raise = True

    first = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-unknown",
        attempt=1,
    )

    assert first.created is True
    assert first.enqueued is False
    assert first.accepted is True
    assert len(redis.enqueued) == 1
    assert first.identity.job_id in redis.active_job_ids
    assert redis.values[dispatch.dispatch_active_key("gen-unknown")].startswith(
        "1|1|enqueued|"
    )

    redis.accept_then_raise = False
    retry = await dispatch.enqueue_generation_dispatch(
        redis,
        task_id="gen-unknown",
        attempt=1,
    )

    assert retry.created is False
    assert retry.enqueued is False
    assert retry.accepted is True
    assert len(redis.enqueued) == 1


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


@pytest.mark.asyncio
async def test_generation_proxy_initialization_failure_precedes_dispatch_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool]] = []
    http_calls = 0
    proxy = ProviderProxyDefinition(
        name="broken-ssh",
        protocol="ssh",
        host="proxy.invalid",
        port=22,
        username="worker",
        known_hosts_path="/missing/known_hosts",
    )
    proxy_state = proxy_runtime.ProviderProxyRuntime()
    state = SimpleNamespace(
        task_deadline=asyncio.get_running_loop().time() + 5,
        task_id="gen-proxy-init",
        attempt=1,
        generation=SimpleNamespace(execution_epoch=0),
        gen_upstream_request_snapshot={},
        image_iter=None,
        lease_lost=asyncio.Event(),
        redis=object(),
        action="generate",
        inpaint_size_override=None,
        resolved=SimpleNamespace(size="1024x1024"),
        reserved_provider_name="proxy-provider",
    )

    async def not_cancelled(*_args: object, **_kwargs: object) -> bool:
        return False

    async def active_user(_state: object) -> None:
        return None

    async def record_marker(
        marker_state: Any,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        fence_active_user: bool = False,
    ) -> None:
        _ = fence_active_user
        markers.append((response_received, proven_undelivered))
        if not response_received and not proven_undelivered:
            marker_state.gen_upstream_request_snapshot = mark_upstream_dispatch_started(
                marker_state.gen_upstream_request_snapshot,
                at="2026-08-03T00:00:00+00:00",
                attempt=marker_state.attempt,
                execution_epoch=0,
            )

    async def fail_proxy_start(
        _runtime: proxy_runtime.ProviderProxyRuntime,
        _proxy: ProviderProxyDefinition,
    ) -> str:
        raise RuntimeError("local SSH proxy initialization failed")

    async def image_iter():
        nonlocal http_calls
        await proxy_runtime.resolve_provider_proxy_url(
            proxy,
            runtime=proxy_state,
        )
        http_calls += 1
        yield ("never", None)

    monkeypatch.setattr(runner_dispatch_phase, "is_cancelled", not_cancelled)
    monkeypatch.setattr(
        runner_dispatch_phase,
        "_ensure_generation_user_active",
        active_user,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_generation_upstream_marker",
        record_marker,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "build_image_iterator",
        lambda _state: image_iter(),
    )
    monkeypatch.setattr(
        proxy_runtime,
        "_ensure_ssh_socks_proxy",
        fail_proxy_start,
    )

    with pytest.raises(RuntimeError, match="local SSH proxy initialization failed"):
        await runner_dispatch_phase.dispatch_upstream_request(state)

    assert http_calls == 0
    assert markers == []
