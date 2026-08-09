"""Stateless worker billing settings, snapshots, and reference helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lumen_core import billing as billing_core
from lumen_core.model_entities import (
    Completion,
    Generation,
)
from lumen_core.pricing import parse_canonical_nonnegative_int

from .. import runtime_settings


COMPLETION_BILLING_STATE_KEY = "completion_billing_state"
COMPLETION_BILLING_PENDING = "pending_reconciliation"
COMPLETION_BILLING_PENDING_REASON_KEY = "completion_billing_pending_reason"
COMPLETION_BILLING_PENDING_AT_KEY = "completion_billing_pending_at"
COMPLETION_BILLING_RECONCILE_ATTEMPTS_KEY = "completion_billing_reconcile_attempts"
COMPLETION_BILLING_RECONCILED_AT_KEY = "completion_billing_reconciled_at"
COMPLETION_BILLING_RECONCILED_SOURCE_KEY = "completion_billing_reconciled_source"
COMPLETION_USAGE_STATE_KEY = "completion_usage_state"
COMPLETION_USAGE_UNKNOWN = "unknown"
COMPLETION_USAGE_UNKNOWN_REASON_KEY = "completion_usage_unknown_reason"


async def setting_bool(key: str, default: bool = False) -> bool:
    return billing_core.parse_bool_setting(await runtime_settings.resolve(key), default)


async def billing_enabled() -> bool:
    return await setting_bool("billing.enabled", False)


async def allow_negative_balance() -> bool:
    return await setting_bool("billing.allow_negative_balance", False)


async def window_rate_limit_enabled() -> bool:
    return await setting_bool("billing.window_rate_limit", False)


async def cache_aware_enabled() -> bool:
    return await setting_bool("billing.cache_aware", True)


async def thresholds() -> dict[str, int]:
    return billing_core.parse_thresholds(
        await runtime_settings.resolve("billing.image_size_thresholds")
    )


def generation_billing_tier(generation: Generation) -> str | None:
    upstream_request = getattr(generation, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    tier = upstream_request.get("billing_tier")
    return tier if tier in {"1k", "2k", "4k"} else None


def task_pricing_snapshot(task: Generation | Completion) -> dict[str, Any] | None:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    snapshot = upstream_request.get("billing_pricing_snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def generation_snapshot_cost(
    generation: Generation,
    *,
    image_count: int,
) -> tuple[int, str] | None:
    snapshot = task_pricing_snapshot(generation)
    if not snapshot or snapshot.get("kind") != "image":
        return None
    try:
        unit_price = int(snapshot.get("unit_price_micro") or 0)
    except (TypeError, ValueError):
        return None
    tier = snapshot.get("tier")
    if unit_price <= 0 or not isinstance(tier, str) or not tier:
        return None
    return unit_price * max(1, int(image_count)), tier


def apply_rate_multiplier_micro(amount_micro: int, multiplier_x10000: int) -> int:
    amount = max(0, int(amount_micro or 0))
    multiplier = max(0, int(multiplier_x10000 or 0))
    if amount == 0 or multiplier == 0:
        return 0
    return max(1, (amount * multiplier) // 10_000)


def generation_billing_ref_id(generation: Generation) -> str:
    return billing_core.generation_billing_ref_id(generation)


def generation_billing_retry_count(generation: Generation) -> int:
    return billing_core.generation_billing_retry_count(generation)


def generation_settle_provider(generation: Generation) -> str | None:
    diagnostics = getattr(generation, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        return None
    for key in ("actual_provider", "provider"):
        value = diagnostics.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:64]
    return None


def completion_billing_ref_id(completion: Completion) -> str:
    return billing_core.completion_billing_ref_id(completion)


def completion_billing_retry_count(completion: Completion) -> int:
    return billing_core.completion_billing_retry_count(completion)


def snapshot_rate_multiplier_x10000(
    task: Generation | Completion,
    *,
    strict: bool = False,
) -> int | None:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    if "billing_rate_multiplier_x10000" not in upstream_request:
        return None
    raw = upstream_request.get("billing_rate_multiplier_x10000")
    value = parse_canonical_nonnegative_int(raw)
    max_value = int(billing_core.MAX_RATE_MULTIPLIER * 10_000)
    if value is not None and value <= max_value:
        return value
    if strict:
        raise billing_core.BillingError(
            "RATE_MULTIPLIER_INVALID",
            "billing rate multiplier snapshot is invalid",
            503,
        )
    return None


def completion_billing_pending(task: Any) -> bool:
    upstream_request = getattr(task, "upstream_request", None)
    return bool(
        isinstance(upstream_request, dict)
        and upstream_request.get(COMPLETION_BILLING_STATE_KEY)
        == COMPLETION_BILLING_PENDING
    )


def completion_billing_pending_reason(task: Any) -> str | None:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    reason = upstream_request.get(COMPLETION_BILLING_PENDING_REASON_KEY)
    return reason if isinstance(reason, str) and reason else None


def completion_usage_unknown(task: Any) -> bool:
    upstream_request = getattr(task, "upstream_request", None)
    return bool(
        isinstance(upstream_request, dict)
        and upstream_request.get(COMPLETION_USAGE_STATE_KEY) == COMPLETION_USAGE_UNKNOWN
    )


def mark_completion_billing_pending(
    task: Any,
    *,
    reason: str,
    usage_unknown: bool = False,
) -> None:
    upstream_request = getattr(task, "upstream_request", None)
    updated = dict(upstream_request) if isinstance(upstream_request, dict) else {}
    updated[COMPLETION_BILLING_STATE_KEY] = COMPLETION_BILLING_PENDING
    updated[COMPLETION_BILLING_PENDING_REASON_KEY] = str(reason)
    updated.setdefault(
        COMPLETION_BILLING_PENDING_AT_KEY,
        datetime.now(timezone.utc).isoformat(),
    )
    if usage_unknown:
        updated[COMPLETION_USAGE_STATE_KEY] = COMPLETION_USAGE_UNKNOWN
        updated[COMPLETION_USAGE_UNKNOWN_REASON_KEY] = str(reason)
    task.upstream_request = updated


def begin_completion_billing_reconciliation(task: Any) -> None:
    upstream_request = getattr(task, "upstream_request", None)
    updated = dict(upstream_request) if isinstance(upstream_request, dict) else {}
    try:
        attempts = max(
            0,
            int(updated.get(COMPLETION_BILLING_RECONCILE_ATTEMPTS_KEY) or 0),
        )
    except (TypeError, ValueError):
        attempts = 0
    updated[COMPLETION_BILLING_RECONCILE_ATTEMPTS_KEY] = attempts + 1
    updated.pop(COMPLETION_BILLING_STATE_KEY, None)
    task.upstream_request = updated


def mark_completion_billing_reconciled(
    task: Any,
    *,
    source: str,
) -> None:
    upstream_request = getattr(task, "upstream_request", None)
    updated = dict(upstream_request) if isinstance(upstream_request, dict) else {}
    for key in (
        COMPLETION_BILLING_STATE_KEY,
        COMPLETION_BILLING_PENDING_REASON_KEY,
        COMPLETION_BILLING_PENDING_AT_KEY,
        COMPLETION_USAGE_STATE_KEY,
        COMPLETION_USAGE_UNKNOWN_REASON_KEY,
    ):
        updated.pop(key, None)
    updated[COMPLETION_BILLING_RECONCILED_AT_KEY] = datetime.now(
        timezone.utc
    ).isoformat()
    updated[COMPLETION_BILLING_RECONCILED_SOURCE_KEY] = str(source)
    task.upstream_request = updated


def completion_service_tier(completion: Completion) -> str:
    upstream_request = getattr(completion, "upstream_request", None)
    if isinstance(upstream_request, dict):
        raw = upstream_request.get("service_tier")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "standard"
