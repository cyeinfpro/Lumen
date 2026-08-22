from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from app import main
from app.agent_runtime_client import AgentRuntimeClient
from app.provider_runtime.upstream_services import ImageUpstreamRuntime
from app.runtime import (
    CapabilityStatus,
    LifecycleState,
    RuntimeLifecycle,
    WorkerRuntime,
)
from app.runtime_settings import RuntimeSettingsCache
from app.storage_writes import StorageWriteCoordinator
from app.tasks.completion_parts.runtime import CompletionRuntime
from app.tasks.generation_parts.runtime import GenerationRuntime
from app.tasks.video_generation_parts.runtime import VideoGenerationRuntime


def _generation_runtime_stub() -> SimpleNamespace:
    async def shutdown() -> None:
        return None

    return SimpleNamespace(
        postprocess_runtime=SimpleNamespace(executor=None),
        shutdown=shutdown,
    )


@pytest.mark.asyncio
async def test_startup_failure_closes_partial_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_warm_tiktoken() -> bool:
        raise RuntimeError("startup failed")

    async def fake_billing_shutdown() -> None:
        calls.append("billing_shutdown")

    image_upstream_runtime = ImageUpstreamRuntime(services=object())  # type: ignore[arg-type]

    async def fake_close_client(*, runtime: ImageUpstreamRuntime) -> None:
        assert runtime is image_upstream_runtime
        calls.append("close_client")

    monkeypatch.setattr(
        main.storage, "ensure_ready", lambda: calls.append("storage_ready")
    )

    async def valid_image_job_configuration(*, runtime: object) -> None:
        assert runtime is not None
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
        main, "stop_metrics_server", lambda *_args: calls.append("metrics_stop")
    )
    monkeypatch.setattr(main, "warm_tiktoken", fail_warm_tiktoken)
    monkeypatch.setattr(main.billing_cache, "shutdown", fake_billing_shutdown)
    monkeypatch.setattr(main, "close_client", fake_close_client)
    monkeypatch.setattr(
        main,
        "build_image_upstream_runtime",
        lambda: image_upstream_runtime,
    )
    monkeypatch.setattr(main, "build_storage_capacity", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        main,
        "StorageWriteCoordinator",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        main,
        "build_generation_runtime",
        lambda **_kwargs: _generation_runtime_stub(),
    )
    monkeypatch.setattr(
        main,
        "build_completion_runtime",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        main,
        "build_video_generation_runtime",
        lambda **_kwargs: object(),
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        await main._on_startup({"redis": object()})

    assert calls == [
        "storage_ready",
        "metrics_stop",
        "close_client",
    ]


@pytest.mark.asyncio
async def test_startup_rejects_invalid_effective_image_job_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def invalid_image_job_configuration(*, runtime: object) -> None:
        assert runtime is not None
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
    monkeypatch.setattr(main, "close_client", lambda *, runtime: None)
    monkeypatch.setattr(main, "stop_metrics_server", lambda *_args: None)

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

    image_upstream_runtime = ImageUpstreamRuntime(services=object())  # type: ignore[arg-type]

    async def fake_close_client(*, runtime: ImageUpstreamRuntime) -> None:
        assert runtime is image_upstream_runtime
        calls.append("upstream")

    def fake_stop_metrics(*_args: object) -> None:
        calls.append("metrics")

    async def fake_dispose() -> None:
        calls.append("engine")

    monkeypatch.setattr(main.billing_cache, "shutdown", failing_billing_shutdown)
    monkeypatch.setattr(main, "close_client", fake_close_client)
    monkeypatch.setattr(main, "stop_metrics_server", fake_stop_metrics)
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=fake_dispose))

    await main._on_shutdown(
        {
            "image_upstream_runtime": image_upstream_runtime,
            "metrics_server_runtime": main.MetricsServerRuntime(),
        }
    )

    # 引擎必须最后 dispose：前面的清理仍可能借用连接。
    assert calls == ["billing", "upstream", "metrics", "engine"]


