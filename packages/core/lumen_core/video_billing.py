"""Video-generation pricing helpers.

Seedance returns billable usage after the async task finishes, so video uses a
conservative hold followed by actual-token settlement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .billing import pricing_price_micro
from .pricing import MAX_BILLABLE_TOKENS, MAX_RATE_PER_1K_MICRO
from .video_providers import is_seedance_25_identifier

VIDEO_PRICING_SCOPE = "video"
VIDEO_PRICING_UNIT = "per_mtoken"
SMART_VIDEO_DURATION_S = -1
SMART_VIDEO_HOLD_DURATION_S = 15
SUPPORTED_VIDEO_DURATIONS_S = tuple(range(3, 16))
SEEDANCE_25_SMART_VIDEO_HOLD_DURATION_S = 30
SEEDANCE_25_SUPPORTED_VIDEO_DURATIONS_S = tuple(range(4, 31))
VIDEO_BILLING_TOKENS_PER_SECOND = 1_000_000
VIDEO_REFERENCE_IMAGE_PRICING_VARIANT = "reference_image"
VIDEO_REFERENCE_VIDEO_PRICING_VARIANT = "reference_video"
VIDEO_LEGACY_REFERENCE_PRICING_VARIANT = "reference"
SEEDANCE_25_MODEL = "seedance-2.5"
SEEDANCE_20_FAST_MODEL = "seedance-2.0-fast"
SEEDANCE_20_MINI_MODEL = "seedance-2.0-mini"
SEEDANCE_20_MODEL = "seedance-2.0"
_SEEDANCE_20_FAST_RE = re.compile(
    r"(?:seedance[-.]2[-.]0[-.]fast|video[-.]ds[-.]2[-.]0[-.]fast)"
)
_SEEDANCE_20_MINI_RE = re.compile(
    r"(?:seedance[-.]2[-.]0[-.]mini|video[-.]ds[-.]2[-.]0[-.]mini)"
)
VIDEO_PRICING_VARIANTS = (
    "t2v",
    "i2v",
    VIDEO_LEGACY_REFERENCE_PRICING_VARIANT,
    VIDEO_REFERENCE_IMAGE_PRICING_VARIANT,
    VIDEO_REFERENCE_VIDEO_PRICING_VARIANT,
)


class VideoBillingError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 422,
        *,
        actual_micro: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        # 当上游成本已经算出、只是超过预估倍数时携带该金额。上层据此仍能全额
        # 转嫁，而不会与"定价缺失、根本算不出"的情形混为一谈。
        self.actual_micro = actual_micro
        super().__init__(message)


def _guard_video_factor(field: str, value: int, limit: int) -> None:
    """因子越界直接抛错（审计 F-5）。

    与 pricing 侧同理：封顶会让超出部分由平台吸收，纯转嫁不允许。
    上界远高于任何真实视频用量，触发即说明上游返回的用量不可信。
    """
    if value > limit:
        raise VideoBillingError(
            "video_cost_factor_out_of_range",
            f"video billing factor {field}={value} exceeds limit {limit}",
            500,
        )


@dataclass(frozen=True)
class VideoCostEstimate:
    estimated_tokens: int
    hold_micro: int
    unit_price_micro: int
    source: str


def round_micro_for_tokens(total_tokens: int, price_per_mtoken_micro: int) -> int:
    """把 token 数换算成 µRMB，不足 1 micro 的零头一律向上进位。

    视频计费是纯转嫁：上游按实际用量扣了钱，平台就必须原样收回。若这里用
    ROUND_HALF_UP，小于半 micro 的尾数会被抹掉，那部分成本由平台承担；
    高频调用下会持续侵蚀收入。ROUND_UP 把舍入方向固定为「归用户」，
    单笔最多多收 1 µRMB（即 0.000001 元），代价可忽略，但平台永不亏损。

    因子上界在相乘之前校验（审计 F-5）：越界抛错而非封顶，理由同
    :mod:`lumen_core.pricing` 里的 F-4 —— 封顶等于让平台吞掉超出部分。
    """
    if total_tokens < 0:
        raise ValueError("total_tokens must not be negative")
    if price_per_mtoken_micro < 0:
        raise ValueError("price_per_mtoken_micro must not be negative")
    _guard_video_factor("total_tokens", int(total_tokens), MAX_BILLABLE_TOKENS)
    _guard_video_factor(
        "price_per_mtoken_micro",
        int(price_per_mtoken_micro),
        MAX_RATE_PER_1K_MICRO,
    )
    value = (
        Decimal(int(total_tokens))
        * Decimal(int(price_per_mtoken_micro))
        / Decimal(1_000_000)
    )
    return int(value.quantize(Decimal("1"), rounding=ROUND_UP))


def estimate_key(*, resolution: str, duration_s: int) -> str:
    return f"{resolution}:{int(duration_s)}"


def hold_estimate_duration_s(
    duration_s: int,
    *,
    model: str | None = None,
    upstream_model: str | None = None,
) -> int:
    if int(duration_s) == SMART_VIDEO_DURATION_S:
        if is_seedance_25_identifier(model, upstream_model):
            return SEEDANCE_25_SMART_VIDEO_HOLD_DURATION_S
        return SMART_VIDEO_HOLD_DURATION_S
    return int(duration_s)


def _reference_kind(item: Any) -> str | None:
    if isinstance(item, Mapping):
        raw = item.get("kind")
    else:
        raw = getattr(item, "kind", None)
    return raw if isinstance(raw, str) else None


def video_resolution_pricing_variant(variant: str, resolution: str | None) -> str:
    """拼出 ``{动作}_{分辨率}`` 形式的计价 variant（定价规则的查表键）。

    这里必须 ``lower()``：``split_video_resolution_pricing_variant`` 反向解析时
    会把分辨率归一成小写，两边不对称的话 ``1080P`` 拼出的键是 ``t2v_1080P``，
    而运营录入的定价规则键是 ``t2v_1080p``，查不中就一路回退到不带分辨率的
    基础 variant——高分辨率通常单价更高，回退等于按低价结算，差额由平台吸收，
    踩「纯转嫁」红线。归一之后 ``1080P``/``1080p``/`` 1080p `` 收敛成同一个键。

    这里刻意**不**按审计建议做「白名单枚举校验」：``_pricing_fallback_variants``
    已经无条件把不带分辨率的基础 variant 放进回退链，未知分辨率本来就能优雅
    降级；反过来，一旦加了白名单，运营为新分辨率（比如 8k）配的规则会因为
    枚举没同步更新而被直接丢弃，只能按基础价结算——又是平台吸收成本。
    宁可多出一个查不中的键，也不能丢掉一条已配置的高价规则。
    """
    resolution = (resolution or "").strip().lower()
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
    if is_seedance_25_identifier(model, upstream_model):
        return SEEDANCE_25_MODEL
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
    durations_s: tuple[int, ...] | None = None,
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
                resolution, duration_s, parsed_estimate = parsed
                by_resolution.setdefault(resolution, {})[duration_s] = parsed_estimate
            expanded_action: dict[str, Any] = {}
            model_durations_s = (
                durations_s
                if durations_s is not None
                else (
                    SEEDANCE_25_SUPPORTED_VIDEO_DURATIONS_S
                    if is_seedance_25_identifier(model)
                    else SUPPORTED_VIDEO_DURATIONS_S
                )
            )
            for resolution, duration_map in by_resolution.items():
                for duration_s in model_durations_s:
                    duration_estimate = _duration_estimate(duration_map, duration_s)
                    if duration_estimate is not None:
                        expanded_action[
                            estimate_key(resolution=resolution, duration_s=duration_s)
                        ] = duration_estimate
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
        duration_s=hold_estimate_duration_s(duration_s, model=model),
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
    if unit_price is None or int(unit_price) <= 0:
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
    if unit_price is None or int(unit_price) <= 0:
        raise VideoBillingError(
            "video_pricing_missing",
            f"missing enabled video pricing rule for {model}/{effective_pricing_variant}",
            503,
        )
    actual_micro = round_micro_for_tokens(int(actual_total_tokens), int(unit_price))
    if actual_micro <= 0:
        raise VideoBillingError(
            "video_invalid_settlement",
            "actual video settlement cost must be positive",
            500,
        )
    if estimated_micro is not None:
        estimate = int(estimated_micro)
        if estimate > 0 and actual_micro > estimate * max(
            1, int(max_estimate_multiplier)
        ):
            raise VideoBillingError(
                "video_cost_exceeds_estimate",
                f"actual cost {actual_micro} exceeds estimate {estimate} by more than {max_estimate_multiplier}x",
                500,
                actual_micro=actual_micro,
            )
    return actual_micro


__all__ = [
    "MAX_BILLABLE_TOKENS",
    "MAX_RATE_PER_1K_MICRO",
    "VIDEO_PRICING_SCOPE",
    "VIDEO_PRICING_UNIT",
    "SMART_VIDEO_DURATION_S",
    "SMART_VIDEO_HOLD_DURATION_S",
    "SEEDANCE_25_MODEL",
    "SEEDANCE_25_SMART_VIDEO_HOLD_DURATION_S",
    "SEEDANCE_25_SUPPORTED_VIDEO_DURATIONS_S",
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
