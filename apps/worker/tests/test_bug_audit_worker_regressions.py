from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from app import account_limiter, sse_publish, upstream
from app.provider_pool import ProviderConfig, ProviderPool
from app.tasks import completion, generation, memory_extraction


def test_completion_charge_uses_same_session_before_success_commit() -> None:
    source = inspect.getsource(completion.run_completion)
    charge_call = "await worker_billing.charge_completion(session, comp_for_billing)"

    charge_idx = source.index(charge_call)
    commit_idx = source.index("await session.commit()", charge_idx)
    between = source[charge_idx:commit_idx]

    assert charge_idx < commit_idx
    assert "SessionLocal" not in between
    assert "async with" not in between


def test_completion_rechecks_cancel_after_billing_charge_before_commit() -> None:
    source = inspect.getsource(completion.run_completion)
    charge_idx = source.index(
        "await worker_billing.charge_completion(session, comp_for_billing)"
    )
    cancel_idx = source.index(
        'await _raise_if_completion_cancelled(\n                    redis,\n                    task_id,\n                    "cancelled before success commit"',
        charge_idx,
    )
    commit_idx = source.index("await session.commit()", charge_idx)

    assert charge_idx < cancel_idx < commit_idx


def test_completion_tool_limit_continues_with_tool_choice_none() -> None:
    body = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [{"type": "web_search_preview"}],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }

    fallback = completion._tool_limited_completion_body(body)  # noqa: SLF001

    assert fallback is not body
    assert fallback["tool_choice"] == "none"
    assert fallback["parallel_tool_calls"] is False
    assert fallback["tools"] == body["tools"]
    assert fallback["input"][:-1] == body["input"]
    assert fallback["input"][-1]["content"][0]["text"] == (
        completion._TOOL_LIMIT_FALLBACK_TEXT  # noqa: SLF001
    )


def test_completion_cancelled_response_uses_cancel_branch() -> None:
    with pytest.raises(completion._TaskCancelled, match="upstream response cancelled"):  # noqa: SLF001
        completion._raise_for_terminal_response_event(  # noqa: SLF001
            "response.cancelled",
            {"id": "resp-1"},
        )


@pytest.mark.asyncio
async def test_completion_checks_cancel_before_billing_commit() -> None:
    class CancelledRedis:
        async def get(self, _key: str) -> str:
            return "1"

    with pytest.raises(completion._TaskCancelled, match="before billing settle"):  # noqa: SLF001
        await completion._raise_if_completion_cancelled(  # noqa: SLF001
            CancelledRedis(),
            "comp-1",
            "cancelled before billing settle",
        )


@pytest.mark.asyncio
async def test_completion_abort_iterator_closes_inner_stream() -> None:
    class HangingStream:
        closed = False

        def __aiter__(self) -> "HangingStream":
            return self

        async def __anext__(self) -> dict[str, Any]:
            await asyncio.sleep(60)
            return {"type": "response.output_text.delta", "delta": "late"}

        async def aclose(self) -> None:
            self.closed = True

    stream = HangingStream()
    cancel_requested = asyncio.Event()
    lease_lost = asyncio.Event()
    cancel_requested.set()

    with pytest.raises(completion._TaskCancelled, match="cancelled during stream"):  # noqa: SLF001
        await completion._next_completion_stream_event(  # noqa: SLF001
            stream,
            cancel_requested=cancel_requested,
            lease_lost=lease_lost,
        )
    assert stream.closed is True


