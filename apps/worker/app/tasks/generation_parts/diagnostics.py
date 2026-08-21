"""Generation diagnostics shaping, redaction, and timing helpers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NotRequired, TypedDict, Unpack


DIAG_STRING_LIMIT = 500
DIAG_COLLECTION_LIMIT = 20
PROVIDER_ATTEMPT_ERROR_LIMIT = 300
PRIVATE_DIAGNOSTIC_KEYS = frozenset(
    {
        "provider",
        "actual_provider",
        "actual_endpoint",
        "proxy_name",
        "proxy_enabled",
        "debug_id",
    }
)
PRIVATE_PROVIDER_ATTEMPT_KEYS = frozenset(
    {
        "provider",
        "actual_provider",
        "endpoint",
        "actual_endpoint",
        "proxy_name",
        "proxy_enabled",
    }
)
PRIVATE_PROVIDER_PROGRESS_KEYS = PRIVATE_PROVIDER_ATTEMPT_KEYS | {
    "from_provider",
    "from_endpoint",
}
PROVIDER_ATTEMPT_PROGRESS_KEYS = (
    "attempt",
    "endpoint_attempt",
    "duration_ms",
    "reason",
    "error_code",
    "status_code",
    "byok",
    "source",
    "endpoint",
    "from_endpoint",
)

RedisText = Callable[[Any], str | None]


class StageTimer:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.timings_ms: dict[str, int] = {}
        self._monotonic = monotonic

    def set_ms(self, name: str, value_ms: int | float | None) -> None:
        if value_ms is None:
            return
        self.timings_ms[name] = max(0, int(value_ms))

    def add_elapsed(self, name: str, started: float) -> None:
        elapsed_ms = int(max(0.0, self._monotonic() - started) * 1000)
        self.timings_ms[name] = self.timings_ms.get(name, 0) + elapsed_ms

    def snapshot(self) -> dict[str, int]:
        return dict(self.timings_ms)


def generation_trace_id(
    task_id: str,
    upstream_request: dict[str, Any] | None,
) -> str:
    raw = (
        upstream_request.get("trace_id") if isinstance(upstream_request, dict) else None
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"gen_{task_id}"


def queue_wait_ms(
    created_at: datetime | None,
    *,
    now: datetime | None = None,
    now_factory: Callable[[], datetime] | None = None,
) -> int | None:
    if not isinstance(created_at, datetime):
        return None
    current = now or (
        now_factory() if now_factory is not None else datetime.now(timezone.utc)
    )
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return int(
        max(0.0, (current - created_at.astimezone(timezone.utc)).total_seconds()) * 1000
    )


def is_byok_provider_name(name: Any) -> bool:
    return isinstance(name, str) and name.startswith("user:")


def provider_attempt_from_progress(
    event: dict[str, Any],
    *,
    status: str,
    attempt_epoch: int,
    redis_text: RedisText,
    is_byok_provider: Callable[[Any], bool] = is_byok_provider_name,
    provider_key: str = "provider",
    route_default: str | None = None,
) -> dict[str, Any]:
    provider = redis_text(event.get(provider_key))
    attempt: dict[str, Any] = {
        "provider": provider,
        "route": event.get("route") or route_default,
        "status": status,
        "attempt": event.get("attempt") or attempt_epoch,
        "byok": bool(event.get("byok")) or is_byok_provider(provider),
    }
    reason = redis_text(event.get("reason"))
    if reason:
        attempt["reason"] = reason
        attempt["error_summary"] = reason
    for key in PROVIDER_ATTEMPT_PROGRESS_KEYS:
        value = event.get(key)
        if value is not None and key not in attempt:
            attempt[key] = value
    return {key: value for key, value in attempt.items() if value is not None}


def compact_diag_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:DIAG_STRING_LIMIT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [compact_diag_value(item) for item in value[:DIAG_COLLECTION_LIMIT]]
    if isinstance(value, dict):
        return {
            str(key): compact_diag_value(item)
            for key, item in list(value.items())[:DIAG_COLLECTION_LIMIT]
        }
    return value


def compact_provider_attempt(
    attempt: dict[str, Any],
    *,
    expose_provider_diagnostics: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attempt.items():
        key_text = str(key)
        if (
            not expose_provider_diagnostics
            and key_text in PRIVATE_PROVIDER_ATTEMPT_KEYS
        ):
            continue
        compacted = compact_diag_value(value)
        if key_text == "error_summary" and isinstance(compacted, str):
            compacted = compacted.replace("\n", " ")[:PROVIDER_ATTEMPT_ERROR_LIMIT]
        out[key_text] = compacted
    return out


def compact_provider_attempts(
    attempts: list[dict[str, Any]] | None,
    *,
    expose_provider_diagnostics: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        compacted = compact_provider_attempt(
            attempt,
            expose_provider_diagnostics=expose_provider_diagnostics,
        )
        if compacted:
            out.append(compacted)
        if len(out) >= 12:
            break
    return out


def sanitize_generation_diagnostics_payload(
    diagnostics: dict[str, Any],
    *,
    expose_provider_diagnostics: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in diagnostics.items():
        if not expose_provider_diagnostics and key in PRIVATE_DIAGNOSTIC_KEYS:
            continue
        if key == "provider_attempts" and isinstance(value, list):
            attempts = compact_provider_attempts(
                value,
                expose_provider_diagnostics=expose_provider_diagnostics,
            )
            if attempts:
                out[key] = attempts
            continue
        out[key] = compact_diag_value(value)
    return {key: value for key, value in out.items() if value is not None}


def sanitize_generation_upstream_request(
    upstream_request: dict[str, Any],
    *,
    expose_provider_diagnostics: bool,
) -> dict[str, Any]:
    out = dict(upstream_request)
    if not expose_provider_diagnostics:
        for key in PRIVATE_DIAGNOSTIC_KEYS:
            out.pop(key, None)
    attempts = out.get("provider_attempts")
    if isinstance(attempts, list):
        compact_attempts = compact_provider_attempts(
            attempts,
            expose_provider_diagnostics=expose_provider_diagnostics,
        )
        if compact_attempts:
            out["provider_attempts"] = compact_attempts
        else:
            out.pop("provider_attempts", None)
    diagnostics = out.get("generation_diagnostics")
    if isinstance(diagnostics, dict):
        out["generation_diagnostics"] = sanitize_generation_diagnostics_payload(
            diagnostics,
            expose_provider_diagnostics=expose_provider_diagnostics,
        )
    return out


def request_event_provider_from_attempts(
    attempts: list[dict[str, Any]] | None,
    *,
    redis_text: RedisText,
) -> str | None:
    """Return the best provider for admin request events after redaction."""
    fallback: str | None = None
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        provider = redis_text(attempt.get("provider") or attempt.get("actual_provider"))
        if not provider or provider in {"dual_race", "dual_race_bonus"}:
            continue
        status = str(attempt.get("status") or "").strip().lower()
        if status == "used":
            return provider
        fallback = provider
    return fallback


def sanitize_provider_progress_payload(
    payload: dict[str, Any],
    *,
    expose_provider_diagnostics: bool,
) -> dict[str, Any]:
    out = dict(payload)
    if not expose_provider_diagnostics:
        for key in PRIVATE_PROVIDER_PROGRESS_KEYS:
            out.pop(key, None)
    reason = out.get("reason")
    if reason is not None:
        compacted = compact_diag_value(reason)
        if isinstance(compacted, str):
            compacted = compacted.replace("\n", " ")[:PROVIDER_ATTEMPT_ERROR_LIMIT]
        out["reason"] = compacted
    return {key: value for key, value in out.items() if value is not None}


def image_requested_params_snapshot(
    upstream_request: dict[str, Any] | None,
    *,
    size: str,
    aspect_ratio: str,
    action: str,
    input_count: int,
    has_mask: bool,
) -> dict[str, Any]:
    req = upstream_request if isinstance(upstream_request, dict) else {}
    out: dict[str, Any] = {
        "size": size,
        "aspect_ratio": aspect_ratio,
        "action": action,
        "input_image_count": input_count,
        "has_mask": has_mask,
    }
    for key in (
        "responses_model",
        "render_quality",
        "output_format",
        "output_compression",
        "background",
        "moderation",
        "billing_tier",
        "n",
    ):
        if key in req:
            out[key] = compact_diag_value(req[key])
    return out


def image_effective_params_snapshot(
    image_request_options: dict[str, Any] | None,
    *,
    size: str,
    width: int | None = None,
    height: int | None = None,
    mime: str | None = None,
) -> dict[str, Any]:
    opts = image_request_options if isinstance(image_request_options, dict) else {}
    out: dict[str, Any] = {"size": size}
    for key in (
        "responses_model",
        "render_quality",
        "output_format",
        "output_compression",
        "background",
        "moderation",
    ):
        if key in opts:
            out[key] = compact_diag_value(opts[key])
    if width and height:
        out["size_actual"] = f"{width}x{height}"
    if mime:
        out["mime"] = mime
    return out


def safe_generation_error_summary(
    *,
    code: str | None,
    message: str | None,
    status_code: Any = None,
) -> str:
    parts: list[str] = []
    if code:
        parts.append(str(code)[:120])
    if status_code is not None:
        parts.append(f"http {status_code}")
    if message:
        parts.append(str(message).replace("\n", " ")[:300])
    return " · ".join(parts) if parts else "unknown generation error"


class _GenerationDiagnosticsArgs(TypedDict):
    trace_id: NotRequired[str | None]
    requested_params: dict[str, Any]
    effective_params: NotRequired[dict[str, Any] | None]
    revised_prompt: NotRequired[str | None]
    provider: NotRequired[str | None]
    upstream_route: NotRequired[str | None]
    actual_route: NotRequired[str | None]
    actual_source: NotRequired[str | None]
    actual_endpoint: NotRequired[str | None]
    provider_attempts: NotRequired[list[dict[str, Any]] | None]
    stage_timings_ms: NotRequired[dict[str, int] | None]
    route_diagnostics: NotRequired[list[dict[str, Any]] | None]
    upstream_duration_ms: NotRequired[int | None]
    duration_ms: NotRequired[int | None]
    debug_id: NotRequired[str | None]
    error_summary: NotRequired[Any]
    expose_provider_diagnostics: NotRequired[bool]


@dataclass(frozen=True)
class _GenerationDiagnostics:
    requested_params: dict[str, Any]
    trace_id: str | None = None
    effective_params: dict[str, Any] | None = None
    revised_prompt: str | None = None
    provider: str | None = None
    upstream_route: str | None = None
    actual_route: str | None = None
    actual_source: str | None = None
    actual_endpoint: str | None = None
    provider_attempts: list[dict[str, Any]] | None = None
    stage_timings_ms: dict[str, int] | None = None
    route_diagnostics: list[dict[str, Any]] | None = None
    upstream_duration_ms: int | None = None
    duration_ms: int | None = None
    debug_id: str | None = None
    error_summary: Any = None
    expose_provider_diagnostics: bool = False


def build_generation_diagnostics(
    **kwargs: Unpack[_GenerationDiagnosticsArgs],
) -> dict[str, Any]:
    diagnostics = _GenerationDiagnostics(**kwargs)
    attempts = diagnostics.provider_attempts or []
    failover_count = sum(
        1
        for attempt in attempts
        if str(attempt.get("status") or "").lower() in {"failover", "failed"}
    )
    out: dict[str, Any] = {
        "requested_params": diagnostics.requested_params,
        "debug_id": diagnostics.debug_id,
    }
    _apply_optional_diagnostic_fields(
        out,
        trace_id=diagnostics.trace_id,
        effective_params=diagnostics.effective_params,
        revised_prompt=diagnostics.revised_prompt,
        upstream_route=diagnostics.upstream_route,
        actual_route=diagnostics.actual_route,
        actual_source=diagnostics.actual_source,
        actual_endpoint=diagnostics.actual_endpoint,
        provider_attempts=attempts[:12] if attempts else None,
        stage_timings_ms=diagnostics.stage_timings_ms,
        route_diagnostics=(
            diagnostics.route_diagnostics[:12]
            if diagnostics.route_diagnostics
            else None
        ),
        safe_error_summary=diagnostics.error_summary,
        error_summary=diagnostics.error_summary,
    )
    if diagnostics.provider:
        out["provider"] = diagnostics.provider
        out["actual_provider"] = diagnostics.provider
    if failover_count:
        out["failover"] = True
        out["failover_count"] = failover_count
    if diagnostics.upstream_duration_ms is not None:
        out["upstream_duration_ms"] = diagnostics.upstream_duration_ms
    if diagnostics.duration_ms is not None:
        out["duration_ms"] = diagnostics.duration_ms
    return sanitize_generation_diagnostics_payload(
        out,
        expose_provider_diagnostics=diagnostics.expose_provider_diagnostics,
    )


def _apply_optional_diagnostic_fields(
    output: dict[str, Any],
    **fields: Any,
) -> None:
    for key, value in fields.items():
        if value:
            output[key] = value
