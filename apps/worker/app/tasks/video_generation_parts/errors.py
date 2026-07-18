"""Video upstream error classification and diagnostic helpers."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...video_upstream import VideoUpstreamError


SUBMIT_RETRY_DELAYS_S = (8, 24, 60)
RETRYABLE_VIDEO_ERROR_CODES = {
    "capacity",
    "fetch_failed",
    "provider_error",
    "upstream_network_error",
    "upstream_not_ready",
    "upstream_timeout",
    "upstream_unknown",
}


def video_exception_code(exc: Exception, *, default: str) -> str:
    if isinstance(exc, VideoUpstreamError):
        value = (exc.error_code or "").strip()
        return value or default
    raw_code = getattr(exc, "error_code", None)
    if isinstance(raw_code, str) and raw_code.strip():
        return raw_code.strip()[:64]
    if isinstance(exc, httpx.TimeoutException) or isinstance(exc, asyncio.TimeoutError):
        return "upstream_timeout"
    if isinstance(exc, httpx.TransportError):
        return "upstream_network_error"
    return default


def video_exception_message(exc: Exception, *, phase: str) -> str:
    raw = str(exc).strip()
    if raw:
        return raw[:1000]
    code = video_exception_code(exc, default="provider_unavailable")
    status_code = getattr(exc, "status_code", None)
    suffix = f" status={status_code}" if status_code else ""
    return f"video upstream {phase} failed: {code} ({exc.__class__.__name__}){suffix}"[
        :1000
    ]


def is_retryable_video_exception(exc: Exception) -> bool:
    if isinstance(exc, VideoUpstreamError):
        if exc.status_code in {408, 409, 425, 429}:
            return True
        if exc.status_code is not None and exc.status_code >= 500:
            return True
        return exc.error_code in RETRYABLE_VIDEO_ERROR_CODES
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return True
    return isinstance(exc, httpx.TransportError)


def submit_outcome_unknown(exc: Exception) -> bool:
    if isinstance(
        exc, (httpx.TimeoutException, asyncio.TimeoutError, httpx.TransportError)
    ):
        return True
    if not isinstance(exc, VideoUpstreamError):
        return False
    if exc.status_code in {408, 409}:
        return True
    if exc.status_code is not None and exc.status_code >= 500:
        return True
    return exc.error_code in {"bad_response", "upstream_unknown"}


def submit_retry_delay_s(attempt: int) -> int:
    index = max(0, min(attempt - 1, len(SUBMIT_RETRY_DELAYS_S) - 1))
    return SUBMIT_RETRY_DELAYS_S[index]


def generation_attempt(generation: Any) -> int:
    return int(getattr(generation, "attempt", 0) or 0)


def generation_diagnostics(generation: Any) -> dict[str, Any]:
    raw = getattr(generation, "diagnostics", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def submit_failure_billable_hint(exc: Exception) -> bool | None:
    if is_retryable_video_exception(exc):
        return None
    if isinstance(exc, VideoUpstreamError) and exc.error_code in {
        "bad_response",
        "upstream_unknown",
    }:
        return None
    return False


def exception_log_info(exc: Exception):
    return (type(exc), exc, exc.__traceback__)


def append_bounded_history(
    diagnostics: dict[str, Any], key: str, item: dict[str, Any], *, limit: int = 10
) -> None:
    raw = diagnostics.get(key)
    history = list(raw) if isinstance(raw, list) else []
    history.append(item)
    diagnostics[key] = history[-limit:]
