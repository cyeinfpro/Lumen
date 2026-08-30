from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from lumen_core.agent_provider_contract import (
    AGENT_PROBE_EXPECTED_TEXT,
    AGENT_PROBE_MAX_RESPONSE_BYTES,
    agent_endpoint_contract,
    agent_probe_headers,
    build_agent_probe_request,
    parse_agent_probe_sse,
)
from lumen_core.providers import parse_proxy_item
from lumen_core.providers_parts.definitions import ProviderProxyDefinition
from lumen_core.schema_models.providers import ProviderProbeResult

from .presentation import normalize_bool

from ...proxy_pool import resolve_provider_proxy_url


PROBE_TIMEOUT_S = 15.0


@dataclass
class ProbeOutcome:
    ok: bool
    latency_ms: int
    error: str | None
    http_status: int | None
    capability_signal: str | None

    def __iter__(self):
        yield self.ok
        yield self.latency_ms
        yield self.error


def responses_url(base_url: str) -> str:
    """Legacy compatibility facade for non-Agent callers and tests."""
    return agent_endpoint_contract(base_url, "openai-responses").request_url


def classify_probe_status(status: int) -> tuple[str, str | None]:
    if status in (404, 405):
        return "unsupported", f"HTTP {status}"
    if status in (401, 403):
        return "auth", f"HTTP {status}"
    if status == 429 or 500 <= status < 600:
        return "transient", f"HTTP {status}"
    return "unsupported" if status == 501 else "", f"HTTP {status}"


def truncate_probe_error(value: str, *, limit: int = 240) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 8].rstrip() + "... (truncated)"


def probe_error_detail_from_payload(payload: object) -> str | None:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if isinstance(message, str) and message.strip():
                return truncate_probe_error(message)
        if isinstance(error, str) and error.strip():
            return truncate_probe_error(error)
        message = payload.get("message") or payload.get("detail")
        if isinstance(message, str) and message.strip():
            return truncate_probe_error(message)
    return None


def probe_http_error_message(response: httpx.Response, fallback: str | None) -> str:
    detail: str | None = None
    try:
        detail = probe_error_detail_from_payload(response.json())
    except Exception:  # noqa: BLE001
        detail = None
    prefix = fallback or f"HTTP {response.status_code}"
    return f"{prefix}: {detail}" if detail else prefix


async def probe_one(
    base_url: str,
    api_key: str,
    *,
    proxy: ProviderProxyDefinition | None = None,
    agent_api: str = "openai-responses",
    agent_base_url: str | None = None,
    model: str = "gpt-5.4-mini",
) -> ProbeOutcome:
    started_at = time.monotonic()
    try:
        endpoint = agent_endpoint_contract(
            base_url,
            agent_api,
            agent_base_url=agent_base_url,
        )
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PROBE_TIMEOUT_S),
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                endpoint.request_url,
                json=build_agent_probe_request(agent_api, model),
                headers=agent_probe_headers(agent_api, api_key),
            )
        latency = int((time.monotonic() - started_at) * 1000)
        if response.status_code >= 400:
            signal, error = classify_probe_status(response.status_code)
            return ProbeOutcome(
                ok=False,
                latency_ms=latency,
                error=probe_http_error_message(response, error),
                http_status=response.status_code,
                capability_signal=signal or None,
            )
        raw = response.text.encode("utf-8")
        if len(raw) > AGENT_PROBE_MAX_RESPONSE_BYTES:
            return ProbeOutcome(False, latency, "response_too_large", 200, None)
        try:
            parsed = parse_agent_probe_sse(agent_api, raw.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            return ProbeOutcome(False, latency, "invalid_stream", 200, None)
        if not parsed.terminal:
            return ProbeOutcome(False, latency, "terminal_missing", 200, None)
        if not parsed.usage_present:
            return ProbeOutcome(False, latency, "usage_missing", 200, None)
        if parsed.text.strip() != AGENT_PROBE_EXPECTED_TEXT:
            digest = hashlib.sha256(parsed.text.encode("utf-8")).hexdigest()[:16]
            return ProbeOutcome(False, latency, f"wrong_answer:{digest}", 200, None)
        return ProbeOutcome(True, latency, None, 200, "supported")
    except httpx.TimeoutException:
        latency = int((time.monotonic() - started_at) * 1000)
        return ProbeOutcome(False, latency, "timeout", None, "transient")
    except ValueError:
        latency = int((time.monotonic() - started_at) * 1000)
        return ProbeOutcome(
            False, latency, "invalid_agent_endpoint", None, "unsupported"
        )
    except Exception as exc:  # noqa: BLE001
        latency = int((time.monotonic() - started_at) * 1000)
        return ProbeOutcome(False, latency, type(exc).__name__, None, None)


def probe_blocked_by_endpoint_lock(_item: dict[str, Any]) -> bool:
    """Agent probes are independent from image endpoint locks."""
    return False


async def probe_configured_providers(
    items: list[dict[str, Any]],
    proxy_items: list[dict[str, Any]],
    *,
    names_filter: set[str] | None,
    default_model: str,
    runner: Callable[..., Awaitable[ProbeOutcome]] = probe_one,
) -> list[ProviderProbeResult]:
    proxy_by_name: dict[str, ProviderProxyDefinition] = {}
    for index, proxy_item in enumerate(proxy_items):
        try:
            parsed = parse_proxy_item(proxy_item, index=index)
        except Exception:  # noqa: BLE001
            continue
        proxy_by_name[parsed.name] = parsed

    async def probe_item(item: dict[str, Any], index: int) -> ProviderProbeResult:
        name = item.get("name") or f"provider-{index}"
        base_url = item.get("base_url", "")
        api_key = item.get("api_key", "")
        agent_api = str(item.get("agent_api") or "openai-responses")
        agent_base_url = str(item.get("agent_base_url") or "").strip() or None
        agent_models = {
            value.strip()
            for value in item.get("agent_models", [])
            if isinstance(value, str) and value.strip()
        }

        if names_filter and name not in names_filter:
            return ProviderProbeResult(name=name, ok=False, status="skipped")
        if not normalize_bool(item.get("enabled"), default=True):
            return ProviderProbeResult(name=name, ok=False, status="disabled")
        if probe_blocked_by_endpoint_lock(item):
            return ProviderProbeResult(
                name=name,
                ok=False,
                status="skipped",
                error="endpoint_locked_to_generations",
            )
        if not base_url or not api_key or not default_model:
            return ProviderProbeResult(
                name=name,
                ok=False,
                error="missing config",
                status="unhealthy",
            )
        if agent_models and default_model not in agent_models:
            return ProviderProbeResult(
                name=name,
                ok=False,
                error="configured_model_not_admitted",
                status="skipped",
            )

        proxy_name = item.get("proxy")
        proxy = (
            proxy_by_name.get(proxy_name)
            if isinstance(proxy_name, str) and proxy_name
            else None
        )
        outcome = await runner(
            base_url,
            api_key,
            proxy=proxy,
            agent_api=agent_api,
            agent_base_url=agent_base_url,
            model=default_model,
        )
        return ProviderProbeResult(
            name=name,
            ok=outcome.ok,
            latency_ms=outcome.latency_ms,
            error=outcome.error,
            status="healthy" if outcome.ok else "unhealthy",
            capability_signal=outcome.capability_signal,
            http_status=outcome.http_status,
        )

    return list(
        await asyncio.gather(
            *(probe_item(item, index) for index, item in enumerate(items))
        )
    )
