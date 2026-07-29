"""Stateless worker billing settings, snapshots, and reference helpers."""

from __future__ import annotations

from typing import Any

from lumen_core import billing as billing_core
from lumen_core.models import Completion, Generation

from .. import runtime_settings


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


def snapshot_rate_multiplier_x10000(task: Generation | Completion) -> int | None:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None
    raw = upstream_request.get("billing_rate_multiplier_x10000")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def completion_service_tier(completion: Completion) -> str:
    upstream_request = getattr(completion, "upstream_request", None)
    if isinstance(upstream_request, dict):
        raw = upstream_request.get("service_tier")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "standard"
