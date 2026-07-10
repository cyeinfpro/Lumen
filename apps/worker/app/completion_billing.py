"""Completion-specific billing estimates used during task execution."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core.models import Completion
from lumen_core.pricing_resolver import PricingResolver

from . import billing as worker_billing


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
        return 1
    denominator = rate * multiplier
    return max(1, (budget * 1000 * 10_000 + denominator - 1) // denominator)


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
        pricing = (
            await PricingResolver().resolve(session, getattr(completion, "model", ""))
        ).with_defaults()
        rate_multiplier = await worker_billing.completion_rate_multiplier_x10000(
            session,
            completion,
        )
        return image_output_tokens_for_budget(
            budget,
            image_output_per_1k_micro=pricing.image_output_per_1k_micro,
            rate_multiplier_x10000=rate_multiplier,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "completion tool image fallback usage estimate failed comp=%s",
            getattr(completion, "id", None),
            exc_info=True,
        )
        return 1
