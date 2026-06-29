"""Video-generation pricing helpers.

Seedance returns billable usage after the async task finishes, so video uses a
conservative hold followed by actual-token settlement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .billing import pricing_price_micro

VIDEO_PRICING_SCOPE = "video"
VIDEO_PRICING_UNIT = "per_mtoken"
SMART_VIDEO_DURATION_S = -1
SMART_VIDEO_HOLD_DURATION_S = 15
SUPPORTED_VIDEO_DURATIONS_S = tuple(range(3, 16))
VIDEO_BILLING_TOKENS_PER_SECOND = 1_000_000
VIDEO_REFERENCE_IMAGE_PRICING_VARIANT = "reference_image"
VIDEO_REFERENCE_VIDEO_PRICING_VARIANT = "reference_video"
VIDEO_LEGACY_REFERENCE_PRICING_VARIANT = "reference"
SEEDANCE_20_FAST_MODEL = "seedance-2.0-fast"
SEEDANCE_20_MINI_MODEL = "seedance-2.0-mini"
SEEDANCE_20_MODEL = "seedance-2.0"
_SEEDANCE_20_FAST_RE = re.compile(
    r"(?:seedance[-.]2[-.]0[-.]fast|video[-.]ds[-.]2[-.]0[-.]fast)"
)
_SEEDANCE_20_MINI_RE = re.compile(r"seedance[-.]2[-.]0[-.]mini")
VIDEO_PRICING_VARIANTS = (
    "t2v",
    "i2v",
    VIDEO_LEGACY_REFERENCE_PRICING_VARIANT,
    VIDEO_REFERENCE_IMAGE_PRICING_VARIANT,
    VIDEO_REFERENCE_VIDEO_PRICING_VARIANT,
)


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


def hold_estimate_duration_s(duration_s: int) -> int:
    if int(duration_s) == SMART_VIDEO_DURATION_S:
        return SMART_VIDEO_HOLD_DURATION_S
    return int(duration_s)


def _reference_kind(item: Any) -> str | None:
    if isinstance(item, Mapping):
        raw = item.get("kind")
    else:
        raw = getattr(item, "kind", None)
    return raw if isinstance(raw, str) else None


def video_resolution_pricing_variant(variant: str, resolution: str | None) -> str:
    resolution = (resolution or "").strip()
    if not resolution:
        return variant
    return f"{variant}_{resolution}"


def split_video_resolution_pricing_variant(
    raw: str,
) -> tuple[str, str | None]:
    if "_" not in raw:
        return raw, None
    variant, maybe_resolution = raw.rsplit("_", 1)
    normalized_resolution = maybe_resolution.strip().lower()
    if (
        normalized_resolution == "4k"
        or normalized_resolution.endswith("p")
        and normalized_resolution[:-1].isdigit()
    ):
        return variant, normalized_resolution
    return raw, None


def is_seedance_20_fast_identifier(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("_", "-")
        if _SEEDANCE_20_FAST_RE.search(value):
            return True
    return False


def is_seedance_20_mini_identifier(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("_", "-")
        if _SEEDANCE_20_MINI_RE.search(value):
            return True
    return False


def is_video_ds_20_standard_identifier(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("_", "-").replace(".", "-")
        if "video-ds-2-0" in value:
            return True
    return False


def video_billing_model(model: str, upstream_model: str | None = None) -> str:
    if is_seedance_20_fast_identifier(model, upstream_model):
        return SEEDANCE_20_FAST_MODEL
    if is_seedance_20_mini_identifier(model, upstream_model):
        return SEEDANCE_20_MINI_MODEL
    if is_video_ds_20_standard_identifier(model, upstream_model):
        return SEEDANCE_20_MODEL
    return model


def video_pricing_variant(
    action: str,
    reference_media: Iterable[Any] | None = None,
    *,
    resolution: str | None = None,
) -> str:
    if action != VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        return video_resolution_pricing_variant(action, resolution)
    if any(_reference_kind(item) == "video" for item in reference_media or ()):
        return video_resolution_pricing_variant(
            VIDEO_REFERENCE_VIDEO_PRICING_VARIANT, resolution
        )
    return video_resolution_pricing_variant(
        VIDEO_REFERENCE_IMAGE_PRICING_VARIANT, resolution
    )


def _pricing_fallback_variants(
    action: str,
    pricing_variant: str,
    resolution: str | None,
) -> tuple[str, ...]:
    base_variant, variant_resolution = split_video_resolution_pricing_variant(
        pricing_variant
    )
    lookup_resolution = variant_resolution or (resolution or "").strip() or None
    variants = [
        video_resolution_pricing_variant(base_variant, lookup_resolution),
        base_variant,
    ]
    if action == VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        variants.append(
            video_resolution_pricing_variant(
                VIDEO_LEGACY_REFERENCE_PRICING_VARIANT, lookup_resolution
            )
        )
        variants.append(VIDEO_LEGACY_REFERENCE_PRICING_VARIANT)
        if base_variant == VIDEO_REFERENCE_IMAGE_PRICING_VARIANT:
            variants.append(video_resolution_pricing_variant("i2v", lookup_resolution))
            variants.append("i2v")
    return tuple(dict.fromkeys(variants))


async def _video_unit_price_micro(
    db: AsyncSession,
    *,
    model: str,
    action: str,
    pricing_variant: str,
    resolution: str | None,
) -> tuple[int | None, str]:
    for variant in _pricing_fallback_variants(action, pricing_variant, resolution):
        unit_price = await pricing_price_micro(
            db,
            scope=VIDEO_PRICING_SCOPE,
            key=model,
            variant=variant,
            unit=VIDEO_PRICING_UNIT,
        )
        if unit_price is not None:
            return int(unit_price), variant
    return None, pricing_variant


def _parse_estimate_key(key: str, value: Any) -> tuple[str, int, int] | None:
    if not isinstance(key, str) or ":" not in key:
        return None
    resolution, duration = key.rsplit(":", 1)
    try:
        duration_s = int(duration)
        estimate = int(value)
    except (TypeError, ValueError):
        return None
    if not resolution or duration_s <= 0 or estimate <= 0 or isinstance(value, bool):
        return None
    return resolution, duration_s, estimate


def _ceil_scale(value: int, numerator: int, denominator: int) -> int:
    return (int(value) * int(numerator) + int(denominator) - 1) // int(denominator)


def _duration_estimate(entries: dict[int, int], duration_s: int) -> int | None:
    if duration_s in entries:
        return entries[duration_s]
    longer = sorted(item for item in entries.items() if item[0] >= duration_s)
    if longer:
        return longer[0][1]
    if not entries:
        return None
    base_duration, base_estimate = max(entries.items())
    return _ceil_scale(base_estimate, duration_s, base_duration)


def expand_video_duration_estimates(
    estimates: dict[str, Any],
    *,
    durations_s: tuple[int, ...] = SUPPORTED_VIDEO_DURATIONS_S,
) -> dict[str, Any]:
    """Fill missing 1-second duration buckets with conservative estimates."""
    expanded: dict[str, Any] = {}
    for model, model_value in estimates.items():
        if not isinstance(model, str) or not isinstance(model_value, dict):
            continue
        expanded_model: dict[str, Any] = {}
        for action, action_value in model_value.items():
            if not isinstance(action, str) or not isinstance(action_value, dict):
                continue
            by_resolution: dict[str, dict[int, int]] = {}
            for key, value in action_value.items():
                parsed = _parse_estimate_key(key, value)
                if parsed is None:
                    continue
                resolution, duration_s, estimate = parsed
                by_resolution.setdefault(resolution, {})[duration_s] = estimate
            expanded_action: dict[str, Any] = {}
            for resolution, duration_map in by_resolution.items():
                for duration_s in durations_s:
                    estimate = _duration_estimate(duration_map, duration_s)
                    if estimate is not None:
                        expanded_action[
                            estimate_key(resolution=resolution, duration_s=duration_s)
                        ] = estimate
            expanded_model[action] = expanded_action
        expanded[model] = expanded_model
    return expanded


def token_upper_bound(
    estimates: dict[str, Any],
    *,
    model: str,
    action: str,
    resolution: str,
    duration_s: int,
    pricing_variant: str | None = None,
) -> int | None:
    model_map = estimates.get(model)
    if not isinstance(model_map, dict):
        return None

    action_names: list[str] = []
    if pricing_variant:
        variant_action, _variant_resolution = split_video_resolution_pricing_variant(
            pricing_variant
        )
        action_names.append(variant_action)
    action_names.append(action)
    if action == VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        if action_names[0] == VIDEO_REFERENCE_VIDEO_PRICING_VARIANT:
            # A video reference has a separate official minimum-token schedule.
            # Falling back to image/reference estimates under-reserves 720p+ jobs.
            action_names = [VIDEO_REFERENCE_VIDEO_PRICING_VARIANT]
        else:
            action_names.extend(
                (
                    VIDEO_REFERENCE_IMAGE_PRICING_VARIANT,
                    "i2v",
                    "t2v",
                )
            )

    value = None
    key = estimate_key(
        resolution=resolution,
        duration_s=hold_estimate_duration_s(duration_s),
    )
    for action_name in tuple(dict.fromkeys(action_names)):
        action_map = model_map.get(action_name)
        if not isinstance(action_map, dict):
            continue
        value = action_map.get(key)
        if value is not None:
            break
    else:
        return None

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
    pricing_variant: str | None = None,
    reference_media: Iterable[Any] | None = None,
) -> VideoCostEstimate:
    del fps, generate_audio
    effective_pricing_variant = pricing_variant or video_pricing_variant(
        action, reference_media, resolution=resolution
    )
    unit_price, used_pricing_variant = await _video_unit_price_micro(
        db,
        model=model,
        action=action,
        pricing_variant=effective_pricing_variant,
        resolution=resolution,
    )
    if unit_price is None:
        raise VideoBillingError(
            "video_pricing_missing",
            f"missing enabled video pricing rule for {model}/{effective_pricing_variant}",
            503,
        )
    tokens = token_upper_bound(
        estimates,
        model=model,
        action=action,
        resolution=resolution,
        duration_s=duration_s,
        pricing_variant=effective_pricing_variant,
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
        source=f"video.token_hold_estimates:{used_pricing_variant}",
    )


async def settle_video_cost(
    db: AsyncSession,
    *,
    model: str,
    action: str,
    actual_total_tokens: int,
    resolution: str | None = None,
    pricing_variant: str | None = None,
    reference_media: Iterable[Any] | None = None,
    estimated_micro: int | None = None,
    max_estimate_multiplier: int = 3,
) -> int:
    effective_pricing_variant = pricing_variant or video_pricing_variant(
        action, reference_media, resolution=resolution
    )
    unit_price, _used_pricing_variant = await _video_unit_price_micro(
        db,
        model=model,
        action=action,
        pricing_variant=effective_pricing_variant,
        resolution=resolution,
    )
    if unit_price is None:
        raise VideoBillingError(
            "video_pricing_missing",
            f"missing enabled video pricing rule for {model}/{effective_pricing_variant}",
            503,
        )
    actual_micro = round_micro_for_tokens(int(actual_total_tokens), int(unit_price))
    if estimated_micro is not None:
        estimate = int(estimated_micro)
        if estimate > 0 and actual_micro > estimate * max(
            1, int(max_estimate_multiplier)
        ):
            return estimate
    return actual_micro


__all__ = [
    "VIDEO_PRICING_SCOPE",
    "VIDEO_PRICING_UNIT",
    "SMART_VIDEO_DURATION_S",
    "SMART_VIDEO_HOLD_DURATION_S",
    "SEEDANCE_20_FAST_MODEL",
    "SEEDANCE_20_MODEL",
    "SEEDANCE_20_MINI_MODEL",
    "SUPPORTED_VIDEO_DURATIONS_S",
    "VIDEO_BILLING_TOKENS_PER_SECOND",
    "VIDEO_LEGACY_REFERENCE_PRICING_VARIANT",
    "VIDEO_PRICING_VARIANTS",
    "VIDEO_REFERENCE_IMAGE_PRICING_VARIANT",
    "VIDEO_REFERENCE_VIDEO_PRICING_VARIANT",
    "VideoBillingError",
    "VideoCostEstimate",
    "estimate_key",
    "estimate_video_cost",
    "expand_video_duration_estimates",
    "hold_estimate_duration_s",
    "is_seedance_20_fast_identifier",
    "is_seedance_20_mini_identifier",
    "is_video_ds_20_standard_identifier",
    "round_micro_for_tokens",
    "settle_video_cost",
    "split_video_resolution_pricing_variant",
    "token_upper_bound",
    "video_billing_model",
    "video_resolution_pricing_variant",
    "video_pricing_variant",
]
