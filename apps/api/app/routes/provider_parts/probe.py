from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from lumen_core.byok import (
    build_provider_probe_request,
    extract_response_output_text,
    extract_sse_output_text,
)
from lumen_core.providers import (
    ProviderProxyDefinition,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
)

from .presentation import (
    normalize_image_jobs_endpoint,
    normalize_image_jobs_endpoint_lock,
)


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
    base = base_url.strip().rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


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
    return text[: limit - 8].rstrip() + "…\n（已截断）"


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


def probe_http_error_message(
    response: httpx.Response,
    fallback: str | None,
) -> str:
    detail: str | None = None
    try:
        detail = probe_error_detail_from_payload(response.json())
    except Exception:  # noqa: BLE001
        detail = None
    if not detail and response.text:
        detail = truncate_probe_error(response.text)
    prefix = fallback or f"HTTP {response.status_code}"
    return f"{prefix}: {detail}" if detail else prefix


async def probe_one(
    base_url: str,
    api_key: str,
    *,
    proxy: ProviderProxyDefinition | None = None,
) -> ProbeOutcome:
    url = responses_url(base_url)
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }
    body = build_provider_probe_request()
    started_at = time.monotonic()
    try:
        proxy_url = await resolve_provider_proxy_url(proxy)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PROBE_TIMEOUT_S),
            proxy=proxy_url,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(url, json=body, headers=headers)
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
        try:
            payload = response.json()
            text = extract_response_output_text(payload)
        except Exception:  # noqa: BLE001
            text = extract_sse_output_text(response.text)
            if not text:
                return ProbeOutcome(
                    ok=False,
                    latency_ms=latency,
                    error="bad_json",
                    http_status=response.status_code,
                    capability_signal=None,
                )
        if "9801" in text:
            return ProbeOutcome(
                ok=True,
                latency_ms=latency,
                error=None,
                http_status=response.status_code,
                capability_signal="supported",
            )
        return ProbeOutcome(
            ok=False,
            latency_ms=latency,
            error="wrong_answer",
            http_status=response.status_code,
            capability_signal=None,
        )
    except httpx.TimeoutException:
        latency = int((time.monotonic() - started_at) * 1000)
        return ProbeOutcome(
            ok=False,
            latency_ms=latency,
            error="timeout",
            http_status=None,
            capability_signal="transient",
        )
    except Exception as exc:
        latency = int((time.monotonic() - started_at) * 1000)
        message = truncate_probe_error(str(exc))
        error = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
        return ProbeOutcome(
            ok=False,
            latency_ms=latency,
            error=error,
            http_status=None,
            capability_signal=None,
        )


def probe_blocked_by_endpoint_lock(item: dict[str, Any]) -> bool:
    endpoint = normalize_image_jobs_endpoint(item.get("image_jobs_endpoint"))
    if endpoint == "auto":
        return False
    probe_view = {
        "image_jobs_endpoint": endpoint,
        "image_jobs_endpoint_lock": normalize_image_jobs_endpoint_lock(
            item.get("image_jobs_endpoint_lock"), endpoint
        ),
    }
    return not endpoint_kind_allowed(probe_view, "responses")
