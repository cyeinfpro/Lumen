"""Paid-call-free Agent Runtime health and feature readiness."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from lumen_core.runtime_settings import get_spec

from ..config import settings
from ..observability import (
    agent_runtime_health_duration_seconds,
    agent_runtime_health_total,
)
from ..runtime_settings import get_setting


logger = logging.getLogger(__name__)
_MIN_SECRET_BYTES = 32


@dataclass(frozen=True, slots=True)
class AgentRuntimeProbe:
    ok: bool
    status_code: int | None
    runtime_version: str | None
    error_code: str | None
    auth_key_id: str | None = None
    max_request_bytes: int | None = None
    max_line_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class AgentHealthSnapshot:
    enabled: bool
    runtime_auth_configured: bool
    tool_gateway_configured: bool
    runtime_live: bool | None
    runtime_ready: bool | None
    runtime_version: str | None
    error_code: str | None

    @property
    def operational(self) -> bool:
        return not self.enabled or (
            self.runtime_auth_configured
            and self.tool_gateway_configured
            and self.runtime_live is True
            and self.runtime_ready is True
        )


def _secret_configured(value: str) -> bool:
    return len((value or "").encode("utf-8")) >= _MIN_SECRET_BYTES


async def effective_agent_enabled(executor: Any) -> bool:
    spec = get_spec("agent.enabled")
    if spec is None:
        return False
    return await get_setting(executor, spec) == "1"


def _probe_payload(
    response: httpx.Response,
) -> tuple[
    bool,
    str | None,
    str | None,
    str | None,
    int | None,
    int | None,
]:
    try:
        payload = response.json()
    except ValueError:
        return False, None, "agent_runtime_invalid_health_response", None, None, None
    if not isinstance(payload, dict):
        return False, None, "agent_runtime_invalid_health_response", None, None, None
    version = payload.get("runtime_version")
    runtime_version = version if isinstance(version, str) and version else None
    ok = response.status_code == 200 and payload.get("ok") is True
    raw_error = payload.get("error_code")
    error_code = raw_error if isinstance(raw_error, str) and raw_error else None
    if not ok and error_code is None:
        error_code = "agent_runtime_not_ready"
    raw_key_id = payload.get("auth_key_id")
    auth_key_id = (
        raw_key_id
        if isinstance(raw_key_id, str)
        and len(raw_key_id) == 16
        and all(char in "0123456789abcdef" for char in raw_key_id)
        else None
    )
    max_request = payload.get("max_request_bytes")
    max_line = payload.get("max_line_bytes")
    return (
        ok,
        runtime_version,
        error_code,
        auth_key_id,
        max_request if isinstance(max_request, int) else None,
        max_line if isinstance(max_line, int) else None,
    )


async def probe_agent_runtime(endpoint: str) -> AgentRuntimeProbe:
    if endpoint not in {"healthz", "readyz"}:
        raise ValueError("unsupported Agent Runtime health endpoint")
    started = time.monotonic()
    outcome = "transport_error"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.agent_runtime_health_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.get(f"{settings.agent_runtime_url}/{endpoint}")
        (
            ok,
            runtime_version,
            error_code,
            auth_key_id,
            max_request_bytes,
            max_line_bytes,
        ) = _probe_payload(response)
        outcome = "ready" if ok else "not_ready"
        return AgentRuntimeProbe(
            ok=ok,
            status_code=response.status_code,
            runtime_version=runtime_version,
            error_code=error_code,
            auth_key_id=auth_key_id,
            max_request_bytes=max_request_bytes,
            max_line_bytes=max_line_bytes,
        )
    except httpx.HTTPError:
        logger.warning("Agent Runtime %s probe failed", endpoint)
        return AgentRuntimeProbe(
            ok=False,
            status_code=None,
            runtime_version=None,
            error_code="agent_runtime_unreachable",
        )
    finally:
        agent_runtime_health_total.labels(endpoint=endpoint, outcome=outcome).inc()
        agent_runtime_health_duration_seconds.labels(
            endpoint=endpoint,
            outcome=outcome,
        ).observe(max(0.0, time.monotonic() - started))


async def agent_health_snapshot(
    executor: Any,
    *,
    enabled_override: bool | None = None,
) -> AgentHealthSnapshot:
    enabled = (
        enabled_override
        if enabled_override is not None
        else await effective_agent_enabled(executor)
    )
    runtime_auth_configured = _secret_configured(settings.agent_runtime_shared_secret)
    tool_gateway_configured = _secret_configured(settings.agent_tool_capability_secret)
    if not enabled:
        return AgentHealthSnapshot(
            enabled=False,
            runtime_auth_configured=runtime_auth_configured,
            tool_gateway_configured=tool_gateway_configured,
            runtime_live=None,
            runtime_ready=None,
            runtime_version=None,
            error_code=None,
        )
    if not runtime_auth_configured or not tool_gateway_configured:
        return AgentHealthSnapshot(
            enabled=True,
            runtime_auth_configured=runtime_auth_configured,
            tool_gateway_configured=tool_gateway_configured,
            runtime_live=False,
            runtime_ready=False,
            runtime_version=None,
            error_code="agent_runtime_secrets_unconfigured",
        )
    live = await probe_agent_runtime("healthz")
    if not live.ok:
        return AgentHealthSnapshot(
            enabled=True,
            runtime_auth_configured=True,
            tool_gateway_configured=True,
            runtime_live=False,
            runtime_ready=False,
            runtime_version=live.runtime_version,
            error_code=live.error_code,
        )
    ready = await probe_agent_runtime("readyz")
    expected_key_id = hashlib.sha256(
        settings.agent_runtime_shared_secret.encode("utf-8")
    ).hexdigest()[:16]
    if not hmac.compare_digest(ready.auth_key_id or "", expected_key_id):
        return AgentHealthSnapshot(
            enabled=True,
            runtime_auth_configured=True,
            tool_gateway_configured=True,
            runtime_live=True,
            runtime_ready=False,
            runtime_version=ready.runtime_version or live.runtime_version,
            error_code="agent_runtime_auth_mismatch",
        )
    if (
        ready.max_request_bytes is not None
        and ready.max_request_bytes != settings.agent_runtime_max_request_bytes
    ):
        return AgentHealthSnapshot(
            enabled=True,
            runtime_auth_configured=True,
            tool_gateway_configured=True,
            runtime_live=True,
            runtime_ready=False,
            runtime_version=ready.runtime_version or live.runtime_version,
            error_code="agent_runtime_limit_mismatch",
        )
    return AgentHealthSnapshot(
        enabled=True,
        runtime_auth_configured=True,
        tool_gateway_configured=True,
        runtime_live=True,
        runtime_ready=ready.ok,
        runtime_version=ready.runtime_version or live.runtime_version,
        error_code=ready.error_code,
    )


__all__ = [
    "AgentHealthSnapshot",
    "AgentRuntimeProbe",
    "agent_health_snapshot",
    "effective_agent_enabled",
    "probe_agent_runtime",
]