@pytest.mark.asyncio
async def test_completion_tool_image_budget_checks_byok_task_with_wallet_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        async def get(self, _model: Any, _task_id: str) -> Any:
            return type(
                "CompletionRow",
                (),
                {
                    "id": "comp-1",
                    "upstream_request": {"billing_retry_count": 1},
                },
            )()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

    checked_refs: list[str] = []

    async def wallet_billing_applies(*_args: Any, **kwargs: Any) -> bool:
        checked_refs.append(kwargs["ref_id"])
        return True

    async def billing_enabled() -> bool:
        return True

    async def get_wallet(*_args: Any, **_kwargs: Any) -> Any:
        return type("Wallet", (), {"balance_micro": 10})()

    async def held_amount_for_ref(*args: Any, **_kwargs: Any) -> int:
        checked_refs.append(args[3])
        return 5

    async def allow_negative_balance() -> bool:
        return False

    async def resolve_int(*_args: Any) -> int:
        return 20

    monkeypatch.setattr(completion.runtime_settings, "resolve_int", resolve_int)
    monkeypatch.setattr(completion, "SessionLocal", lambda: Session())
    monkeypatch.setattr(
        completion.worker_billing,
        "_wallet_billing_applies",
        wallet_billing_applies,
    )
    monkeypatch.setattr(completion.worker_billing, "billing_enabled", billing_enabled)
    monkeypatch.setattr(completion.billing_core, "get_wallet", get_wallet)
    monkeypatch.setattr(
        completion.worker_billing,
        "held_amount_for_ref",
        held_amount_for_ref,
    )
    monkeypatch.setattr(
        completion.worker_billing,
        "allow_negative_balance",
        allow_negative_balance,
    )

    with pytest.raises(completion._CompletionToolInsufficientBalance) as excinfo:  # noqa: SLF001
        await completion._ensure_completion_tool_image_wallet_budget(  # noqa: SLF001
            user_id="user-1",
            task_id="comp-1",
        )

    assert excinfo.value.payload["balance_micro"] == 10
    assert excinfo.value.payload["held_micro"] == 5
    assert checked_refs == ["comp-1:retry:1", "comp-1:retry:1"]


def test_generation_retry_delay_is_jittered() -> None:
    helper_source = inspect.getsource(generation._retry_delay_seconds)  # noqa: SLF001
    runner_source = inspect.getsource(generation.run_generation)

    assert "jitter" in helper_source.lower()
    assert "random.uniform" in helper_source
    assert "_retry_delay_seconds(attempt)" in runner_source


def test_provider_pool_weighted_round_robin_honors_weights() -> None:
    pool = ProviderPool()
    group = [
        ProviderConfig(
            name="heavy",
            base_url="https://heavy.example",
            api_key="sk-heavy",
            priority=10,
            weight=3,
        ),
        ProviderConfig(
            name="light",
            base_url="https://light.example",
            api_key="sk-light",
            priority=10,
            weight=1,
        ),
    ]

    first_choices = [pool._weighted_round_robin(group)[0].name for _ in range(8)]

    assert first_choices.count("heavy") == 6
    assert first_choices.count("light") == 2


@pytest.mark.asyncio
async def test_cancel_checks_fail_closed_for_completion_when_redis_errors() -> None:
    class BrokenRedis:
        calls = 0

        async def get(self, _key: str) -> str:
            self.calls += 1
            raise RuntimeError("redis unavailable")

    redis = BrokenRedis()

    # Redis is the authoritative cancellation channel for both task types. If the
    # read path is unavailable, fail closed so a cancellation cannot be missed.
    assert await generation._is_cancelled(redis, "gen-1") is True
    assert await completion._is_cancelled(redis, "comp-1") is True
    assert redis.calls >= 4


@pytest.mark.asyncio
async def test_completion_cancel_check_honors_redis_cancel_key() -> None:
    class Redis:
        async def get(self, _key: str) -> str:
            return "1"

    assert await completion._is_cancelled(Redis(), "comp-1") is True


def test_tool_limit_fallback_completed_finalizes_active_tools() -> None:
    source = inspect.getsource(completion.run_completion)
    fallback_idx = source.index("if tool_loop_truncated:")
    completed_idx = source.index('elif ev_type == "response.completed":', fallback_idx)
    failed_idx = source.index("elif ev_type in {", completed_idx)
    completed_block = source[completed_idx:failed_idx]

    assert "finalize_active(" in completed_block
    assert "ToolStatus.SUCCEEDED.value" in completed_block