@pytest.mark.asyncio
async def test_shutdown_disposes_engine_even_when_earlier_cleanup_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E-5：任一清理项失败都不得跳过 engine.dispose，否则连接持续泄漏。"""
    disposed: list[str] = []

    image_upstream_runtime = ImageUpstreamRuntime(services=object())  # type: ignore[arg-type]

    async def failing_close_client(*, runtime: ImageUpstreamRuntime) -> None:
        assert runtime is image_upstream_runtime
        raise RuntimeError("upstream client close failed")

    def failing_stop_metrics(*_args: object) -> None:
        raise RuntimeError("metrics server stop failed")

    async def fake_dispose() -> None:
        disposed.append("engine")

    monkeypatch.setattr(main.billing_cache, "shutdown", lambda: None)
    monkeypatch.setattr(main, "close_client", failing_close_client)
    monkeypatch.setattr(main, "stop_metrics_server", failing_stop_metrics)
    monkeypatch.setattr(main, "engine", SimpleNamespace(dispose=fake_dispose))

    await main._on_shutdown({"image_upstream_runtime": image_upstream_runtime})

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


@pytest.mark.asyncio
async def test_startup_injects_one_storage_coordinator_into_all_media_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = object()
    image_upstream_runtime = object()
    injected: list[tuple[str, object, object]] = []
    ctx = {"redis": object()}

    async def valid_image_job_configuration(*, runtime: object) -> None:
        assert runtime is image_upstream_runtime
        return None

    async def configure_billing(_redis: object) -> None:
        return None

    monkeypatch.setattr(
        main,
        "validate_effective_image_job_configuration",
        valid_image_job_configuration,
    )
    monkeypatch.setattr(
        main,
        "build_image_upstream_runtime",
        lambda: image_upstream_runtime,
    )
    monkeypatch.setattr(main.storage, "ensure_ready", lambda: None)
    monkeypatch.setattr(main, "build_storage_capacity", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        main,
        "StorageWriteCoordinator",
        lambda **_kwargs: coordinator,
    )
    monkeypatch.setattr(
        main,
        "build_generation_runtime",
        lambda *, storage_writes, image_upstream_runtime: (
            injected.append(("generation", storage_writes, image_upstream_runtime))
            or _generation_runtime_stub()
        ),
    )
    monkeypatch.setattr(
        main,
        "build_completion_runtime",
        lambda *, storage_writes, image_upstream_runtime: (
            injected.append(("completion", storage_writes, image_upstream_runtime))
            or object()
        ),
    )
    monkeypatch.setattr(
        main,
        "build_video_generation_runtime",
        lambda *, storage_writes: (
            injected.append(("video", storage_writes, image_upstream_runtime))
            or object()
        ),
    )
    monkeypatch.setattr(main, "init_sentry", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "init_otel", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "bind_db_pool_metrics", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "start_metrics_server", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "warm_tiktoken", lambda: True)
    monkeypatch.setattr(main.billing_cache, "configure", configure_billing)

    await main._on_startup(ctx)

    assert injected == [
        ("generation", coordinator, image_upstream_runtime),
        ("completion", coordinator, image_upstream_runtime),
        ("video", coordinator, image_upstream_runtime),
    ]
    assert ctx["storage_write_coordinator"] is coordinator
    assert ctx["image_upstream_runtime"] is image_upstream_runtime


def test_provider_cron_has_hard_timeout() -> None:
    probe_job = next(
        job
        for job in main.WorkerSettings.cron_jobs
        if job.coroutine is main.probe_providers
    )

    assert probe_job.timeout_s == main._PROVIDER_CRON_TIMEOUT_S
    assert probe_job.timeout_s > 0


@pytest.mark.asyncio
async def test_worker_runtime_exposes_typed_context_and_idempotent_shutdown() -> None:
    calls: list[str] = []
    lifecycle = RuntimeLifecycle("worker")

    async def close_generation() -> None:
        calls.append("generation")

    async def close_upstream() -> None:
        calls.append("upstream")

    async def close_engine() -> None:
        calls.append("engine")

    lifecycle.own("engine", close_engine)
    lifecycle.own("upstream", close_upstream)
    lifecycle.own("generation", close_generation)
    runtime_settings = cast(RuntimeSettingsCache, object())
    image_upstream = cast(ImageUpstreamRuntime, object())
    storage_writes = cast(StorageWriteCoordinator, object())
    generation = cast(
        GenerationRuntime,
        SimpleNamespace(postprocess_runtime=SimpleNamespace(executor=None)),
    )
    completion = cast(CompletionRuntime, object())
    video = cast(VideoGenerationRuntime, object())
    agent_runtime = AgentRuntimeClient(
        base_url="http://agent-runtime:8090",
        shared_secret="test-agent-runtime-secret-0123456789",
    )
    runtime = WorkerRuntime(
        _runtime_settings=runtime_settings,
        _image_upstream=image_upstream,
        _storage_writes=storage_writes,
        _generation=generation,
        _completion=completion,
        _video=video,
        _metrics_server=main.MetricsServerRuntime(),
        _agent_runtime=agent_runtime,
        _lifecycle=lifecycle,
    )

    runtime.start()
    values = runtime.context_values()
    await runtime.close()
    await runtime.close()

    assert values == {
        "runtime_settings_cache": runtime_settings,
        "image_upstream_runtime": image_upstream,
        "storage_write_coordinator": storage_writes,
        "generation_runtime": generation,
        "completion_runtime": completion,
        "video_generation_runtime": video,
        "metrics_server_runtime": runtime.metrics_server(),
        "agent_runtime_client": agent_runtime,
    }
    assert calls == ["generation", "upstream", "engine"]
    diagnostics = runtime.diagnostics()
    assert diagnostics.lifecycle.state is LifecycleState.CLOSED
    assert {
        capability.name: capability.status for capability in diagnostics.capabilities
    }["postprocess_executor"] is CapabilityStatus.DISABLED
