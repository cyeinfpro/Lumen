from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import main


@pytest.mark.asyncio
async def test_startup_failure_closes_partial_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_warm_tiktoken() -> bool:
        raise RuntimeError("startup failed")

    async def fake_billing_shutdown() -> None:
        calls.append("billing_shutdown")

    async def fake_close_client() -> None:
        calls.append("close_client")

    monkeypatch.setattr(
        main.storage, "ensure_ready", lambda: calls.append("storage_ready")
    )

    async def valid_image_job_configuration() -> None:
        return None

    monkeypatch.setattr(
        main,
        "validate_effective_image_job_configuration",
        valid_image_job_configuration,
    )
    monkeypatch.setattr(main, "init_sentry", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "init_otel", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "start_metrics_server", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        main, "stop_metrics_server", lambda: calls.append("metrics_stop")
    )
    monkeypatch.setattr(main, "warm_tiktoken", fail_warm_tiktoken)
    monkeypatch.setattr(main.billing_cache, "shutdown", fake_billing_shutdown)
    monkeypatch.setattr(main, "close_client", fake_close_client)

    with pytest.raises(RuntimeError, match="startup failed"):
        await main._on_startup({"redis": object()})

    assert calls == [
        "storage_ready",
        "billing_shutdown",
        "close_client",
        "metrics_stop",
    ]


@pytest.mark.asyncio
async def test_startup_rejects_invalid_effective_image_job_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def invalid_image_job_configuration() -> None:
        raise RuntimeError("effective image_jobs_only configuration is invalid")

    monkeypatch.setattr(
        main,
        "validate_effective_image_job_configuration",
        invalid_image_job_configuration,
    )
    monkeypatch.setattr(
        main.storage,
        "ensure_ready",
        lambda: calls.append("storage_ready"),
    )
    monkeypatch.setattr(main.billing_cache, "shutdown", lambda: None)
    monkeypatch.setattr(main, "close_client", lambda: None)
    monkeypatch.setattr(main, "stop_metrics_server", lambda: None)

    with pytest.raises(RuntimeError, match="effective image_jobs_only"):
        await main._on_startup({"redis": object()})

    assert calls == []


@pytest.mark.asyncio
async def test_shutdown_attempts_each_cleanup_after_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def failing_billing_shutdown() -> None:
        calls.append("billing")
        raise RuntimeError("billing cleanup failed")

    async def fake_close_client() -> None:
        calls.append("upstream")

    def fake_stop_metrics() -> None:
        calls.append("metrics")

    async def fake_dispose() -> None:
        calls.append("engine")

    monkeypatch.setattr(main.billing_cache, "shutdown", failing_billing_shutdown)
    monkeypatch.setattr(main, "close_client", fake_close_client)
    monkeypatch.setattr(main, "stop_metrics_server", fake_stop_metrics)
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=fake_dispose))

    await main._on_shutdown({})

    # 引擎必须最后 dispose：前面的清理仍可能借用连接。
    assert calls == ["billing", "upstream", "metrics", "engine"]


@pytest.mark.asyncio
async def test_shutdown_disposes_engine_even_when_earlier_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-5：任一清理项失败都不得跳过 engine.dispose，否则连接持续泄漏。"""
    disposed: list[str] = []

    async def failing_close_client() -> None:
        raise RuntimeError("upstream client close failed")

    def failing_stop_metrics() -> None:
        raise RuntimeError("metrics server stop failed")

    async def fake_dispose() -> None:
        disposed.append("engine")

    monkeypatch.setattr(main.billing_cache, "shutdown", lambda: None)
    monkeypatch.setattr(main, "close_client", failing_close_client)
    monkeypatch.setattr(main, "stop_metrics_server", failing_stop_metrics)
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=fake_dispose))

    await main._on_shutdown({})

    assert disposed == ["engine"]


def test_redis_settings_are_explicitly_hardened() -> None:
    """E-16: from_dsn 只给连接信息，重连/池上限必须显式接管。"""
    from arq.connections import RedisSettings

    dsn = "redis://user:secret@redis.internal:6380/3"
    resolved = main.build_redis_settings(dsn)
    stock = RedisSettings.from_dsn(dsn)

    # DSN 里的连接身份不能被覆盖掉。
    assert (resolved.host, resolved.port, resolved.database) == (
        "redis.internal",
        6380,
        3,
    )
    assert resolved.password == "secret"

    # 库默认值必须被替换：1s 建连超时 + 无重试 + 无池上限撑不住主从切换。
    assert stock.conn_timeout == 1
    assert resolved.conn_timeout == main._REDIS_CONN_TIMEOUT_S > stock.conn_timeout
    assert resolved.conn_retries == main._REDIS_CONN_RETRIES > stock.conn_retries
    assert resolved.conn_retry_delay == main._REDIS_CONN_RETRY_DELAY_S
    assert stock.max_connections is None
    assert resolved.max_connections == main._REDIS_MAX_CONNECTIONS
    assert stock.retry_on_timeout is False
    assert resolved.retry_on_timeout is True
    assert resolved.retry_on_error == [main.RedisConnectionError, main.BusyLoadingError]
    assert resolved.retry is not None


def test_worker_settings_use_hardened_redis_settings() -> None:
    assert main.WorkerSettings.redis_settings.max_connections == (
        main._REDIS_MAX_CONNECTIONS
    )
    assert main.WorkerSettings.redis_settings.retry_on_timeout is True


def test_provider_cron_has_hard_timeout() -> None:
    probe_job = next(
        job
        for job in main.WorkerSettings.cron_jobs
        if job.coroutine is main.probe_providers
    )

    assert probe_job.timeout_s == main._PROVIDER_CRON_TIMEOUT_S
    assert probe_job.timeout_s > 0
