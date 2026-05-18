from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Any

import pytest

from app import account_limiter, upstream
from app.tasks import completion, generation


def test_completion_charge_uses_same_session_before_success_commit() -> None:
    source = inspect.getsource(completion.run_completion)
    charge_call = "await worker_billing.charge_completion(session, comp_for_billing)"

    charge_idx = source.index(charge_call)
    commit_idx = source.index("await session.commit()", charge_idx)
    between = source[charge_idx:commit_idx]

    assert charge_idx < commit_idx
    assert "SessionLocal" not in between
    assert "async with" not in between


def test_generation_retry_delay_is_jittered() -> None:
    helper_source = inspect.getsource(generation._retry_delay_seconds)  # noqa: SLF001
    runner_source = inspect.getsource(generation.run_generation)

    assert "jitter" in helper_source.lower()
    assert "random.uniform" in helper_source
    assert "_retry_delay_seconds(attempt)" in runner_source


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
