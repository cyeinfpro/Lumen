"""Showcase preflight compatibility facade."""

# This module intentionally re-exports dependencies and private callables used by
# the historical routes.workflows facade and its monkeypatch-based tests.
# ruff: noqa: F401

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Iterable, cast

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.models import (
    Generation,
    Image,
    ModelCandidate,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schemas import AccessoryPlanIn, ShowcaseImagesCreateIn

from ..workflow_domain.apparel_scene_planner import (
    build_garment_lock as _build_garment_lock,
    compose_image_prompt_with_gpt55 as _compose_image_prompt_with_gpt55,
    fallback_risk_review as _fallback_risk_review,
    plan_scene_cards_with_gpt55 as _plan_scene_cards_with_gpt55,
    review_prompt_risk_with_gpt55 as _review_prompt_risk_with_gpt55,
    resolve_scene_provider_order as _resolve_scene_provider_order,
    rules_fallback_planning as _rules_fallback_scene_planning,
    scene_fingerprint as _scene_fingerprint,
)
from ..workflow_domain.showcase_model_policy import age_direction as _age_direction  # noqa: F401
from ..workflow_domain.showcase_model_policy import (
    compact_showcase_user_direction as _compact_showcase_user_direction,
)  # noqa: F401
from ..workflow_domain.showcase_model_policy import (
    height_requirement as _height_requirement,
)  # noqa: F401
from ..workflow_domain.showcase_model_policy import (
    infer_candidate_gender as _infer_candidate_gender,
)  # noqa: F401
from ..workflow_domain.showcase_model_policy import (
    infer_model_height_cm as _infer_model_height_cm,
)  # noqa: F401
from ..workflow_domain.showcase_model_policy import (
    model_diversity_anchor as _model_diversity_anchor,
)  # noqa: F401
from ..workflow_domain.showcase_model_policy import (
    style_region_from_text as _style_region_from_text,
)  # noqa: F401
from ..workflow_domain.showcase_shot_pool import (
    SHOT_CLASS_ORDER,
    ShotClass,
    ShotPool,
    ShotVariant,
    Template,
    age_soft_constraint as _age_soft_constraint,
    resolve_pool_band as _resolve_pool_band,
    select_variants as _select_shot_variants,
    shot_class_distribution as _shot_class_distribution,
)
from ..workflow_domain.showcase_shot_pool_adult import ADULT_POOL
from ..workflow_domain.showcase_shot_pool_kids import CHILD_POOL, TODDLER_POOL
from ..workflow_domain.immutables import freeze_mapping
from ..workflow_domain.showcase_template_policy import (
    showcase_composition_direction as _showcase_composition_direction,
)  # noqa: F401
from ..workflow_domain.showcase_template_policy import (
    showcase_framing_direction as _showcase_framing_direction,
)  # noqa: F401
from ..workflow_domain.showcase_template_policy import (
    showcase_pose_direction as _showcase_pose_direction,
)  # noqa: F401
from ..workflow_domain.showcase_template_policy import (
    showcase_render_direction as _showcase_render_direction,
)  # noqa: F401
from ..workflow_domain.showcase_template_policy import (
    template_requirement as _template_requirement,
)  # noqa: F401
from .serialization import dedupe_nonempty as _dedupe_nonempty  # noqa: F401
from .serialization import dict_or_empty as _dict_or_empty  # noqa: F401
from .serialization import http as _http  # noqa: F401
from .showcase_context import (
    prepare_durable_showcase_preflight as _prepare_durable_showcase_preflight,
)  # noqa: F401
from .showcase_context import (
    showcase_generation_context as _showcase_generation_context,
)  # noqa: F401
from .showcase_context import (
    showcase_request_input_json as _showcase_request_input_json,
)  # noqa: F401
from .showcase_inputs import candidate_prompt as _candidate_prompt  # noqa: F401
from .showcase_inputs import product_analysis_prompt as _product_analysis_prompt  # noqa: F401
from .showcase_inputs import seed_steps as _seed_steps  # noqa: F401
from .showcase_inputs import (
    showcase_reference_image_ids as _showcase_reference_image_ids,
)  # noqa: F401
from .showcase_inputs import showcase_target_image_count as _showcase_target_image_count  # noqa: F401
from .showcase_inputs import (
    validate_accessory_preview_image as _validate_accessory_preview_image,
)  # noqa: F401
from .showcase_inputs import validate_owned_images as _validate_owned_images  # noqa: F401
from .showcase_orchestration import (
    ShowcasePreflightProgressHook as _ShowcasePreflightProgressHook,
)  # noqa: F401
from .showcase_orchestration import (
    prepare_showcase_preflight_impl as _prepare_showcase_preflight_impl,
)  # noqa: F401
from .showcase_prompts import (
    STATIC_REWRITE_REPLACEMENTS as _STATIC_REWRITE_REPLACEMENTS,
)  # noqa: F401
from .showcase_prompts import composition_shooting_brief as _composition_shooting_brief  # noqa: F401
from .showcase_prompts import guarded_shooting_brief as _guarded_shooting_brief  # noqa: F401
from .showcase_prompts import (
    preserve_safe_motion_rewrite_instruction as _preserve_safe_motion_rewrite_instruction,
)  # noqa: F401
from .showcase_prompts import (
    rewrite_instruction_replaces_scene_or_composition as _rewrite_instruction_replaces_scene_or_composition,
)  # noqa: F401
from .showcase_prompts import (
    showcase_garment_lock_prefix as _showcase_garment_lock_prefix,
)  # noqa: F401
from .showcase_prompts import showcase_prompt as _showcase_prompt  # noqa: F401
from .showcase_prompts import showcase_prompt_brief as _showcase_prompt_brief  # noqa: F401
from .showcase_scene_policy import compact_lock_text as _compact_lock_text  # noqa: F401
from .showcase_scene_policy import compact_product_identity as _compact_product_identity  # noqa: F401
from .showcase_scene_policy import is_child_showcase as _is_child_showcase  # noqa: F401
from .showcase_scene_policy import join_lock_items as _join_lock_items  # noqa: F401
from .showcase_scene_policy import (
    showcase_scene_card_action_direction as _showcase_scene_card_action_direction,
)  # noqa: F401
from .showcase_scene_policy import (
    showcase_scene_card_camera_direction as _showcase_scene_card_camera_direction,
)  # noqa: F401
from .showcase_scene_policy import (
    showcase_scene_card_direction as _showcase_scene_card_direction,
)  # noqa: F401
from .showcase_scene_policy import (
    showcase_scene_card_scene_direction as _showcase_scene_card_scene_direction,
)  # noqa: F401
from .showcase_scene_policy import showcase_scene_card_text as _showcase_scene_card_text  # noqa: F401
from .showcase_scene_policy import (
    showcase_scene_framing_direction as _showcase_scene_framing_direction,
)  # noqa: F401
from .showcase_scene_policy import showcase_scene_label as _showcase_scene_label  # noqa: F401
from .showcase_scene_policy import (
    showcase_scene_render_direction as _showcase_scene_render_direction,
)  # noqa: F401
from .showcase_scene_policy import (
    showcase_visibility_policy as _showcase_visibility_policy,
)  # noqa: F401
from .showcase_scene_policy import text_has_any as _text_has_any  # noqa: F401
from .showcase_scene_policy import truncate_prompt_text as _truncate_prompt_text  # noqa: F401
from .showcase_shots import showcase_default_variant as _showcase_default_variant  # noqa: F401
from .showcase_shots import showcase_pick_shot_variants as _showcase_pick_shot_variants  # noqa: F401


logger = logging.getLogger("app.routes.workflows")

WORKFLOW_STEPS = (
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
)
SHOT_POOL_BY_BAND = freeze_mapping(
    {
        "young_adult": ADULT_POOL,
        "child": CHILD_POOL,
        "toddler": TODDLER_POOL,
    }
)


# Public workflow contracts.
candidate_prompt = _candidate_prompt
product_analysis_prompt = _product_analysis_prompt
seed_steps = _seed_steps
validate_owned_images = _validate_owned_images


# Public compatibility contracts.
STATIC_REWRITE_REPLACEMENTS = _STATIC_REWRITE_REPLACEMENTS
ShowcasePreflightProgressHook = _ShowcasePreflightProgressHook
compact_lock_text = _compact_lock_text
compact_product_identity = _compact_product_identity
composition_shooting_brief = _composition_shooting_brief
guarded_shooting_brief = _guarded_shooting_brief
is_child_showcase = _is_child_showcase
join_lock_items = _join_lock_items
prepare_durable_showcase_preflight = _prepare_durable_showcase_preflight
prepare_showcase_preflight_impl = _prepare_showcase_preflight_impl
preserve_safe_motion_rewrite_instruction = _preserve_safe_motion_rewrite_instruction
rewrite_instruction_replaces_scene_or_composition = (
    _rewrite_instruction_replaces_scene_or_composition
)
showcase_default_variant = _showcase_default_variant
showcase_garment_lock_prefix = _showcase_garment_lock_prefix
showcase_generation_context = _showcase_generation_context
showcase_pick_shot_variants = _showcase_pick_shot_variants
showcase_prompt = _showcase_prompt
showcase_prompt_brief = _showcase_prompt_brief
showcase_reference_image_ids = _showcase_reference_image_ids
showcase_request_input_json = _showcase_request_input_json
showcase_scene_card_action_direction = _showcase_scene_card_action_direction
showcase_scene_card_camera_direction = _showcase_scene_card_camera_direction
showcase_scene_card_direction = _showcase_scene_card_direction
showcase_scene_card_scene_direction = _showcase_scene_card_scene_direction
showcase_scene_card_text = _showcase_scene_card_text
showcase_scene_framing_direction = _showcase_scene_framing_direction
showcase_scene_label = _showcase_scene_label
showcase_scene_render_direction = _showcase_scene_render_direction
showcase_target_image_count = _showcase_target_image_count
showcase_visibility_policy = _showcase_visibility_policy
text_has_any = _text_has_any
truncate_prompt_text = _truncate_prompt_text
validate_accessory_preview_image = _validate_accessory_preview_image
