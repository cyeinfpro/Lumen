from __future__ import annotations

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

    monkeypatch.setattr(main.billing_cache, "shutdown", failing_billing_shutdown)
    monkeypatch.setattr(main, "close_client", fake_close_client)
    monkeypatch.setattr(main, "stop_metrics_server", fake_stop_metrics)

    await main._on_shutdown({})

    assert calls == ["billing", "upstream", "metrics"]


def test_provider_cron_has_hard_timeout() -> None:
    probe_job = next(
        job
        for job in main.WorkerSettings.cron_jobs
        if job.coroutine is main.probe_providers
    )

    assert probe_job.timeout_s == main._PROVIDER_CRON_TIMEOUT_S
    assert probe_job.timeout_s > 0
