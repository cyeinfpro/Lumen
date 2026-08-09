"""Completion-specific billing estimates used during task execution."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core import billing as billing_core
from lumen_core.models import Completion
from lumen_core.pricing import (
    UsageTokens,
    missing_pricing_buckets,
    model_pricing_from_snapshot,
)
from lumen_core.pricing_resolver import (
    PRICING_MODE_STRICT_BILLING,
    PricingResolver,
)

from .billing_parts import helpers as billing_helpers
from .billing_parts import rate_multipliers


logger = logging.getLogger(__name__)


def image_output_tokens_for_budget(
    budget_micro: int,
    *,
    image_output_per_1k_micro: int,
    rate_multiplier_x10000: int = 10_000,
) -> int:
    budget = max(0, int(budget_micro or 0))
    rate = max(0, int(image_output_per_1k_micro or 0))
    multiplier = max(0, int(rate_multiplier_x10000 or 0))
    if budget <= 0:
        return 0
    if rate <= 0 or multiplier <= 0:
        raise billing_core.BillingError(
            "PRICING_MISSING",
            "image output rate or billing multiplier is unavailable",
            503,
        )
    denominator = rate * multiplier
    return max(1, (budget * 1000 * 10_000 + denominator - 1) // denominator)


def mark_completion_billing_pending(
    completion: Completion,
    *,
    reason: str,
    usage_unknown: bool = False,
) -> None:
    billing_helpers.mark_completion_billing_pending(
        completion,
        reason=reason,
        usage_unknown=usage_unknown,
    )


def completion_billing_pending(completion: Completion) -> bool:
    return billing_helpers.completion_billing_pending(completion)


async def _completion_tool_image_pricing(
    session: Any,
    completion: Completion,
) -> Any:
    upstream_request = getattr(completion, "upstream_request", None)
    snapshot = (
        upstream_request.get("billing_pricing_snapshot")
        if isinstance(upstream_request, dict)
        else None
    )
    if isinstance(snapshot, dict):
        pricing = model_pricing_from_snapshot(snapshot)
    else:
        pricing = await PricingResolver().resolve(
            session,
            getattr(completion, "model", ""),
            mode=PRICING_MODE_STRICT_BILLING,
        )
    missing = missing_pricing_buckets(
        pricing,
        UsageTokens(input_tokens=0, output_tokens=0, image_output_tokens=1),
    )
    if missing:
        raise billing_core.BillingError(
            "PRICING_MISSING",
            "missing enabled image output pricing rule",
            503,
        )
    return pricing.with_defaults()


async def fallback_completion_tool_image_tokens(
    session: Any,
    completion: Completion,
    *,
    budget_micro: int,
) -> int:
    budget = max(0, int(budget_micro or 0))
    if budget <= 0:
        return 0
    try:
        pricing = await _completion_tool_image_pricing(session, completion)
        rate_multiplier = await rate_multipliers.completion_rate_multiplier_x10000(
            session,
            completion,
        )
        return image_output_tokens_for_budget(
            budget,
            image_output_per_1k_micro=pricing.image_output_per_1k_micro,
            rate_multiplier_x10000=rate_multiplier,
        )
    except (billing_core.BillingError, ValueError) as exc:
        reason = getattr(exc, "code", type(exc).__name__)
        mark_completion_billing_pending(
            completion,
            reason=f"tool_image_pricing:{reason}",
        )
        logger.warning(
            "completion tool image pricing pending comp=%s reason=%s",
            getattr(completion, "id", None),
            reason,
            exc_info=True,
        )
        return 0
