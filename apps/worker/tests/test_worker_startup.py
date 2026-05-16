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

    monkeypatch.setattr(main, "init_sentry", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "init_otel", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "start_metrics_server", lambda *_a, **_kw: None)
    monkeypatch.setattr(main, "warm_tiktoken", fail_warm_tiktoken)
    monkeypatch.setattr(main.billing_cache, "shutdown", fake_billing_shutdown)
    monkeypatch.setattr(main, "close_client", fake_close_client)

    with pytest.raises(RuntimeError, match="startup failed"):
        await main._on_startup({"redis": object()})

    assert calls == ["billing_shutdown", "close_client"]
