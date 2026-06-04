"""Video-generation pricing helpers.

Seedance returns billable usage after the async task finishes, so video uses a
conservative hold followed by actual-token settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .billing import pricing_price_micro

VIDEO_PRICING_SCOPE = "video"
VIDEO_PRICING_UNIT = "per_mtoken"


class VideoBillingError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class VideoCostEstimate:
    estimated_tokens: int
    hold_micro: int
    unit_price_micro: int
    source: str


def round_micro_for_tokens(total_tokens: int, price_per_mtoken_micro: int) -> int:
    if total_tokens < 0:
        raise ValueError("total_tokens must not be negative")
    if price_per_mtoken_micro < 0:
        raise ValueError("price_per_mtoken_micro must not be negative")
    value = (
        Decimal(int(total_tokens))
        * Decimal(int(price_per_mtoken_micro))
        / Decimal(1_000_000)
    )
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def estimate_key(*, resolution: str, duration_s: int) -> str:
    return f"{resolution}:{int(duration_s)}"


def token_upper_bound(
    estimates: dict[str, Any],
    *,
    model: str,
    action: str,
    resolution: str,
    duration_s: int,
) -> int | None:
    model_map = estimates.get(model)
    if not isinstance(model_map, dict):
        return None
    action_map = model_map.get(action)
    if not isinstance(action_map, dict):
        return None
    value = action_map.get(estimate_key(resolution=resolution, duration_s=duration_s))
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


async def estimate_video_cost(
    db: AsyncSession,
    *,
    model: str,
    action: str,
    resolution: str,
    duration_s: int,
    fps: int | None = None,
    generate_audio: bool = False,
    estimates: dict[str, Any],
) -> VideoCostEstimate:
    del fps, generate_audio
    unit_price = await pricing_price_micro(
        db,
        scope=VIDEO_PRICING_SCOPE,
        key=model,
        variant=action,
        unit=VIDEO_PRICING_UNIT,
    )
    if unit_price is None:
        raise VideoBillingError(
            "video_pricing_missing",
            f"missing enabled video pricing rule for {model}/{action}",
            503,
        )
    tokens = token_upper_bound(
        estimates,
        model=model,
        action=action,
        resolution=resolution,
        duration_s=duration_s,
    )
    if tokens is None:
        raise VideoBillingError(
            "video_estimate_missing",
            f"missing video token hold estimate for {model}/{action}/{resolution}:{duration_s}",
            503,
        )
    return VideoCostEstimate(
        estimated_tokens=tokens,
        hold_micro=round_micro_for_tokens(tokens, int(unit_price)),
        unit_price_micro=int(unit_price),
        source="video.token_hold_estimates",
    )


async def settle_video_cost(
    db: AsyncSession,
    *,
    model: str,
    action: str,
    actual_total_tokens: int,
) -> int:
    unit_price = await pricing_price_micro(
        db,
        scope=VIDEO_PRICING_SCOPE,
        key=model,
        variant=action,
        unit=VIDEO_PRICING_UNIT,
    )
    if unit_price is None:
        raise VideoBillingError(
            "video_pricing_missing",
            f"missing enabled video pricing rule for {model}/{action}",
            503,
        )
    return round_micro_for_tokens(int(actual_total_tokens), int(unit_price))


__all__ = [
    "VIDEO_PRICING_SCOPE",
    "VIDEO_PRICING_UNIT",
    "VideoBillingError",
    "VideoCostEstimate",
    "estimate_key",
    "estimate_video_cost",
    "round_micro_for_tokens",
    "settle_video_cost",
    "token_upper_bound",
]
