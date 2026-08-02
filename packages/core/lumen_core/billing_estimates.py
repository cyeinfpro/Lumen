"""Billing 成本估算与定价快照(image/completion)。

从 lumen_core/billing.py 拆出,保持主文件在 general module 行数上限内。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .billing_values import BillingError, tier_for_pixels
from .model_entities import PricingRule
from .pricing import (
    CostBreakdown,
    ModelPricing,
    UsageTokens,
    compute_breakdown,
    missing_pricing_buckets,
    model_pricing_from_snapshot,
)
from .pricing_resolver import PricingResolver


async def pricing_price_micro(
    db: AsyncSession,
    *,
    scope: str,
    key: str,
    unit: str,
    variant: str = "default",
) -> int | None:
    return (
        await db.execute(
            select(PricingRule.price_micro).where(
                PricingRule.scope == scope,
                PricingRule.key == key,
                PricingRule.variant == variant,
                PricingRule.unit == unit,
                PricingRule.enabled.is_(True),
            )
        )
    ).scalar_one_or_none()


async def estimate_image_cost(
    db: AsyncSession,
    *,
    size_px: int,
    n: int = 1,
    thresholds: dict[str, int] | None = None,
) -> tuple[int, str]:
    tier = tier_for_pixels(size_px, thresholds)
    unit = await pricing_price_micro(db, scope="image_size", key=tier, unit="per_image")
    if unit is None or int(unit) <= 0:
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled image pricing rule for {tier}",
            503,
        )
    return int(unit) * max(1, int(n)), tier


async def estimate_image_cost_for_tier(
    db: AsyncSession,
    *,
    tier: str,
    n: int = 1,
) -> tuple[int, str]:
    unit = await pricing_price_micro(db, scope="image_size", key=tier, unit="per_image")
    if unit is None or int(unit) <= 0:
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled image pricing rule for {tier}",
            503,
        )
    return int(unit) * max(1, int(n)), tier


async def estimate_completion_cost(
    db: AsyncSession,
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    reasoning_tokens: int = 0,
    image_output_tokens: int = 0,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
) -> int:
    breakdown = await estimate_completion_breakdown(
        db,
        model=model,
        tokens=UsageTokens(
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_creation_5m_tokens=cache_creation_5m_tokens,
            cache_creation_1h_tokens=cache_creation_1h_tokens,
            reasoning_tokens=reasoning_tokens,
            image_output_tokens=image_output_tokens,
        ),
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=service_tier,
    )
    return breakdown.actual_cost_micro


async def estimate_completion_breakdown(
    db: AsyncSession,
    *,
    model: str,
    tokens: UsageTokens,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
    channel: str | None = None,
    resolver: PricingResolver | None = None,
) -> CostBreakdown:
    pricing = await (resolver or PricingResolver()).resolve(db, model, channel=channel)
    missing_buckets = missing_pricing_buckets(
        pricing,
        tokens,
        service_tier=service_tier,
    )
    if pricing.pricing_source == "missing" or missing_buckets:
        detail = (
            f"; missing rates for {', '.join(missing_buckets)}"
            if missing_buckets
            else ""
        )
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled chat pricing rule for {model}{detail}",
            503,
        )
    if int(rate_multiplier_x10000) < 0:
        raise BillingError(
            "PRICING_MISSING",
            f"negative billing multiplier for {model}",
            503,
        )
    return compute_breakdown(
        pricing,
        tokens,
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=service_tier,
    )


async def completion_pricing_snapshot(
    db: AsyncSession,
    *,
    model: str,
    service_tier: str = "standard",
    channel: str | None = None,
    resolver: PricingResolver | None = None,
) -> dict[str, Any]:
    pricing = await (resolver or PricingResolver()).resolve(
        db,
        model,
        channel=channel,
    )
    probe_usage = UsageTokens(input_tokens=1, output_tokens=1)
    missing_buckets = missing_pricing_buckets(
        pricing,
        probe_usage,
        service_tier=service_tier,
    )
    if pricing.pricing_source == "missing" or missing_buckets:
        detail = (
            f"; missing rates for {', '.join(missing_buckets)}"
            if missing_buckets
            else ""
        )
        raise BillingError(
            "PRICING_MISSING",
            f"missing enabled chat pricing rule for {model}{detail}",
            503,
        )
    return pricing.with_defaults().model_dump()


def completion_breakdown_from_snapshot(
    snapshot: dict[str, Any],
    *,
    model: str,
    tokens: UsageTokens,
    rate_multiplier_x10000: int = 10_000,
    service_tier: str = "standard",
) -> CostBreakdown:
    try:
        pricing: ModelPricing = model_pricing_from_snapshot(snapshot)
    except ValueError as exc:
        raise BillingError(
            "PRICING_SNAPSHOT_INVALID",
            f"invalid billing pricing snapshot for {model}",
            500,
        ) from exc
    missing_buckets = missing_pricing_buckets(
        pricing,
        tokens,
        service_tier=service_tier,
    )
    if missing_buckets:
        raise BillingError(
            "PRICING_SNAPSHOT_INVALID",
            (
                f"billing pricing snapshot for {model} is missing rates for "
                f"{', '.join(missing_buckets)}"
            ),
            500,
        )
    if int(rate_multiplier_x10000) < 0:
        raise BillingError(
            "PRICING_SNAPSHOT_INVALID",
            f"negative billing multiplier for {model}",
            500,
        )
    return compute_breakdown(
        pricing,
        tokens,
        rate_multiplier_x10000=rate_multiplier_x10000,
        service_tier=service_tier,
    )
