"""Compatibility facade for apparel showcase scene planning.

The workflow route imports this module as a stable adapter surface. Stateful
provider hooks remain late-bound here for monkeypatch compatibility, while the
planning, composition, validation, transport, and fallback implementations live
in ``apparel_scene_planner_parts``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.providers import (
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec

from ...runtime_settings import get_setting
from ..domain.apparel_scene_planner_exports import APPAREL_SCENE_PLANNER_EXPORTS
from ..domain.apparel_scene_fallbacks import (
    fallback_scene_cards_from_pool,
)
from ..domain.apparel_scene_fallbacks import *  # noqa: F403,F401
from ..ports.runtime_state import ProviderRoundRobinStatePort
from .apparel_scene_planner_parts import (
    contracts as _contracts,
    fallbacks as _fallbacks,
    parsing_validation as _parsing,
    planning as _planning,
    prompt_composition as _prompt_composition,
    provider_client as _provider_client,
)

logger = logging.getLogger(__name__)

ContinuityAnchor = _contracts.ContinuityAnchor
ScenePlannerMode = _contracts.ScenePlannerMode
SceneProviderSelection = _contracts.SceneProviderSelection
SceneStrategy = _contracts.SceneStrategy
SceneVariety = _contracts.SceneVariety

_DIRECTOR_MODEL = _provider_client.DIRECTOR_MODEL
_FALLBACK_MODEL = _provider_client.FALLBACK_MODEL
_RETRYABLE_STATUS = _provider_client.RETRYABLE_STATUS
_GPT55_PROVIDER_LIMIT_ENV = _provider_client.PROVIDER_LIMIT_ENV
_GPT55_CALL_TIMEOUT_ENV = _provider_client.CALL_TIMEOUT_ENV
_GPT55_DEFAULT_PROVIDER_LIMIT = _provider_client.DEFAULT_PROVIDER_LIMIT
_GPT55_DIRECTOR_TIMEOUT_SEC = _provider_client.DIRECTOR_TIMEOUT_SEC
_GPT55_COMPOSER_TIMEOUT_SEC = _provider_client.COMPOSER_TIMEOUT_SEC
_GPT55_REVIEW_TIMEOUT_SEC = _provider_client.REVIEW_TIMEOUT_SEC
_GPT55_DEFAULT_TIMEOUT_SEC = _provider_client.DEFAULT_TIMEOUT_SEC
_GPT55_ATTEMPT_TIMEOUT_SEC = _provider_client.ATTEMPT_TIMEOUT_SEC
_GPT55_DIRECTOR_RETRY_ENV = _planning.DIRECTOR_RETRY_ENV
_GPT55_DIRECTOR_DEFAULT_RETRIES = _planning.DIRECTOR_DEFAULT_RETRIES
_REFERENCE_IMAGE_RETRY_STATUS = _provider_client.REFERENCE_IMAGE_RETRY_STATUS
_REFERENCE_IMAGE_RETRY_TOKENS = _provider_client.REFERENCE_IMAGE_RETRY_TOKENS

_BACK_VIEW_TEXT_TOKENS = _parsing.BACK_VIEW_TEXT_TOKENS
_SIDE_BACK_VIEW_TOKENS = _parsing.SIDE_BACK_VIEW_TOKENS
_SIDE_BACK_CAMERA_ANGLE_TOKENS = _parsing.SIDE_BACK_CAMERA_ANGLE_TOKENS
_FRONT_CAMERA_ANGLE_TOKENS = _parsing.FRONT_CAMERA_ANGLE_TOKENS

_Gpt55CallTimeout = _provider_client.Gpt55CallTimeout
_UpstreamHTTPError = _provider_client.UpstreamHTTPError

_director_retry_payload = _planning.director_retry_payload
_director_retry_instructions = _planning.director_retry_instructions
_director_retry_failure_summary = _planning.director_retry_failure_summary
_director_instructions = _planning.director_instructions

_sanitize_shooting_brief = _parsing.sanitize_shooting_brief
_coerce_candidate_briefs = _parsing.coerce_candidate_briefs
_coerce_selection_scores = _parsing.coerce_selection_scores
_has_view_token = _parsing.has_view_token
_camera_angle_has_token = _parsing.camera_angle_has_token
_required_gpt_scene_fields_missing = _parsing.required_gpt_scene_fields_missing
_reject_side_back_for_non_side_card = _parsing.reject_side_back_for_non_side_card
_scene_card_match_index = _parsing.scene_card_match_index
_align_scene_cards = _parsing.align_scene_cards
_validate_normalized_scene_card = _parsing.validate_normalized_scene_card
_normalize_scene_card = _parsing.normalize_scene_card
_normalize_scene_cards = _parsing.normalize_scene_cards
_assert_unique_scene_fingerprints = _parsing.assert_unique_scene_fingerprints
_unique_fingerprints = _parsing.unique_fingerprints
_extract_json_object = _parsing.extract_json_object


def _planning_dependencies() -> _planning.PlanningDependencies:
    return _planning.PlanningDependencies(
        call_json=_call_gpt55_json,
        normalize_scene_cards=_normalize_scene_cards,
        fallback_scene_cards=fallback_scene_cards_from_pool,
        fallback_result=_fallback_planning_result,
        unique_fingerprints=_unique_fingerprints,
        director_retry_count=_gpt55_director_retry_count,
        logger=logger,
    )


def _prompt_dependencies() -> _prompt_composition.PromptCompositionDependencies:
    return _prompt_composition.PromptCompositionDependencies(
        call_json=_call_gpt55_json,
        fallback_prompt=fallback_prompt_composition,
        fallback_risk_review=fallback_risk_review,
        logger=logger,
    )


async def plan_scene_cards_with_gpt55(
    db: AsyncSession,
    *,
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    output_count: int,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    scene_strategy: str,
    scene_variety: str,
    continuity_anchor: str,
    allow_pet: bool,
    allow_background_people: bool,
    provider_selection: SceneProviderSelection | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return await _planning.plan_scene_cards_with_gpt55(
        db,
        product_analysis=product_analysis,
        garment_lock=garment_lock,
        model_summary=model_summary,
        template=template,
        scene_environment=scene_environment,
        shot_picks=shot_picks,
        aspect_ratio=aspect_ratio,
        output_count=output_count,
        user_prompt=user_prompt,
        accessory_plan=accessory_plan,
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
        continuity_anchor=continuity_anchor,
        allow_pet=allow_pet,
        allow_background_people=allow_background_people,
        provider_selection=provider_selection,
        reference_images=reference_images,
        dependencies=_planning_dependencies(),
    )


def rules_fallback_planning(
    *,
    product_analysis: dict[str, Any],
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    allow_pet: bool,
    continuity_anchor: str,
    scene_strategy: str = "natural_series",
    scene_variety: str = "rich",
) -> dict[str, Any]:
    return _fallbacks.rules_fallback_planning(
        product_analysis=product_analysis,
        template=template,
        scene_environment=scene_environment,
        shot_picks=shot_picks,
        aspect_ratio=aspect_ratio,
        user_prompt=user_prompt,
        accessory_plan=accessory_plan,
        allow_pet=allow_pet,
        continuity_anchor=continuity_anchor,
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
        fallback_scene_cards=fallback_scene_cards_from_pool,
        fallback_result=_fallback_planning_result,
    )


def _fallback_planning_result(
    cards: list[dict[str, Any]],
    *,
    reason: str,
) -> dict[str, Any]:
    return _fallbacks.fallback_planning_result(
        cards,
        reason=reason,
        unique_fingerprints=_unique_fingerprints,
    )


def _gpt55_director_retry_count() -> int:
    return _planning.gpt55_director_retry_count(logger)


async def compose_image_prompt_with_gpt55(
    db: AsyncSession,
    *,
    base_prompt: str,
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    scene_card: dict[str, Any],
    shot_class: str,
    template: str,
    aspect_ratio: str,
    final_quality: str,
    rewrite_instruction: str | None = None,
    provider_selection: SceneProviderSelection | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return await _prompt_composition.compose_image_prompt_with_gpt55(
        db,
        base_prompt=base_prompt,
        product_analysis=product_analysis,
        garment_lock=garment_lock,
        model_summary=model_summary,
        scene_card=scene_card,
        shot_class=shot_class,
        template=template,
        aspect_ratio=aspect_ratio,
        final_quality=final_quality,
        rewrite_instruction=rewrite_instruction,
        provider_selection=provider_selection,
        reference_images=reference_images,
        dependencies=_prompt_dependencies(),
    )


def fallback_prompt_composition(
    *,
    base_prompt: str,
    scene_card: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return _fallbacks.fallback_prompt_composition(
        base_prompt=base_prompt,
        scene_card=scene_card,
        reason=reason,
    )


async def review_prompt_risk_with_gpt55(
    db: AsyncSession,
    *,
    final_prompt: str,
    garment_lock: dict[str, Any],
    scene_card: dict[str, Any],
    batch_context: dict[str, Any],
    provider_selection: SceneProviderSelection | None = None,
) -> dict[str, Any]:
    return await _prompt_composition.review_prompt_risk_with_gpt55(
        db,
        final_prompt=final_prompt,
        garment_lock=garment_lock,
        scene_card=scene_card,
        batch_context=batch_context,
        provider_selection=provider_selection,
        dependencies=_prompt_dependencies(),
    )


def fallback_risk_review(
    *,
    scene_card: dict[str, Any],
    reason: str | None = None,
) -> dict[str, Any]:
    return _fallbacks.fallback_risk_review(
        scene_card=scene_card,
        reason=reason,
    )


async def resolve_scene_provider_order(
    db: AsyncSession,
    provider_runtime: ProviderRoundRobinStatePort,
) -> list[ProviderDefinition]:
    dependencies = _provider_client.ProviderResolutionDependencies(
        get_spec=get_spec,
        get_setting=get_setting,
        build_effective_provider_config=build_effective_provider_config,
        endpoint_kind_allowed=endpoint_kind_allowed,
        weighted_priority_order=weighted_priority_order,
        logger=logger,
    )
    return await _provider_client.resolve_scene_provider_order(
        db,
        provider_runtime,
        dependencies=dependencies,
    )


async def _call_gpt55_json(
    db: AsyncSession,
    *,
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    provider_selection: SceneProviderSelection | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    dependencies = _provider_client.ProviderCallDependencies(
        resolve_providers=_resolve_gpt55_providers,
        call_responses_text=_call_responses_text,
        extract_json_object=_extract_json_object,
        call_timeout_seconds=_gpt55_call_timeout_seconds,
        should_retry_without_reference_images=_should_retry_without_reference_images,
        should_try_next_attempt=_should_try_next_attempt,
        logger=logger,
    )
    return await _provider_client.call_gpt55_json(
        db,
        purpose=purpose,
        instructions=instructions,
        payload=payload,
        max_output_tokens=max_output_tokens,
        provider_selection=provider_selection,
        reference_images=reference_images,
        dependencies=dependencies,
    )


async def _resolve_gpt55_providers(
    db: AsyncSession,
    selection: SceneProviderSelection | None,
) -> list[ProviderDefinition]:
    return await _provider_client.resolve_gpt55_providers(
        db,
        selection,
        resolve_provider_order=resolve_scene_provider_order,
        limit_providers=_limit_gpt55_providers,
    )


def _gpt55_provider_limit() -> int:
    return _provider_client.gpt55_provider_limit(logger)


def _limit_gpt55_providers(
    providers: list[ProviderDefinition],
) -> list[ProviderDefinition]:
    return _provider_client.limit_gpt55_providers(
        providers,
        provider_limit=_gpt55_provider_limit,
    )


def _gpt55_call_timeout_seconds(purpose: str) -> float:
    return _provider_client.gpt55_call_timeout_seconds(
        purpose,
        logger=logger,
    )


async def _call_responses_text_with_timeout(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    reference_images: list[dict[str, str]] | None = None,
    timeout_seconds: float,
) -> str:
    return await _provider_client.call_responses_text_with_timeout(
        provider=provider,
        attempt=attempt,
        purpose=purpose,
        instructions=instructions,
        payload=payload,
        max_output_tokens=max_output_tokens,
        reference_images=reference_images,
        timeout_seconds=timeout_seconds,
        call_responses_text=_call_responses_text,
    )


async def _call_responses_text(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    reference_images: list[dict[str, str]] | None = None,
) -> str:
    return await _provider_client.call_responses_text(
        provider=provider,
        attempt=attempt,
        purpose=purpose,
        instructions=instructions,
        payload=payload,
        max_output_tokens=max_output_tokens,
        reference_images=reference_images,
    )


_should_retry_without_reference_images = (
    _provider_client.should_retry_without_reference_images
)
_should_try_next_attempt = _provider_client.should_try_next_attempt

__all__ = APPAREL_SCENE_PLANNER_EXPORTS