@pytest.mark.asyncio
async def test_generation_lease_acquire_uses_nx() -> None:
    class Redis:
        def __init__(self) -> None:
            self.args: tuple[Any, ...] | None = None
            self.kwargs: dict[str, Any] | None = None

        async def set(self, *_args: Any, **kwargs: Any) -> bool:
            self.args = _args
            self.kwargs = kwargs
            return False

    redis = Redis()

    with pytest.raises(generation._LeaseLost):  # noqa: SLF001
        await generation._acquire_lease(redis, "gen-1", "worker-1:token-1")  # noqa: SLF001

    assert redis.args == ("task:gen-1:lease", "worker-1:token-1")
    assert redis.kwargs is not None
    assert redis.kwargs["nx"] is True


@pytest.mark.asyncio
async def test_generation_release_lease_uses_worker_token_cas() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[Any, ...] | None = None

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            return 1

    redis = Redis()

    await generation._release_lease(redis, "gen-1", "worker-1:token-1")  # noqa: SLF001

    assert redis.eval_args is not None
    assert redis.eval_args[1] == 1
    assert redis.eval_args[2] == "task:gen-1:lease"
    assert redis.eval_args[3] == "worker-1:token-1"


@pytest.mark.asyncio
async def test_generation_release_lease_requires_atomic_cas() -> None:
    class RedisWithoutEval:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def get(self, _key: str) -> str:
            return "worker-1"

        async def delete(self, key: str) -> int:
            self.deleted.append(key)
            return 1

    redis = RedisWithoutEval()

    await generation._release_lease(redis, "gen-1", "worker-1")

    assert redis.deleted == []


def test_run_generation_uses_unique_lease_token_for_owner_cas() -> None:
    source = inspect.getsource(generation.run_generation)

    assert 'lease_token = f"{worker_id}:' in source
    assert "_acquire_lease(redis, task_id, lease_token)" in source
    assert "_release_lease(redis, task_id, lease_token)" in source


def test_generation_lease_lost_max_attempts_fails_without_requeue() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.rindex("except _LeaseLost as exc:")
    end = source.index("except _StaleGenerationAttempt", start)
    lease_branch = source[start:end]

    max_idx = lease_branch.index("if attempt >= _MAX_ATTEMPTS:")
    fail_idx = lease_branch.index("_mark_generation_attempt_failed")
    retry_idx = lease_branch.index("_mark_generation_attempt_retrying")

    assert max_idx < fail_idx < retry_idx
    assert "retriable=False" in lease_branch[fail_idx:retry_idx]
    assert "redis.enqueue_job" not in lease_branch[fail_idx:retry_idx]


