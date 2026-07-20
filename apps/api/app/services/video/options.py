"""Video capability, pricing, and submission-readiness services."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Collection, Iterable
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.models import PricingRule
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    VideoAction,
    VideoCreateIn,
    VideoModelOptionOut,
    VideoOptionsOut,
    VideoPriceOptionOut,
    VideoPricingVariant,
    VideoResolution,
)
from lumen_core.video_billing import (
    SMART_VIDEO_DURATION_S,
    SUPPORTED_VIDEO_DURATIONS_S,
    VIDEO_LEGACY_REFERENCE_PRICING_VARIANT,
    VIDEO_PRICING_SCOPE,
    VIDEO_PRICING_UNIT,
    VIDEO_PRICING_VARIANTS,
    expand_video_duration_estimates,
    split_video_resolution_pricing_variant,
    video_billing_model,
)
from lumen_core.video_providers import (
    VIDEO_ACTIONS,
    parse_video_provider_config_json,
    seedance_20_allowed_resolutions,
    seedance_20_variant,
    select_video_provider,
)

from ...runtime_settings import get_setting
from ...video_options import reference_media_limits_for_model
from .errors import video_http_error
from .presentation import money


BoolLoader = Callable[[AsyncSession], Awaitable[bool]]
EstimatesLoader = Callable[[AsyncSession], Awaitable[dict[str, Any]]]
ProviderStateLoader = Callable[
    [AsyncSession],
    Awaitable[tuple[list[Any], list[str]]],
]
PriceOptionsLoader = Callable[
    [AsyncSession],
    Awaitable[list[VideoPriceOptionOut]],
]

DEFAULT_VIDEO_DURATIONS = list(SUPPORTED_VIDEO_DURATIONS_S)
DEFAULT_VIDEO_RESOLUTIONS = ["480p", "720p", "1080p", "4k"]
DEFAULT_VIDEO_ASPECT_RATIOS = [
    "adaptive",
    "16:9",
    "9:16",
    "1:1",
    "4:3",
    "3:4",
    "21:9",
]
VIDEO_RESOLUTION_ORDER = {
    value: index for index, value in enumerate(DEFAULT_VIDEO_RESOLUTIONS)
}
VOLCANO_NEWAPI_RESOLUTIONS = ("720p",)
HAPPYHORSE_RESOLUTIONS = ("720p", "1080p")
OMNI_FLASH_RESOLUTIONS = ("720p", "1080p", "4k")
OMNI_FLASH_DURATIONS = tuple(range(6, 11))
VIDEO_ACTION_VALUES = cast(tuple[VideoAction, ...], VIDEO_ACTIONS)
HAPPYHORSE_MODEL_PREFIX = "happyhorse-1.0"
OMNI_FLASH_MODEL_PREFIXES = ("omni-flash", "gemini_omni_flash")


async def setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    return await get_setting(db, spec)


async def video_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await setting_raw(db, "video.enabled"),
        False,
    )


async def billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await setting_raw(db, "billing.enabled"),
        False,
    )


async def allow_negative_balance(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


async def video_provider_state(
    db: AsyncSession,
) -> tuple[list[Any], list[str]]:
    raw_video = await setting_raw(db, "video.providers")
    raw_shared = await setting_raw(db, "providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    return providers, errors


async def video_hold_estimates(db: AsyncSession) -> dict[str, Any]:
    raw = await setting_raw(db, "video.token_hold_estimates")
    if not raw:
        raise video_http_error(
            "video_estimates_missing",
            "video token hold estimates are not configured",
            503,
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise video_http_error(
            "video_estimates_invalid",
            "video token hold estimates are invalid",
            503,
        ) from exc
    if not isinstance(parsed, dict):
        raise video_http_error(
            "video_estimates_invalid",
            "video token hold estimates must be an object",
            503,
        )
    return expand_video_duration_estimates(parsed)


def estimate_pairs(estimates: dict[str, Any]) -> tuple[list[int], list[str]]:
    durations: set[int] = set()
    resolutions: set[str] = set()
    for model_value in estimates.values():
        if not isinstance(model_value, dict):
            continue
        for action_value in model_value.values():
            if not isinstance(action_value, dict):
                continue
            for key in action_value:
                if not isinstance(key, str) or ":" not in key:
                    continue
                resolution, duration = key.rsplit(":", 1)
                try:
                    duration_s = int(duration)
                except ValueError:
                    continue
                if resolution:
                    resolutions.add(resolution)
                if duration_s > 0:
                    durations.add(duration_s)
    return (
        sorted(durations) or list(DEFAULT_VIDEO_DURATIONS),
        ordered_video_resolutions(resolutions) or list(DEFAULT_VIDEO_RESOLUTIONS),
    )


def duration_options(estimates: dict[str, Any]) -> list[int]:
    durations, _resolutions = estimate_pairs(estimates)
    return [
        SMART_VIDEO_DURATION_S,
        *[item for item in durations if item != SMART_VIDEO_DURATION_S],
    ]


def duration_options_for_model(
    model: str,
    *,
    upstream_model: str | None = None,
    available_durations: Iterable[int] | None = None,
) -> list[int]:
    if is_omni_flash_model(model, upstream_model):
        return list(OMNI_FLASH_DURATIONS)
    available = set(available_durations or DEFAULT_VIDEO_DURATIONS)
    positive_durations = sorted(item for item in available if item > 0)
    if is_seedance_20_model(model, upstream_model):
        positive_durations = [item for item in positive_durations if item >= 4]
    return [SMART_VIDEO_DURATION_S, *positive_durations]


def estimate_duration_options_for_model_action(
    estimates: dict[str, Any],
    *,
    model: str,
    action: str,
    resolutions: Iterable[str],
) -> list[int]:
    model_value = estimates.get(model)
    if not isinstance(model_value, dict):
        return []
    actions = [action]
    if action == VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        actions = [
            "reference_image",
            "reference_video",
            VIDEO_LEGACY_REFERENCE_PRICING_VARIANT,
            "i2v",
        ]
    allowed_resolutions = set(resolutions)
    durations: set[int] = set()
    for action_name in actions:
        action_value = model_value.get(action_name)
        if not isinstance(action_value, dict):
            continue
        for key in action_value:
            if not isinstance(key, str) or ":" not in key:
                continue
            resolution, duration = key.rsplit(":", 1)
            if allowed_resolutions and resolution not in allowed_resolutions:
                continue
            try:
                duration_s = int(duration)
            except ValueError:
                continue
            if duration_s > 0:
                durations.add(duration_s)
    return sorted(durations)


def parse_video_action(value: str) -> VideoAction | None:
    if value not in VIDEO_ACTION_VALUES:
        return None
    return cast(VideoAction, value)


def video_price_action_for_provider(
    provider_kind: str,
    action: VideoAction,
) -> VideoPricingVariant:
    if (
        provider_kind in {"dashscope", "omni_flash"}
        and action == VIDEO_LEGACY_REFERENCE_PRICING_VARIANT
    ):
        return "reference_image"
    return action


def duration_options_for_provider_action(
    estimates: dict[str, Any],
    *,
    model: str,
    upstream_model: str | None,
    provider_kind: str,
    action: VideoAction,
    resolutions: Iterable[str],
    fallback_durations: Iterable[int],
    allow_action_fallback: bool = True,
    allow_global_fallback: bool = True,
) -> list[int]:
    if is_omni_flash_model(model, upstream_model):
        return duration_options_for_model(model, upstream_model=upstream_model)
    billing_model = video_billing_model(model, upstream_model)
    price_action = video_price_action_for_provider(provider_kind, action)
    estimate_durations = estimate_duration_options_for_model_action(
        estimates,
        model=billing_model,
        action=price_action,
        resolutions=resolutions,
    )
    if estimate_durations:
        return duration_options_for_model(
            model,
            upstream_model=upstream_model,
            available_durations=estimate_durations,
        )
    if allow_action_fallback:
        action_durations = estimate_duration_options_for_model_action(
            estimates,
            model=billing_model,
            action=price_action,
            resolutions=[],
        )
        if action_durations:
            return duration_options_for_model(
                model,
                upstream_model=upstream_model,
                available_durations=action_durations,
            )
    if not allow_global_fallback:
        return []
    return duration_options_for_model(
        model,
        upstream_model=upstream_model,
        available_durations=fallback_durations,
    )


def ordered_video_resolutions(values: Iterable[str]) -> list[str]:
    return sorted(
        set(values),
        key=lambda value: (VIDEO_RESOLUTION_ORDER.get(value, 999), value),
    )


def is_seedance_20_fast_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) == "fast"


def is_seedance_20_mini_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) == "mini"


def is_seedance_20_standard_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) == "standard"


def is_seedance_20_model(*identifiers: str | None) -> bool:
    return seedance_20_variant(*identifiers) is not None


def is_happyhorse_model(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        if identifier.strip().lower().startswith(HAPPYHORSE_MODEL_PREFIX):
            return True
    return False


def is_omni_flash_model(*identifiers: str | None) -> bool:
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        value = identifier.strip().lower().replace("-", "_")
        if any(
            value.startswith(prefix.replace("-", "_"))
            for prefix in OMNI_FLASH_MODEL_PREFIXES
        ):
            return True
    return False


def video_resolution_options_for_model(
    model: str,
    *,
    upstream_model: str | None = None,
    available_resolutions: Iterable[str] | None = None,
) -> list[str]:
    available = ordered_video_resolutions(
        available_resolutions or DEFAULT_VIDEO_RESOLUTIONS
    )
    if is_happyhorse_model(model, upstream_model):
        allowed = set(HAPPYHORSE_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    if is_omni_flash_model(model, upstream_model):
        allowed = set(OMNI_FLASH_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    seedance_resolutions = seedance_20_allowed_resolutions(model, upstream_model)
    if seedance_resolutions is not None:
        allowed = set(seedance_resolutions)
        return [resolution for resolution in available if resolution in allowed]
    return [resolution for resolution in available if resolution != "4k"]


def video_resolution_options_for_provider(
    provider_kind: str,
    model: str,
    *,
    upstream_model: str | None = None,
    available_resolutions: Iterable[str] | None = None,
) -> list[str]:
    available = ordered_video_resolutions(
        available_resolutions or DEFAULT_VIDEO_RESOLUTIONS
    )
    if provider_kind == "volcano_newapi":
        allowed = set(VOLCANO_NEWAPI_RESOLUTIONS)
        return [resolution for resolution in available if resolution in allowed]
    return video_resolution_options_for_model(
        model,
        upstream_model=upstream_model,
        available_resolutions=available,
    )


async def video_price_options(
    db: AsyncSession,
) -> list[VideoPriceOptionOut]:
    rows = (
        (
            await db.execute(
                select(PricingRule).where(
                    PricingRule.scope == VIDEO_PRICING_SCOPE,
                    PricingRule.unit == VIDEO_PRICING_UNIT,
                    PricingRule.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[VideoPriceOptionOut] = []
    for row in rows:
        raw_action, resolution = split_video_resolution_pricing_variant(row.variant)
        if raw_action not in VIDEO_PRICING_VARIANTS:
            continue
        action = cast(VideoPricingVariant, raw_action)
        out.append(
            VideoPriceOptionOut(
                model=row.key,
                action=action,
                resolution=resolution,
                variant=row.variant,
                price=money(row.price_micro),
                enabled=row.enabled,
                note=row.note,
            )
        )
    return out


def has_video_price(
    price_pairs: Collection[tuple[str, VideoPricingVariant, str | None]],
    *,
    model: str,
    action: VideoPricingVariant,
    resolutions: list[str] | tuple[str, ...] | None = None,
) -> bool:
    def has(action_name: VideoPricingVariant, resolution: str | None) -> bool:
        return (model, action_name, resolution) in price_pairs or (
            model,
            action_name,
            None,
        ) in price_pairs

    resolution_options: Iterable[str | None] = resolutions if resolutions else (None,)
    if action != VIDEO_LEGACY_REFERENCE_PRICING_VARIANT:
        return any(has(action, resolution) for resolution in resolution_options)
    for resolution in resolution_options:
        if (
            has(VIDEO_LEGACY_REFERENCE_PRICING_VARIANT, resolution)
            or has("reference_image", resolution)
            or has("i2v", resolution)
            or has("reference_video", resolution)
        ):
            return True
    return False


def public_video_hold_estimates(
    estimates: dict[str, Any],
    model_billing_models: dict[str, dict[str, str]],
) -> dict[str, Any]:
    allowed_models = {
        billing_model
        for action_map in model_billing_models.values()
        for billing_model in action_map.values()
        if isinstance(billing_model, str) and billing_model
    }
    out: dict[str, Any] = {}
    for model in sorted(allowed_models):
        value = estimates.get(model)
        if isinstance(value, dict):
            out[model] = value
    return out


def forbidden_video_options() -> VideoOptionsOut:
    return VideoOptionsOut(
        enabled=False,
        models=[],
        durations_s=[],
        resolutions=[],
        aspect_ratios=list(DEFAULT_VIDEO_ASPECT_RATIOS),
        generate_audio=False,
        pricing=[],
        hold_estimates={},
        unavailable_reason="account_mode_forbidden",
    )


def _provider_model_entries(provider: Any) -> Iterable[tuple[str, VideoAction]]:
    for key in provider.models or {}:
        if ":" not in key:
            for action in VIDEO_ACTION_VALUES:
                if provider.supports(key, action):
                    yield key, action
            continue
        model, raw_action = key.rsplit(":", 1)
        action = parse_video_action(raw_action)
        if action is not None and provider.supports(model, action):
            yield model, action


def _record_model_capability(
    *,
    provider: Any,
    model: str,
    action: VideoAction,
    estimates: dict[str, Any],
    prices: Collection[tuple[str, VideoPricingVariant, str | None]],
    global_resolutions: list[str],
    fallback_durations: list[int],
    model_actions: dict[str, set[VideoAction]],
    model_durations: dict[str, set[int]],
    model_action_durations: dict[str, dict[VideoAction, set[int]]],
    model_action_resolution_durations: dict[
        str,
        dict[VideoAction, dict[str, set[int]]],
    ],
    model_resolutions: dict[str, set[str]],
    model_billing_models: dict[str, dict[str, str]],
) -> None:
    upstream_model = provider.upstream_model_for(model, action)
    billing_model = video_billing_model(model, upstream_model)
    allowed_resolutions = video_resolution_options_for_provider(
        provider.kind,
        model,
        upstream_model=upstream_model,
        available_resolutions=global_resolutions,
    )
    price_action = video_price_action_for_provider(provider.kind, action)
    action_resolutions = [
        resolution
        for resolution in allowed_resolutions
        if has_video_price(
            prices,
            model=billing_model,
            action=price_action,
            resolutions=[resolution],
        )
    ]
    if not action_resolutions:
        return
    estimate_durations = estimate_duration_options_for_model_action(
        estimates,
        model=billing_model,
        action=price_action,
        resolutions=action_resolutions,
    )
    action_durations = duration_options_for_provider_action(
        estimates,
        model=model,
        upstream_model=upstream_model,
        provider_kind=provider.kind,
        action=action,
        resolutions=action_resolutions,
        fallback_durations=fallback_durations,
    )
    model_actions.setdefault(model, set()).add(action)
    model_durations.setdefault(model, set()).update(action_durations)
    model_action_durations.setdefault(model, {}).setdefault(action, set()).update(
        action_durations
    )
    action_resolution_durations = model_action_resolution_durations.setdefault(
        model,
        {},
    ).setdefault(action, {})
    for resolution in action_resolutions:
        resolution_durations = duration_options_for_provider_action(
            estimates,
            model=model,
            upstream_model=upstream_model,
            provider_kind=provider.kind,
            action=action,
            resolutions=[resolution],
            fallback_durations=estimate_durations or fallback_durations,
            allow_action_fallback=False,
            allow_global_fallback=False,
        )
        action_resolution_durations.setdefault(resolution, set()).update(
            resolution_durations
        )
    model_resolutions.setdefault(model, set()).update(action_resolutions)
    model_billing_models.setdefault(model, {})[action] = billing_model


def _model_option(
    providers: list[Any],
    *,
    model: str,
    actions: set[VideoAction],
    model_durations: dict[str, set[int]],
    model_action_durations: dict[str, dict[VideoAction, set[int]]],
    model_action_resolution_durations: dict[
        str,
        dict[VideoAction, dict[str, set[int]]],
    ],
    model_resolutions: dict[str, set[str]],
    model_billing_models: dict[str, dict[str, str]],
) -> VideoModelOptionOut:
    sorted_actions = sorted(actions)
    billing_models: dict[str, str] = {
        action: model_billing_models.get(model, {}).get(action, model)
        for action in sorted_actions
    }
    unique_billing_models = set(billing_models.values())
    return VideoModelOptionOut(
        model=model,
        billing_model=(
            next(iter(unique_billing_models))
            if len(unique_billing_models) == 1
            else None
        ),
        billing_models=billing_models,
        actions=sorted_actions,
        durations_s=sorted(model_durations.get(model, set())),
        durations_by_action={
            action: sorted(durations)
            for action, durations in model_action_durations.get(model, {}).items()
        },
        durations_by_action_resolution={
            action: {
                resolution: sorted(durations)
                for resolution, durations in resolution_map.items()
            }
            for action, resolution_map in model_action_resolution_durations.get(
                model,
                {},
            ).items()
        },
        resolutions=cast(
            list[VideoResolution],
            ordered_video_resolutions(model_resolutions.get(model, set())),
        ),
        reference_media_limits=reference_media_limits_for_model(
            providers,
            model,
            actions,
        ),
    )


async def get_wallet_video_options(
    db: AsyncSession,
    *,
    enabled_loader: BoolLoader = video_enabled,
    estimates_loader: EstimatesLoader = video_hold_estimates,
    provider_loader: ProviderStateLoader = video_provider_state,
    price_loader: PriceOptionsLoader = video_price_options,
) -> VideoOptionsOut:
    enabled = await enabled_loader(db)
    estimates: dict[str, Any] = {}
    unavailable_reason: str | None = None
    try:
        estimates = await estimates_loader(db)
    except HTTPException as exc:
        unavailable_reason = (
            exc.detail.get("error", {}).get("code")
            if isinstance(exc.detail, dict)
            else "video_estimates_missing"
        )

    providers, provider_errors = await provider_loader(db)
    if provider_errors:
        unavailable_reason = "video_provider_config_invalid"
    prices = await price_loader(db)
    price_pairs: set[tuple[str, VideoPricingVariant, str | None]] = {
        (item.model, item.action, item.resolution) for item in prices
    }
    durations, resolutions = estimate_pairs(estimates)
    global_durations = duration_options(estimates)
    fallback_durations = durations or global_durations

    model_actions: dict[str, set[VideoAction]] = {}
    model_durations: dict[str, set[int]] = {}
    model_action_durations: dict[str, dict[VideoAction, set[int]]] = {}
    model_action_resolution_durations: dict[
        str,
        dict[VideoAction, dict[str, set[int]]],
    ] = {}
    model_resolutions: dict[str, set[str]] = {}
    model_billing_models: dict[str, dict[str, str]] = {}
    for provider in providers:
        for model, action in _provider_model_entries(provider):
            _record_model_capability(
                provider=provider,
                model=model,
                action=action,
                estimates=estimates,
                prices=price_pairs,
                global_resolutions=resolutions,
                fallback_durations=fallback_durations,
                model_actions=model_actions,
                model_durations=model_durations,
                model_action_durations=model_action_durations,
                model_action_resolution_durations=model_action_resolution_durations,
                model_resolutions=model_resolutions,
                model_billing_models=model_billing_models,
            )
    if enabled and not model_actions and unavailable_reason is None:
        unavailable_reason = "video_provider_or_pricing_missing"
    model_options = [
        _model_option(
            providers,
            model=model,
            actions=actions,
            model_durations=model_durations,
            model_action_durations=model_action_durations,
            model_action_resolution_durations=model_action_resolution_durations,
            model_resolutions=model_resolutions,
            model_billing_models=model_billing_models,
        )
        for model, actions in sorted(model_actions.items())
    ]
    public_hold_estimates = public_video_hold_estimates(
        estimates,
        model_billing_models,
    )
    return VideoOptionsOut(
        enabled=enabled and unavailable_reason is None,
        models=model_options,
        durations_s=duration_options(estimates),
        resolutions=resolutions,
        aspect_ratios=list(DEFAULT_VIDEO_ASPECT_RATIOS),
        generate_audio=True,
        pricing=prices,
        hold_estimates=public_hold_estimates,
        unavailable_reason=None
        if enabled and unavailable_reason is None
        else unavailable_reason or "video_disabled",
    )


async def get_video_options(
    user: Any,
    db: AsyncSession,
    *,
    enabled_loader: BoolLoader = video_enabled,
    estimates_loader: EstimatesLoader = video_hold_estimates,
    provider_loader: ProviderStateLoader = video_provider_state,
    price_loader: PriceOptionsLoader = video_price_options,
) -> VideoOptionsOut:
    """Return video capabilities without depending on an HTTP route module."""
    if getattr(user, "account_mode", "wallet") != "wallet":
        return forbidden_video_options()
    return await get_wallet_video_options(
        db,
        enabled_loader=enabled_loader,
        estimates_loader=estimates_loader,
        provider_loader=provider_loader,
        price_loader=price_loader,
    )


async def require_video_create_ready(
    db: AsyncSession,
    body: VideoCreateIn,
    *,
    video_enabled_loader: BoolLoader = video_enabled,
    billing_enabled_loader: BoolLoader = billing_enabled,
    estimates_loader: EstimatesLoader = video_hold_estimates,
    provider_loader: ProviderStateLoader = video_provider_state,
) -> tuple[Any, dict[str, Any]]:
    if not await video_enabled_loader(db):
        raise video_http_error(
            "video_disabled",
            "video generation is disabled",
            503,
        )
    if not await billing_enabled_loader(db):
        raise video_http_error(
            "billing_disabled",
            "video generation requires wallet billing",
            503,
        )
    estimates = await estimates_loader(db)
    durations, resolutions = estimate_pairs(estimates)
    if body.resolution not in resolutions:
        raise video_http_error(
            "invalid_resolution",
            "resolution is not available",
            422,
        )
    if body.aspect_ratio not in DEFAULT_VIDEO_ASPECT_RATIOS:
        raise video_http_error(
            "invalid_aspect_ratio",
            "aspect_ratio is not available",
            422,
        )
    providers, provider_errors = await provider_loader(db)
    if provider_errors:
        raise video_http_error(
            "video_provider_config_invalid",
            "; ".join(provider_errors),
            503,
        )
    provider = select_video_provider(
        providers,
        model=body.model,
        action=body.action,
    )
    if provider is None:
        raise video_http_error(
            "video_provider_missing",
            "no enabled video provider supports this model/action",
            503,
        )
    upstream_model = provider.upstream_model_for(body.model, body.action)
    model_resolutions = video_resolution_options_for_provider(
        provider.kind,
        body.model,
        upstream_model=upstream_model,
        available_resolutions=resolutions,
    )
    if body.resolution not in model_resolutions:
        raise video_http_error(
            "invalid_resolution",
            "resolution is not available for this model",
            422,
            model=body.model,
            resolution=body.resolution,
            available_resolutions=model_resolutions,
        )
    model_durations = duration_options_for_provider_action(
        estimates,
        model=body.model,
        upstream_model=upstream_model,
        provider_kind=provider.kind,
        action=body.action,
        resolutions=[body.resolution],
        fallback_durations=durations,
        allow_action_fallback=False,
        allow_global_fallback=False,
    )
    if body.duration_s not in model_durations:
        raise video_http_error(
            "invalid_duration",
            "duration_s is not available for this model",
            422,
            model=body.model,
            duration_s=body.duration_s,
            available_durations_s=model_durations,
        )
    return provider, estimates
