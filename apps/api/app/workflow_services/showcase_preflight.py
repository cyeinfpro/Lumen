"""Showcase preflight compatibility facade."""

# This module intentionally re-exports dependencies and private callables used by
# the historical routes.workflows facade and its monkeypatch-based tests.
# ruff: noqa: F401

from __future__ import annotations

import logging
import re
import sys
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

from ..routes._apparel_scene_planner import (
    build_garment_lock as _build_garment_lock,
    compose_image_prompt_with_gpt55 as _compose_image_prompt_with_gpt55,
    fallback_risk_review as _fallback_risk_review,
    plan_scene_cards_with_gpt55 as _plan_scene_cards_with_gpt55,
    review_prompt_risk_with_gpt55 as _review_prompt_risk_with_gpt55,
    resolve_scene_provider_order as _resolve_scene_provider_order,
    rules_fallback_planning as _rules_fallback_scene_planning,
    scene_fingerprint as _scene_fingerprint,
)
from ..routes._showcase_model_policy import (
    _age_direction,
    _compact_showcase_user_direction,
    _height_requirement,
    _infer_candidate_gender,
    _infer_model_height_cm,
    _model_diversity_anchor,
    _style_region_from_text,
)
from ..routes._showcase_shot_pool import (
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
from ..routes._showcase_shot_pool_adult import ADULT_POOL
from ..routes._showcase_shot_pool_kids import CHILD_POOL, TODDLER_POOL
from ..routes._showcase_template_policy import (
    _showcase_composition_direction,
    _showcase_framing_direction,
    _showcase_pose_direction,
    _showcase_render_direction,
    _template_requirement,
)
from .facade import FacadeRuntime
from .serialization import _dedupe_nonempty, _dict_or_empty, _http
from .showcase_context import (
    _prepare_durable_showcase_preflight,
    _showcase_generation_context,
    _showcase_request_input_json,
)
from .showcase_inputs import (
    _candidate_prompt,
    _product_analysis_prompt,
    _seed_steps,
    _showcase_reference_image_ids,
    _showcase_target_image_count,
    _validate_accessory_preview_image,
    _validate_owned_images,
)
from .showcase_orchestration import (
    _ShowcasePreflightProgressHook,
    _prepare_showcase_preflight_impl,
)
from .showcase_prompts import (
    _STATIC_REWRITE_REPLACEMENTS,
    _composition_shooting_brief,
    _guarded_shooting_brief,
    _preserve_safe_motion_rewrite_instruction,
    _rewrite_instruction_replaces_scene_or_composition,
    _showcase_garment_lock_prefix,
    _showcase_prompt,
    _showcase_prompt_brief,
)
from .showcase_runtime import FACADE_RUNTIME, runtime as _runtime
from .showcase_scene_policy import (
    _compact_lock_text,
    _compact_product_identity,
    _is_child_showcase,
    _join_lock_items,
    _showcase_scene_card_action_direction,
    _showcase_scene_card_camera_direction,
    _showcase_scene_card_direction,
    _showcase_scene_card_scene_direction,
    _showcase_scene_card_text,
    _showcase_scene_framing_direction,
    _showcase_scene_label,
    _showcase_scene_render_direction,
    _showcase_visibility_policy,
    _text_has_any,
    _truncate_prompt_text,
)
from .showcase_shots import _showcase_default_variant, _showcase_pick_shot_variants


logger = logging.getLogger("app.routes.workflows")

WORKFLOW_STEPS = [
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
]
SHOT_POOL_BY_BAND: dict[str, ShotPool] = {
    "young_adult": ADULT_POOL,
    "child": CHILD_POOL,
    "toddler": TODDLER_POOL,
}
