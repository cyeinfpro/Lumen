from __future__ import annotations

import asyncio

import pytest

import app.main as main
from app.images.application import reconcile_runtime
from app.routes import billing


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Redis:
    def __init__(self) -> None:
        self.pinged = False
        self.closed = False

    async def ping(self) -> bool:
        self.pinged = True
        return True

    async def aclose(self) -> None:
        self.closed = True


class _BillingCache:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start_workers(self) -> None:
        self.started = True

    async def stop_workers(self) -> None:
        self.stopped = True


def _patch_common_startup(
    monkeypatch: pytest.MonkeyPatch,
    redis: _Redis,
    cache: _BillingCache,
    configured: list[object],
) -> None:
    monkeypatch.setattr(main, "init_sentry", lambda *_args: None)
    monkeypatch.setattr(main, "init_otel", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_check_alembic_head", _noop)
    monkeypatch.setattr(main, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(main, "migrate_image_primary_route", _false)
    monkeypatch.setattr(main, "migrate_provider_purposes", _false)
    monkeypatch.setattr(main, "get_redis", lambda: redis)
    monkeypatch.setattr(main, "BillingCacheService", lambda redis: cache)
    monkeypatch.setattr(
        billing,
        "configure_billing_cache",
        lambda service: configured.append(service),
    )


async def _noop(*_args: object, **_kwargs: object) -> None:
    return None


async def _false(*_args: object, **_kwargs: object) -> bool:
    return False


@pytest.mark.asyncio
async def test_lifespan_unwinds_resources_when_arq_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    cache = _BillingCache()
    configured: list[object] = []
    _patch_common_startup(monkeypatch, redis, cache, configured)

    async def fail_arq() -> object:
        raise RuntimeError("arq unavailable")

    monkeypatch.setattr(main, "get_arq_pool", fail_arq)

    with pytest.raises(RuntimeError, match="arq unavailable"):
        async with main.lifespan(main.app):
            raise AssertionError("lifespan should not yield")

    assert redis.pinged is True
    assert redis.closed is True
    assert cache.started is True
    assert cache.stopped is True
    assert configured == [cache, None]


@pytest.mark.asyncio
async def test_lifespan_cancels_reconcile_task_when_warmup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _Redis()
    cache = _BillingCache()
    configured: list[object] = []
    _patch_common_startup(monkeypatch, redis, cache, configured)

    arq_closed = False

    async def close_arq() -> None:
        nonlocal arq_closed
        arq_closed = True

    monkeypatch.setattr(main, "get_arq_pool", _noop)
    monkeypatch.setattr(main, "close_arq_pool", close_arq)

    async def fake_reconcile(stop: asyncio.Event) -> None:
        await stop.wait()

    monkeypatch.setattr(
        reconcile_runtime,
        "image_artifact_reconciler_loop",
        fake_reconcile,
    )
    created_tasks: list[asyncio.Task[object]] = []
    real_create_task = asyncio.create_task

    def capture_create_task(coro, **kwargs):  # type: ignore[no-untyped-def]
        task = real_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(main.asyncio, "create_task", capture_create_task)

    def fail_warmup(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("warmup failed")

    monkeypatch.setattr(main, "warm_tiktoken", fail_warmup)

    with pytest.raises(RuntimeError, match="warmup failed"):
        async with main.lifespan(main.app):
            raise AssertionError("lifespan should not yield")

    assert len(created_tasks) == 1
    assert created_tasks[0].done()
    assert created_tasks[0].cancelled()
    assert arq_closed is True
    assert cache.stopped is True
    assert redis.closed is True
    assert configured == [cache, None]
