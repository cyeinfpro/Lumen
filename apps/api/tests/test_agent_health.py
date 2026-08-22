from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routes import system_settings
from app.services import agent_health


@pytest.mark.asyncio
async def test_disabled_agent_health_does_not_probe_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def disabled(_executor: object) -> bool:
        return False

    async def unexpected_probe(_endpoint: str):
        pytest.fail("disabled Agent must not probe Runtime")

    monkeypatch.setattr(agent_health, "effective_agent_enabled", disabled)
    monkeypatch.setattr(agent_health, "probe_agent_runtime", unexpected_probe)

    snapshot = await agent_health.agent_health_snapshot(object())

    assert snapshot.enabled is False
    assert snapshot.operational is True
    assert snapshot.runtime_live is None
    assert snapshot.runtime_ready is None


@pytest.mark.asyncio
async def test_enabled_agent_health_requires_paid_call_free_runtime_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_runtime_secret = settings.agent_runtime_shared_secret
    original_tool_secret = settings.agent_tool_capability_secret
    calls: list[str] = []
    runtime_key_id = hashlib.sha256(("r" * 32).encode()).hexdigest()[:16]

    async def probe(endpoint: str) -> agent_health.AgentRuntimeProbe:
        calls.append(endpoint)
        return agent_health.AgentRuntimeProbe(
            ok=True,
            status_code=200,
            runtime_version="pi-0.84.2",
            error_code=None,
            auth_key_id=runtime_key_id if endpoint == "readyz" else None,
        )

    settings.agent_runtime_shared_secret = "r" * 32
    settings.agent_tool_capability_secret = "t" * 32
    monkeypatch.setattr(agent_health, "probe_agent_runtime", probe)
    try:
        snapshot = await agent_health.agent_health_snapshot(
            object(),
            enabled_override=True,
        )
    finally:
        settings.agent_runtime_shared_secret = original_runtime_secret
        settings.agent_tool_capability_secret = original_tool_secret

    assert calls == ["healthz", "readyz"]
    assert snapshot.operational is True
    assert snapshot.runtime_version == "pi-0.84.2"


@pytest.mark.asyncio
async def test_enabled_agent_health_rejects_runtime_hmac_key_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_runtime_secret = settings.agent_runtime_shared_secret
    original_tool_secret = settings.agent_tool_capability_secret

    async def probe(endpoint: str) -> agent_health.AgentRuntimeProbe:
        return agent_health.AgentRuntimeProbe(
            ok=True,
            status_code=200,
            runtime_version="pi-0.84.2",
            error_code=None,
            auth_key_id="0" * 16 if endpoint == "readyz" else None,
        )

    settings.agent_runtime_shared_secret = "r" * 32
    settings.agent_tool_capability_secret = "t" * 32
    monkeypatch.setattr(agent_health, "probe_agent_runtime", probe)
    try:
        snapshot = await agent_health.agent_health_snapshot(
            object(),
            enabled_override=True,
        )
    finally:
        settings.agent_runtime_shared_secret = original_runtime_secret
        settings.agent_tool_capability_secret = original_tool_secret

    assert snapshot.operational is False
    assert snapshot.runtime_ready is False
    assert snapshot.error_code == "agent_runtime_auth_mismatch"


@pytest.mark.asyncio
async def test_admin_cannot_enable_agent_before_runtime_is_operational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*_args, **_kwargs):
        return SimpleNamespace(
            operational=False,
            runtime_live=False,
            runtime_ready=False,
            runtime_auth_configured=True,
            tool_gateway_configured=True,
            error_code="agent_runtime_unreachable",
        )

    monkeypatch.setattr(system_settings, "agent_health_snapshot", unavailable)

    with pytest.raises(HTTPException) as exc_info:
        await system_settings._validate_agent_setting_semantics(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            {"agent.enabled": "1"},
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"]["code"] == "agent_runtime_not_ready"


@pytest.mark.asyncio
async def test_admin_disable_does_not_require_runtime_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected(*_args, **_kwargs):
        pytest.fail("disabling Agent must not probe Runtime")

    monkeypatch.setattr(system_settings, "agent_health_snapshot", unexpected)

    await system_settings._validate_agent_setting_semantics(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        {"agent.enabled": "0"},
    )
