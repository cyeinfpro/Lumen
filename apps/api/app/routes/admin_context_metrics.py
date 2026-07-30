"""Pure context-health metric helpers for the admin route."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from lumen_core.utils import ensure_utc

CONTEXT_METRIC_FIELDS = (
    "summary_attempts",
    "summary_successes",
    "summary_failures",
    "manual_compact_calls",
    "cold_start_count",
)


def context_health_zero(
    *,
    degraded: bool = False,
    degrade_reason: str | None = None,
) -> dict:
    return {
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "circuit_breaker_state": "closed",
        "circuit_breaker_until": None,
        "last_24h": {
            "summary_attempts": 0,
            "summary_successes": 0,
            "summary_failures": 0,
            "summary_success_rate": 0.0,
            "summary_p50_latency_ms": 0,
            "summary_p95_latency_ms": 0,
            "manual_compact_calls": 0,
            "cold_start_count": 0,
            "fallback_reasons": {},
        },
    }


def redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def redis_int(value: Any) -> int:
    text = redis_text(value)
    if text is None or not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = math.floor(pos)
    upper = math.ceil(pos)
    if lower == upper:
        return ordered[lower]
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)
    return int(round(interpolated))


def extend_latency_samples(samples: list[int], raw: Any) -> None:
    text = redis_text(raw)
    if not text:
        return
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            samples.extend(redis_int(item) for item in parsed if redis_int(item) >= 0)
            return
    except Exception:
        pass
    for part in text.split(","):
        value = redis_int(part.strip())
        if value >= 0:
            samples.append(value)


def fold_context_metrics(rows: list[dict[Any, Any]]) -> dict:
    totals = {field: 0 for field in CONTEXT_METRIC_FIELDS}
    fallback_reasons: dict[str, int] = {}
    latency_samples: list[int] = []
    p50_values: list[int] = []
    p95_values: list[int] = []

    for row in rows:
        normalized = {str(redis_text(k) or ""): v for k, v in row.items()}
        for field in CONTEXT_METRIC_FIELDS:
            totals[field] += redis_int(normalized.get(field))

        for key, value in normalized.items():
            reason: str | None = None
            for prefix in (
                "fallback_reasons:",
                "fallback_reason:",
                "fallback:",
                "fallback_reasons.",
                "fallback_reason.",
            ):
                if key.startswith(prefix):
                    reason = key[len(prefix) :]
                    break
            if reason:
                fallback_reasons[reason] = fallback_reasons.get(reason, 0) + redis_int(
                    value
                )

        extend_latency_samples(
            latency_samples, normalized.get("summary_latency_ms_samples")
        )
        extend_latency_samples(
            latency_samples, normalized.get("summary_latency_samples")
        )
        p50 = redis_int(normalized.get("summary_p50_latency_ms"))
        p95 = redis_int(normalized.get("summary_p95_latency_ms"))
        if p50:
            p50_values.append(p50)
        if p95:
            p95_values.append(p95)

    attempts = totals["summary_attempts"]
    successes = totals["summary_successes"]
    success_rate = round(successes / attempts, 3) if attempts > 0 else 0.0
    return {
        **totals,
        "summary_success_rate": success_rate,
        "summary_p50_latency_ms": percentile(latency_samples, 0.50)
        if latency_samples
        else percentile(p50_values, 0.50),
        "summary_p95_latency_ms": percentile(latency_samples, 0.95)
        if latency_samples
        else percentile(p95_values, 0.95),
        "fallback_reasons": fallback_reasons,
    }


def hourly_context_metric_keys(now: datetime) -> list[str]:
    current_hour = now.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    return [
        f"context:metrics:hourly:{(current_hour - timedelta(hours=offset)).strftime('%Y%m%d%H')}"
        for offset in range(24)
    ]


def iso_z(dt: datetime) -> str:
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")
