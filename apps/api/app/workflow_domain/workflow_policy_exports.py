# ruff: noqa: F401

"""Compatibility names shared by the historical workflow route facade."""

from __future__ import annotations

from .apparel_library_reference import ReferenceProfile
from .apparel_scene_planner import (
    build_garment_lock as _build_garment_lock,
    compose_image_prompt_with_gpt55 as _compose_image_prompt_with_gpt55,
    fallback_risk_review as _fallback_risk_review,
    plan_scene_cards_with_gpt55 as _plan_scene_cards_with_gpt55,
    review_prompt_risk_with_gpt55 as _review_prompt_risk_with_gpt55,
    resolve_scene_provider_order as _resolve_scene_provider_order,
    rules_fallback_planning as _rules_fallback_scene_planning,
    scene_fingerprint as _scene_fingerprint,
)
from .showcase_model_policy import (
    _FACE_ARCHETYPES_FEMALE,
    _FACE_ARCHETYPES_MALE,
    _accessory_age_direction,
    _accessory_strength_direction,
    _age_direction,
    _compact_showcase_user_direction,
    _height_requirement,
    _infer_age,
    _infer_candidate_gender,
    _infer_model_height_cm,
    _model_diversity_anchor,
    _style_region_from_text,
)
from .showcase_shot_pool import (
    SHOT_CLASS_ORDER,
    age_soft_constraint as _age_soft_constraint,
    resolve_pool_band as _resolve_pool_band,
    select_variants as _select_shot_variants,
    shot_class_distribution as _shot_class_distribution,
)
from .showcase_shot_pool_adult import ADULT_POOL
from .showcase_template_policy import (
    TEMPLATE_LABELS,
    SCENE_ENVIRONMENT_TEMPLATES,
    _LIFESTYLE_TEMPLATES,
    _POSE_DIRECTIONS,
    _RENDER_DIRECTIONS,
    _RENDER_DIRECTIONS_OUTDOOR,
    _SQUARE_OR_LANDSCAPE_RATIOS,
    _scene_environment_outdoor_phrase,
    _showcase_composition_direction,
    _showcase_framing_direction,
    _showcase_pose_direction,
    _showcase_render_direction,
    _template_requirement,
)

__all__ = [
    "ADULT_POOL",
    "ReferenceProfile",
    "SCENE_ENVIRONMENT_TEMPLATES",
    "SHOT_CLASS_ORDER",
    "TEMPLATE_LABELS",
    "_FACE_ARCHETYPES_FEMALE",
    "_FACE_ARCHETYPES_MALE",
    "_LIFESTYLE_TEMPLATES",
    "_POSE_DIRECTIONS",
    "_RENDER_DIRECTIONS",
    "_RENDER_DIRECTIONS_OUTDOOR",
    "_SQUARE_OR_LANDSCAPE_RATIOS",
    "_accessory_age_direction",
    "_accessory_strength_direction",
    "_age_direction",
    "_age_soft_constraint",
    "_build_garment_lock",
    "_compact_showcase_user_direction",
    "_compose_image_prompt_with_gpt55",
    "_fallback_risk_review",
    "_height_requirement",
    "_infer_age",
    "_infer_candidate_gender",
    "_infer_model_height_cm",
    "_model_diversity_anchor",
    "_plan_scene_cards_with_gpt55",
    "_resolve_pool_band",
    "_resolve_scene_provider_order",
    "_review_prompt_risk_with_gpt55",
    "_rules_fallback_scene_planning",
    "_scene_environment_outdoor_phrase",
    "_scene_fingerprint",
    "_select_shot_variants",
    "_showcase_composition_direction",
    "_showcase_framing_direction",
    "_showcase_pose_direction",
    "_showcase_render_direction",
    "_shot_class_distribution",
    "_style_region_from_text",
    "_template_requirement",
]