def test_generation_attempt_update_can_guard_current_status() -> None:
    from sqlalchemy.dialects import postgresql
    from lumen_core.constants import GenerationStatus

    rendered = str(
        generation._generation_attempt_update(  # noqa: SLF001
            "gen-1",
            2,
            statuses=(GenerationStatus.RUNNING.value,),
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "generations.id = 'gen-1'" in rendered
    assert "generations.attempt = 2" in rendered
    assert "generations.status IN ('running')" in rendered


def test_generation_success_write_requires_running_status() -> None:
    source = inspect.getsource(generation.run_generation)
    marker = "parent_upstream_request_for_bonus = dict(upstream_req)"
    start = source.index("status=GenerationStatus.SUCCEEDED.value", source.index(marker))
    update_start = source.rindex("_generation_attempt_update(", 0, start)
    update_end = source.index(").values(", update_start)
    success_update = source[update_start:update_end]

    assert "statuses=_RUNNING_GENERATION_STATUSES" in success_update


def test_completion_terminal_writes_require_streaming_status() -> None:
    assert completion._RUNNING_COMPLETION_STATUSES == (  # noqa: SLF001
        completion.CompletionStatus.STREAMING.value,
    )


def test_generation_max_attempts_failure_releases_hold() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.index('err_code = "max_attempts_exceeded"')
    end = source.index("return", start)
    branch = source[start:end]

    assert "_generation_attempt_update(" in branch
    assert "statuses=(GenerationStatus.QUEUED.value,)" in branch
    assert "worker_billing.release_generation(" in branch
    assert "reason=err_code" in branch
    assert "worker_billing.flush_balance_cache_refreshes(session)" in branch


def test_generation_prequeue_terminal_writes_guard_queued_status() -> None:
    source = inspect.getsource(generation.run_generation)
    markers = [
        "await _ensure_generation_conversation_alive(",
        '"primary_input_image_id must be included in input_image_ids"',
        '"generation already has image task_id=%s image_id=%s',
    ]
    for marker in markers:
        start = source.index(marker)
        end = source.index("return", start)
        branch = source[start:end]
        assert "_generation_attempt_update(" in branch
        assert "statuses=(GenerationStatus.QUEUED.value,)" in branch


def test_completion_max_attempts_failure_releases_hold() -> None:
    source = inspect.getsource(completion.run_completion)
    start = source.index('err_code = "max_attempts_exceeded"')
    end = source.index("return", start)
    branch = source[start:end]

    assert "worker_billing.release_completion(" in branch
    assert "reason=err_code" in branch
    assert "worker_billing.flush_balance_cache_refreshes(session)" in branch


def test_completion_cancel_branch_checks_rowcount_before_message_update() -> None:
    source = inspect.getsource(completion.run_completion)
    start = source.index("except _TaskCancelled as exc:")
    end = source.index("await publish_event(", start)
    branch = source[start:end]

    assert "res = await session.execute(" in branch
    assert "if (res.rowcount or 0) == 0:" in branch
    assert branch.index("if (res.rowcount or 0) == 0:") < branch.index(
        "msg_c = await session.get(Message, message_id)"
    )
    assert "except _CompletionEpochSuperseded as stale_exc:" in branch
    assert branch.index("except _CompletionEpochSuperseded as stale_exc:") < branch.index(
        "except Exception as db_exc:"
    )


def test_generation_byok_early_failure_releases_hold_and_guards_status() -> None:
    source = inspect.getsource(generation.run_generation)
    start = source.index("byok_error = classify_user_credential_error(exc)")
    end = source.index("await publish_event(", start)
    branch = source[start:end]

    assert "_generation_attempt_update(" in branch
    assert "GenerationStatus.QUEUED.value" in branch
    assert "GenerationStatus.RUNNING.value" in branch
    assert "worker_billing.release_generation(" in branch
    assert "reason=err_code" in branch
    assert "worker_billing.flush_balance_cache_refreshes(session)" in branch


def test_sse_timestamp_lock_is_eagerly_initialized() -> None:
    assert sse_publish._TS_LOCK is not None
    assert hasattr(sse_publish._TS_LOCK, "acquire")


def test_sse_xadd_dedupe_uses_per_event_set_nx_ex() -> None:
    lua = " ".join(sse_publish._XADD_IDEMPOTENT_LUA.split())

    assert "HSET" not in sse_publish._XADD_IDEMPOTENT_LUA
    assert "HGET" not in sse_publish._XADD_IDEMPOTENT_LUA
    assert "redis.call('SET', KEYS[2], '', 'NX', 'EX', tonumber(ARGV[5]))" in lua
    assert "return existing" in lua


def test_memory_topic_key_normalizes_unicode() -> None:
    assert memory_extraction._topic_key("Cafe\u0301") == memory_extraction._topic_key(
        "Café"
    )


class _FakeClosableClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("cache_name", "getter_name", "builder_name"),
    [
        ("_proxied_clients", "_get_client", "_build_client"),
        ("_proxied_images_clients", "_get_images_client", "_build_images_client"),
    ],
)
@pytest.mark.asyncio
async def test_proxied_client_cache_is_lru_bounded(
    monkeypatch: pytest.MonkeyPatch,
    cache_name: str,
    getter_name: str,
    builder_name: str,
) -> None:
    timeout_config = upstream._TimeoutConfig(connect=1.0, read=2.0, write=3.0)
    built: list[_FakeClosableClient] = []
    cache = getattr(upstream, cache_name)
    cache.clear()

    async def fake_timeout_config() -> upstream._TimeoutConfig:
        return timeout_config

    def fake_builder(
        _timeout_config: upstream._TimeoutConfig | None = None,
        *,
        proxy_url: str | None = None,
    ) -> _FakeClosableClient:
        assert proxy_url
        client = _FakeClosableClient()
        built.append(client)
        return client

    monkeypatch.setattr(upstream, "_resolve_timeout_config", fake_timeout_config)
    monkeypatch.setattr(upstream, builder_name, fake_builder)
    monkeypatch.setattr(upstream, "_PROXIED_CLIENT_CLOSE_DELAY_SECONDS", 0.01)

    limit = int(getattr(upstream, "_PROXIED_CLIENT_CACHE_MAX", 32))
    getter = getattr(upstream, getter_name)
    try:
        for idx in range(limit + 5):
            await getter(f"http://proxy-{idx}.example:8080")

        assert len(cache) <= limit
        assert not any(client.closed for client in built[:5])
        await asyncio.sleep(0.05)
        assert any(client.closed for client in built[:5])
    finally:
        await upstream.close_client()


@pytest.mark.asyncio
async def test_delayed_client_close_waits_until_idle() -> None:
    class BusyClient:
        def __init__(self) -> None:
            self.closed = False
            self.idle = asyncio.Event()

        async def _wait_until_idle(self, _timeout: float) -> None:
            await self.idle.wait()

        async def aclose(self) -> None:
            self.closed = True

    client = BusyClient()
    close_task = asyncio.create_task(upstream._delayed_aclose(client, delay=0))  # noqa: SLF001
    await asyncio.sleep(0.01)

    assert client.closed is False

    client.idle.set()
    await asyncio.wait_for(close_task, timeout=1.0)
    assert client.closed is True


@pytest.mark.asyncio
async def test_startup_failure_closes_upstream_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    cleanup_calls: list[str] = []

    def raise_startup_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("otel boom")

    async def fake_close_client() -> None:
        cleanup_calls.append("upstream")

    async def fake_billing_shutdown() -> None:
        cleanup_calls.append("billing")

    monkeypatch.setattr(main, "init_sentry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "init_otel", raise_startup_error)
    monkeypatch.setattr(main, "start_metrics_server", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        main, "stop_metrics_server", lambda: cleanup_calls.append("metrics")
    )
    monkeypatch.setattr(main, "close_client", fake_close_client)
    monkeypatch.setattr(main.billing_cache, "shutdown", fake_billing_shutdown)

    with pytest.raises(RuntimeError, match="otel boom"):
        await main._on_startup({"redis": object()})

    assert "upstream" in cleanup_calls
    assert "metrics" in cleanup_calls


@pytest.mark.asyncio
async def test_account_limiter_daily_expiry_stays_in_the_future() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[Any, ...] | None = None

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            return 1

    redis = Redis()
    now = datetime(2026, 5, 16, 23, 59, 59, 900000, tzinfo=timezone.utc).timestamp()

    await account_limiter.record_image_call(redis, "acc1", task_id="task-1", now=now)

    assert redis.eval_args is not None
    day_expire_at = int(redis.eval_args[-1])
    assert day_expire_at > int(now)


@pytest.mark.asyncio
async def test_image_queue_lock_release_uses_owner_cas() -> None:
    class Redis:
        def __init__(self) -> None:
            self.eval_args: tuple[Any, ...] | None = None

        async def set(self, *_args: Any, **_kwargs: Any) -> bool:
            return True

        async def eval(self, *args: Any) -> int:
            self.eval_args = args
            return 0

    redis = Redis()

    async with generation._image_queue_lock(redis):
        pass

    assert redis.eval_args is not None
    assert redis.eval_args[1] == 1
    assert redis.eval_args[2] == "generation:image_queue:lock"


def test_image_queue_reserve_has_atomic_lua_path() -> None:
    source = inspect.getsource(generation._reserve_image_queue_slot)

    assert "_RESERVE_IMAGE_SLOT_LUA" in source
    assert "redis.zadd(provider_zset" in source
