"""Structured workflow routes.

The apparel model showcase workflow is a project-style layer on top of the
existing durable image/text task system. Endpoints here own stage state and
approvals; generations/completions still run through the same worker queues so
refreshing or closing the browser does not lose progress.
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from functools import partial
from typing import Annotated, Any, Iterable

import httpx  # noqa: F401 - library sync facade dependency
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Query,
)
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core import billing as billing_core
from lumen_core.providers import (  # noqa: F401 - library sync facade dependencies
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec  # noqa: F401

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    Intent,
    MAX_PROMPT_CHARS,  # noqa: F401 - showcase facade dependency
    Role,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    ModelCandidate,
    ModelLibraryItem,
    new_uuid7,
    PosterMaster,
    PosterRender,
    QualityReport,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.model_image_metadata import (
    build_model_image_metadata,  # noqa: F401 - library facade dependency
    model_image_filename,  # noqa: F401 - library facade dependency
)
from lumen_core.schemas import (  # noqa: F401 - workflow facade compatibility exports
    AccessoryPlanIn,  # noqa: F401 - showcase facade dependency
    AccessoryPreviewCreateIn,
    AccessorySelectionIn,
    AgeSegment,
    ApparelModelLibraryAutoTagOut,
    ApparelModelLibraryBatchDeleteIn,
    ApparelModelLibraryBatchDeleteOut,
    ApparelModelLibraryGenerateIn,
    ApparelModelLibraryItemCreateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryItemPatchIn,
    ApparelModelLibraryJobItemOut,
    ApparelModelLibraryJobOut,
    ApparelModelLibraryJobsClearOut,
    ApparelModelLibraryJobsOut,
    ApparelModelLibraryListOut,
    ApparelModelLibrarySaveJobItemIn,
    ApparelModelLibrarySelectIn,
    ApparelModelLibrarySyncOut,
    ModelAgeSegment,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ChatParamsIn,
    CopyAnalysisApproveIn,
    GenerationOut,
    ImageOut,
    ImageParamsIn,
    ImageRevisionIn,
    ModelCandidateApproveIn,
    ModelCandidateSaveToLibraryIn,
    ModelCandidatesCreateIn,
    ModelCandidateOut,
    PosterDesignWorkflowCreateIn,
    PosterDesignWorkflowCreateOut,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMasterOut,
    PosterMastersCreateIn,
    PosterRenderOut,
    PosterRendersCreateIn,
    PosterReviseIn,
    ProductAnalysisApproveIn,
    QualityReportOut,
    ShowcaseImagesCreateIn,
    WorkflowRunListItemOut,
    WorkflowRunListOut,
    WorkflowRunOut,
    WorkflowRunPatchIn,
    WorkflowStepOut,
)

from ..db import affected_rows, get_db
from ..deps import CurrentUser, verify_csrf
from ..billing_cache_state import invalidate_balance_cache
from ..config import settings  # noqa: F401 - library facade dependency
from ..redis_client import get_redis
from ..runtime_settings import get_setting  # noqa: F401 - library facade dependency
from .messages import (
    _create_assistant_task,
    _publish_assistant_task,
    _publish_message_appended,
)
from ._showcase_template_policy import (  # noqa: F401
    TEMPLATE_LABELS,
    SCENE_ENVIRONMENT_TEMPLATES,
    _scene_environment_outdoor_phrase,
    _template_requirement,
    _RENDER_DIRECTIONS,
    _RENDER_DIRECTIONS_OUTDOOR,
    _showcase_render_direction,
    _POSE_DIRECTIONS,
    _showcase_pose_direction,
    _LIFESTYLE_TEMPLATES,
    _showcase_composition_direction,
    _SQUARE_OR_LANDSCAPE_RATIOS,
    _showcase_framing_direction,
)
from ._showcase_model_policy import (  # noqa: F401
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
from ..workflow_services.serialization import (  # noqa: F401
    _SHOWCASE_GPT55_REFERENCE_MAX_BYTES,
    _WORKFLOW_CURSOR_VERSION,
    _accessory_preview_request_key,
    _clean_optional_text,
    _clean_string_list,
    _clean_style_tags,
    _decode_workflow_cursor,
    _dedupe_nonempty,
    _dict_or_empty,
    _encode_workflow_cursor,
    _http,
    _iso_now,
    _now,
    _safe_datetime,
    _showcase_gpt55_reference_data_url,
    _storage_path,
    _storage_root,
)
from ..workflow_services.output_sync import (  # noqa: F401
    MODEL_CANDIDATE_COUNT,
    PRODUCT_ANALYSIS_FIELDS,
    _candidate_generated_image_ids,
    _clamp_score,
    _coerce_string_list,
    _extract_jsonish_value,
    _failed_generation_output,
    _generation_batch_outcome,
    _load_quality_reports,
    _lock_workflow_run_for_sync,
    _merge_quality_summary_payload,
    _normalize_product_analysis_payload,
    _quality_payload_from_text,
    _quality_summary_payload,
    _showcase_expected_image_count,
    _sync_quality_reports_from_tasks,
    _sync_workflow_outputs,
    _task_error_summary,
    _try_parse_json_text,
)
from ..workflow_services import library_sync as _library_sync_service
from ..workflow_services import showcase_preflight as _showcase_preflight_service
from ..workflow_services.facade import bind_facade
from ._showcase_shot_pool import (  # noqa: F401
    SHOT_CLASS_ORDER,
    age_soft_constraint as _age_soft_constraint,
    resolve_pool_band as _resolve_pool_band,
    select_variants as _select_shot_variants,
    shot_class_distribution as _shot_class_distribution,
)
from ._showcase_shot_pool_adult import ADULT_POOL  # noqa: F401
from ._apparel_library_reference import ReferenceProfile  # noqa: F401
from ._apparel_scene_planner import (  # noqa: F401
    build_garment_lock as _build_garment_lock,
    compose_image_prompt_with_gpt55 as _compose_image_prompt_with_gpt55,
    fallback_risk_review as _fallback_risk_review,
    plan_scene_cards_with_gpt55 as _plan_scene_cards_with_gpt55,
    review_prompt_risk_with_gpt55 as _review_prompt_risk_with_gpt55,
    resolve_scene_provider_order as _resolve_scene_provider_order,
    rules_fallback_planning as _rules_fallback_scene_planning,
    scene_fingerprint as _scene_fingerprint,
)
from .workflow_routes import apparel as _apparel_routes
from .workflow_routes import model_library as _model_library_routes
from .workflow_routes import poster as _poster_routes
from .workflow_routes._facade import _PublishBundle


router = APIRouter(prefix="/workflows", tags=["workflows"])
logger = logging.getLogger(__name__)
_apparel_routes.export_to_facade(sys.modules[__name__])
_model_library_routes.export_to_facade(sys.modules[__name__])
_poster_routes.export_to_facade(sys.modules[__name__])
POSTER_WORKFLOW_TYPE = _poster_routes.POSTER_WORKFLOW_TYPE
POSTER_WORKFLOW_STEPS = _poster_routes.POSTER_WORKFLOW_STEPS
_sync_poster_workflow_outputs = getattr(
    sys.modules[__name__],
    "_sync_poster_workflow_outputs",
)
_run_auto_tag_in_background = getattr(
    sys.modules[__name__],
    "_run_auto_tag_in_background",
)


class WorkflowAssetsAddIn(BaseModel):
    image_ids: list[str] = Field(min_length=1, max_length=64)
    asset_type: str = Field(default="project_asset", min_length=1, max_length=64)
    source_step_key: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=120)


_WORKFLOW_ASSET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
WORKFLOW_TYPE = "apparel_model_showcase"
WORKFLOW_STEPS = _showcase_preflight_service.WORKFLOW_STEPS
SHOT_POOL_BY_BAND = _showcase_preflight_service.SHOT_POOL_BY_BAND
ShotClass = _showcase_preflight_service.ShotClass
ShotPool = _showcase_preflight_service.ShotPool
ShotVariant = _showcase_preflight_service.ShotVariant
Template = _showcase_preflight_service.Template
CHILD_POOL = _showcase_preflight_service.CHILD_POOL
TODDLER_POOL = _showcase_preflight_service.TODDLER_POOL
_ShowcasePreflightProgressHook = (
    _showcase_preflight_service._ShowcasePreflightProgressHook
)
_STATIC_REWRITE_REPLACEMENTS = _showcase_preflight_service._STATIC_REWRITE_REPLACEMENTS
MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = (
    _library_sync_service.MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY
)
MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = (
    _library_sync_service.MODEL_LIBRARY_SYNC_PROXY_NAME_KEY
)
MODEL_LIBRARY_ROOT_KEY = _library_sync_service.MODEL_LIBRARY_ROOT_KEY
_GITHUB_API_HOST = _library_sync_service._GITHUB_API_HOST
_GITHUB_RAW_HOSTS = _library_sync_service._GITHUB_RAW_HOSTS
# apparel-model-library 常量 + 纯 helper 全部从 _apparel_library 导入。
# 这里 re-export 是为了让既有测试（apps/api/tests/test_workflows_route.py）
# 仍能通过 `workflows._normalize_age_segment` 等私有路径访问。
from app.routes._apparel_library import (  # noqa: E402, F401
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_APPEARANCES,
    MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS,
    MODEL_LIBRARY_FOLDER_BY_AGE,
    MODEL_LIBRARY_GENDER_SEGMENTS,
    MODEL_LIBRARY_GENERATE_COUNTS,
    MODEL_LIBRARY_GENERATE_STEP_KEY,
    MODEL_LIBRARY_GENERATE_WORKER_ACTION,
    MODEL_LIBRARY_IMAGE_SUFFIXES,
    MODEL_LIBRARY_MAX_BINARY_BYTES,
    MODEL_LIBRARY_MAX_GITHUB_DEPTH,
    MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES,
    MODEL_LIBRARY_MAX_GITHUB_FILES,
    MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES,
    MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES,
    MODEL_LIBRARY_MAX_INDEX_BYTES,
    MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES,
    MODEL_LIBRARY_SCHEMA_VERSION,
    MODEL_LIBRARY_SOURCES,
    MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS,
    MODEL_LIBRARY_SYNC_LEASE_SECONDS,
    MODEL_LIBRARY_SYNC_MODES,
    MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS,
    WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
    _SYNC_LOCK,
    _age_segment_from_folder_name,
    _gender_from_folder_name,
    _library_item_url,
    _model_library_folder_for_age,
    _model_library_sync_file_lock,
    _normalize_age_segment,
    _normalize_appearance,
    _normalize_model_gender,
    _preset_id_from_path,
    _title_from_preset_id,
)

_bind_library_service = partial(
    bind_facade,
    facade=sys.modules[__name__],
    runtime=_library_sync_service.FACADE_RUNTIME,
)
_bind_showcase_service = partial(
    bind_facade,
    facade=sys.modules[__name__],
    runtime=_showcase_preflight_service.FACADE_RUNTIME,
)

# Keep every extracted function route-bound so private monkeypatches resolve
# through the historical workflows facade. Classes remain direct aliases.
_write_json_atomic = _bind_library_service(_library_sync_service._write_json_atomic)
_fsync_dir = _bind_library_service(_library_sync_service._fsync_dir)
_read_file_bytes_bounded = _bind_library_service(
    _library_sync_service._read_file_bytes_bounded
)
_read_json_file = _bind_library_service(_library_sync_service._read_json_file)
_library_root = _bind_library_service(_library_sync_service._library_root)
_library_index_path = _bind_library_service(_library_sync_service._library_index_path)
_library_sync_state_path = _bind_library_service(
    _library_sync_service._library_sync_state_path
)
_library_sync_lock_path = _bind_library_service(
    _library_sync_service._library_sync_lock_path
)
_library_user_index_path = _bind_library_service(
    _library_sync_service._library_user_index_path
)
_default_library_index = _bind_library_service(
    _library_sync_service._default_library_index
)
_default_user_library_index = _bind_library_service(
    _library_sync_service._default_user_library_index
)
_default_sync_state = _bind_library_service(_library_sync_service._default_sync_state)
_github_contents_url = _bind_library_service(_library_sync_service._github_contents_url)
_sync_mode = _bind_library_service(_library_sync_service._sync_mode)
_model_library_http_client_kwargs = _bind_library_service(
    _library_sync_service._model_library_http_client_kwargs
)
_resolve_model_library_sync_proxy = _bind_library_service(
    _library_sync_service._resolve_model_library_sync_proxy
)
_can_sync_library = _bind_library_service(_library_sync_service._can_sync_library)
_sync_state_out = _bind_library_service(_library_sync_service._sync_state_out)
_model_library_item_out = _bind_library_service(
    _library_sync_service._model_library_item_out
)
_load_global_library_index = _bind_library_service(
    _library_sync_service._load_global_library_index
)
_load_user_library_index = _bind_library_service(
    _library_sync_service._load_user_library_index
)
_save_global_library_index = _bind_library_service(
    _library_sync_service._save_global_library_index
)
_save_user_library_index = _bind_library_service(
    _library_sync_service._save_user_library_index
)
_remove_user_library_item_from_legacy_index = _bind_library_service(
    _library_sync_service._remove_user_library_item_from_legacy_index
)
_hide_preset_in_legacy_user_library_index = _bind_library_service(
    _library_sync_service._hide_preset_in_legacy_user_library_index
)
_save_sync_state = _bind_library_service(_library_sync_service._save_sync_state)
_model_library_row_to_dict = _bind_library_service(
    _library_sync_service._model_library_row_to_dict
)
_legacy_library_item_insert_values = _bind_library_service(
    _library_sync_service._legacy_library_item_insert_values
)
_ensure_legacy_user_library_migrated = _bind_library_service(
    _library_sync_service._ensure_legacy_user_library_migrated
)
_load_user_library_items = _bind_library_service(
    _library_sync_service._load_user_library_items
)
_load_user_hidden_preset_ids = _bind_library_service(
    _library_sync_service._load_user_hidden_preset_ids
)
_combined_library_items = _bind_library_service(
    _library_sync_service._combined_library_items
)
_filter_library_items = _bind_library_service(
    _library_sync_service._filter_library_items
)
_find_library_item = _bind_library_service(_library_sync_service._find_library_item)
_guess_mime = _bind_library_service(_library_sync_service._guess_mime)
_sha256_file_bounded = _bind_library_service(_library_sync_service._sha256_file_bounded)
_open_library_storage_file = _bind_library_service(
    _library_sync_service._open_library_storage_file
)
_stream_file = _bind_library_service(_library_sync_service._stream_file)
_library_binary_response = _bind_library_service(
    _library_sync_service._library_binary_response
)
_preset_storage_key = _bind_library_service(_library_sync_service._preset_storage_key)
_preset_thumb_storage_key = _bind_library_service(
    _library_sync_service._preset_thumb_storage_key
)
_write_bytes_replace = _bind_library_service(_library_sync_service._write_bytes_replace)
_ModelLibrarySyncLimitExceeded = _library_sync_service._ModelLibrarySyncLimitExceeded
_ModelLibrarySyncLeaseLost = _library_sync_service._ModelLibrarySyncLeaseLost
_fetch_bytes = _bind_library_service(_library_sync_service._fetch_bytes)
_fetch_github_download_bytes = _bind_library_service(
    _library_sync_service._fetch_github_download_bytes
)
_github_api_child_url = _bind_library_service(
    _library_sync_service._github_api_child_url
)
_decoded_url_path_segments = _bind_library_service(
    _library_sync_service._decoded_url_path_segments
)
_validate_github_contents_url = _bind_library_service(
    _library_sync_service._validate_github_contents_url
)
_validate_github_download_url = _bind_library_service(
    _library_sync_service._validate_github_download_url
)
_walk_github_contents = _bind_library_service(
    _library_sync_service._walk_github_contents
)
_metadata_from_github_file = _bind_library_service(
    _library_sync_service._metadata_from_github_file
)
_github_entry_size = _bind_library_service(_library_sync_service._github_entry_size)
_sync_lease_owner = _bind_library_service(_library_sync_service._sync_lease_owner)
_claim_library_sync_lease_sync = _bind_library_service(
    _library_sync_service._claim_library_sync_lease_sync
)
_claim_library_sync_lease = _bind_library_service(
    _library_sync_service._claim_library_sync_lease
)
_renew_library_sync_lease_sync = _bind_library_service(
    _library_sync_service._renew_library_sync_lease_sync
)
_renew_library_sync_lease = _bind_library_service(
    _library_sync_service._renew_library_sync_lease
)
_complete_library_sync_lease_sync = _bind_library_service(
    _library_sync_service._complete_library_sync_lease_sync
)
_complete_library_sync_lease = _bind_library_service(
    _library_sync_service._complete_library_sync_lease
)
_fail_library_sync_lease_sync = _bind_library_service(
    _library_sync_service._fail_library_sync_lease_sync
)
_fail_library_sync_lease = _bind_library_service(
    _library_sync_service._fail_library_sync_lease
)
_cached_sync_response = _bind_library_service(
    _library_sync_service._cached_sync_response
)
_sync_library_presets_from_github_folder = _bind_library_service(
    _library_sync_service._sync_library_presets_from_github_folder
)
_do_sync_library_presets = _bind_library_service(
    _library_sync_service._do_sync_library_presets
)
_owned_image = _bind_library_service(_library_sync_service._owned_image)
_image_url = _bind_library_service(_library_sync_service._image_url)
_model_library_download_filename = _bind_library_service(
    _library_sync_service._model_library_download_filename
)
_model_library_image_metadata_from_fields = _bind_library_service(
    _library_sync_service._model_library_image_metadata_from_fields
)
_create_user_image_from_preset = _bind_library_service(
    _library_sync_service._create_user_image_from_preset
)
_add_user_library_item = _bind_library_service(
    _library_sync_service._add_user_library_item
)

_showcase_prompt_brief = _bind_showcase_service(
    _showcase_preflight_service._showcase_prompt_brief
)
_showcase_reference_image_ids = _bind_showcase_service(
    _showcase_preflight_service._showcase_reference_image_ids
)
_validate_accessory_preview_image = _bind_showcase_service(
    _showcase_preflight_service._validate_accessory_preview_image
)
_showcase_target_image_count = _bind_showcase_service(
    _showcase_preflight_service._showcase_target_image_count
)
_validate_owned_images = _bind_showcase_service(
    _showcase_preflight_service._validate_owned_images
)
_seed_steps = _bind_showcase_service(_showcase_preflight_service._seed_steps)
_product_analysis_prompt = _bind_showcase_service(
    _showcase_preflight_service._product_analysis_prompt
)
_candidate_prompt = _bind_showcase_service(
    _showcase_preflight_service._candidate_prompt
)
_showcase_scene_label = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_label
)
_showcase_scene_card_direction = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_card_direction
)
_showcase_scene_card_scene_direction = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_card_scene_direction
)
_showcase_scene_card_action_direction = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_card_action_direction
)
_showcase_scene_card_camera_direction = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_card_camera_direction
)
_showcase_scene_card_text = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_card_text
)
_text_has_any = _bind_showcase_service(_showcase_preflight_service._text_has_any)
_is_child_showcase = _bind_showcase_service(
    _showcase_preflight_service._is_child_showcase
)
_showcase_scene_render_direction = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_render_direction
)
_showcase_scene_framing_direction = _bind_showcase_service(
    _showcase_preflight_service._showcase_scene_framing_direction
)
_showcase_visibility_policy = _bind_showcase_service(
    _showcase_preflight_service._showcase_visibility_policy
)
_truncate_prompt_text = _bind_showcase_service(
    _showcase_preflight_service._truncate_prompt_text
)
_join_lock_items = _bind_showcase_service(_showcase_preflight_service._join_lock_items)
_compact_lock_text = _bind_showcase_service(
    _showcase_preflight_service._compact_lock_text
)
_compact_product_identity = _bind_showcase_service(
    _showcase_preflight_service._compact_product_identity
)
_showcase_garment_lock_prefix = _bind_showcase_service(
    _showcase_preflight_service._showcase_garment_lock_prefix
)
_showcase_prompt = _bind_showcase_service(_showcase_preflight_service._showcase_prompt)
_showcase_default_variant = _bind_showcase_service(
    _showcase_preflight_service._showcase_default_variant
)
_showcase_pick_shot_variants = _bind_showcase_service(
    _showcase_preflight_service._showcase_pick_shot_variants
)
_composition_shooting_brief = _bind_showcase_service(
    _showcase_preflight_service._composition_shooting_brief
)
_guarded_shooting_brief = _bind_showcase_service(
    _showcase_preflight_service._guarded_shooting_brief
)
_preserve_safe_motion_rewrite_instruction = _bind_showcase_service(
    _showcase_preflight_service._preserve_safe_motion_rewrite_instruction
)
_rewrite_instruction_replaces_scene_or_composition = _bind_showcase_service(
    _showcase_preflight_service._rewrite_instruction_replaces_scene_or_composition
)
_prepare_showcase_preflight_impl = _bind_showcase_service(
    _showcase_preflight_service._prepare_showcase_preflight_impl
)
_showcase_request_input_json = _bind_showcase_service(
    _showcase_preflight_service._showcase_request_input_json
)
_showcase_generation_context = _bind_showcase_service(
    _showcase_preflight_service._showcase_generation_context
)
_prepare_durable_showcase_preflight = _bind_showcase_service(
    _showcase_preflight_service._prepare_durable_showcase_preflight
)

HIDDEN_PROJECT_WORKFLOW_TYPES = frozenset(
    {
        WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        "poster_style_library_generate",
    }
)


def _primary_candidate_image_id(candidate: ModelCandidate) -> str | None:
    if candidate.contact_sheet_image_id:
        return candidate.contact_sheet_image_id
    brief = candidate.model_brief_json or {}
    candidate_image_ids = brief.get("candidate_image_ids")
    if isinstance(candidate_image_ids, list):
        for image_id in candidate_image_ids:
            if isinstance(image_id, str) and image_id:
                return image_id
    return None


def _infer_age_segment_from_workflow(run: WorkflowRun) -> str:
    meta = run.metadata_jsonb or {}
    profile = meta.get("model_profile")
    if isinstance(profile, dict):
        age = _normalize_age_segment(profile.get("age_segment"))
        if age != "user_favorites":
            return age
    return _infer_age_segment_from_text(run.user_prompt or "")


def _metadata_model_profile_from_prompt(text: str) -> dict[str, Any]:
    gender = None
    if "女性" in text or "女" in text:
        gender = "female"
    elif "男性" in text or "男" in text:
        gender = "male"
    appearance = None
    for zh, value in (
        ("欧美", "european"),
        ("亚洲", "asian"),
        ("拉美", "latin"),
        ("中东", "middle_eastern"),
        ("非洲", "african"),
    ):
        if zh in text:
            appearance = value
            break
    return {
        "age_segment": _normalize_age_segment(_infer_age_segment_from_text(text)),
        "gender": gender,
        "appearance_direction": appearance,
    }


def _infer_age_segment_from_text(text: str) -> str:
    if "幼儿" in text:
        return "toddler"
    if any(word in text for word in ("儿童", "童装", "小朋友", "孩子")):
        return "child"
    if "青少年" in text:
        return "teen"
    if "青年" in text:
        return "young_adult"
    if "中年" in text or "中老年" in text:
        return "middle_aged"
    if "老年" in text:
        return "senior"
    if "熟龄" in text or "成年" in text:
        return "adult"
    return "user_favorites"


async def _get_owned_conversation(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise _http("not_found", "conversation not found", 404)
    return conv


async def _get_or_create_workflow_conversation(
    db: AsyncSession,
    *,
    user: User,
    conversation_id: str | None,
    title: str,
    workflow_type: str = WORKFLOW_TYPE,
) -> Conversation:
    if conversation_id:
        conv = await _get_owned_conversation(
            db, user_id=user.id, conversation_id=conversation_id
        )
        params = dict(conv.default_params or {})
        params["workflow_type"] = workflow_type
        params["hidden_from_conversations"] = True
        conv.default_params = params
        return conv
    conv = Conversation(
        user_id=user.id,
        title=title,
        archived=True,
        default_params={
            "workflow_type": workflow_type,
            "hidden_from_conversations": True,
        },
    )
    db.add(conv)
    await db.flush()
    return conv


async def _get_run(
    db: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    lock: bool = False,
) -> WorkflowRun:
    stmt = select(WorkflowRun).where(
        WorkflowRun.id == run_id,
        WorkflowRun.user_id == user_id,
        WorkflowRun.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise _http("not_found", "workflow not found", 404)
    return run


async def _load_steps(
    db: AsyncSession,
    run_id: str,
    *,
    lock: bool = False,
) -> list[WorkflowStep]:
    stmt = select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
    if lock:
        stmt = stmt.with_for_update()
    rows = (await db.execute(stmt)).scalars().all()
    # apparel 与 poster 的 step_key 互不重叠；合并成一张顺序表，
    # 未识别的 key 保留尾部稳定顺序。
    order: dict[str, int] = {}
    for idx, key in enumerate(WORKFLOW_STEPS):
        order[key] = idx
    for idx, key in enumerate(POSTER_WORKFLOW_STEPS):
        order[key] = len(WORKFLOW_STEPS) + idx
    return sorted(rows, key=lambda s: order.get(s.step_key, 999))


async def _step(db: AsyncSession, run_id: str, step_key: str) -> WorkflowStep:
    row = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run_id,
                WorkflowStep.step_key == step_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("workflow_corrupt", f"missing workflow step: {step_key}", 500)
    return row


async def _workflow_steps_and_candidates(
    db: AsyncSession,
    run: WorkflowRun,
) -> tuple[list[WorkflowStep], list[ModelCandidate]]:
    steps = await _load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    return steps, candidates


def _workflow_direct_task_ids(
    steps: Iterable[WorkflowStep],
    candidates: Iterable[ModelCandidate],
) -> list[str]:
    return _dedupe_nonempty(
        [
            *(task_id for step in steps for task_id in (step.task_ids or [])),
            *(
                task_id
                for candidate in candidates
                for task_id in (candidate.task_ids or [])
            ),
        ]
    )


def _workflow_direct_image_ids(
    steps: Iterable[WorkflowStep],
    candidates: Iterable[ModelCandidate],
) -> list[str]:
    return _dedupe_nonempty(
        [
            *(image_id for step in steps for image_id in (step.image_ids or [])),
            *(
                image_id
                for candidate in candidates
                for image_id in _candidate_reference_image_ids(candidate)
            ),
        ]
    )


def _candidate_reference_image_ids(candidate: ModelCandidate) -> list[str]:
    brief = getattr(candidate, "model_brief_json", None) or {}
    raw_candidate_ids = brief.get("candidate_image_ids")
    candidate_image_ids = (
        raw_candidate_ids if isinstance(raw_candidate_ids, list) else []
    )
    return _dedupe_nonempty(
        [
            *(
                image_id
                for image_id in candidate_image_ids
                if isinstance(image_id, str)
            ),
            *(
                image_id
                for image_id in (
                    candidate.contact_sheet_image_id,
                    candidate.portrait_image_id,
                    candidate.front_image_id,
                    candidate.side_image_id,
                    candidate.back_image_id,
                )
                if isinstance(image_id, str)
            ),
        ]
    )


async def _workflow_generation_rows_from_task_ids(
    db: AsyncSession,
    *,
    user_id: str,
    task_ids: list[str],
    include_dual_bonus: bool,
) -> list[Generation]:
    task_ids = _dedupe_nonempty(task_ids)
    if not task_ids:
        return []
    base_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.id.in_(task_ids),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not include_dual_bonus:
        return base_generations
    bonus_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [*base_generations, *bonus_generations]


async def _release_soft_deleted_task_hold(
    db: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
    reason: str,
) -> bool:
    try:
        tx = await billing_core.release(
            db,
            user_id,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=f"workflow_delete:{ref_type}:{ref_id}",
            meta={"reason": reason},
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _workflow_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


def _cleanup_string_list(cleanup: dict[str, Any], key: str) -> list[str]:
    values = cleanup.get(key)
    if not isinstance(values, list):
        return []
    return _dedupe_nonempty(value for value in values if isinstance(value, str))


def _empty_workflow_generated_cleanup() -> dict[str, Any]:
    return {
        "images_deleted": 0,
        "generations_canceled": 0,
        "completions_canceled": 0,
        "holds_released": 0,
        "queued_generation_ids": [],
        "running_generation_ids": [],
        "streaming_completion_ids": [],
    }


async def _release_workflow_generation_queue_state(redis: Any, task_id: str) -> None:
    from .tasks import _release_generation_queue_state

    await _release_generation_queue_state(redis, task_id)


async def _post_commit_workflow_generated_cleanup(
    *,
    user_id: str,
    cleanup: dict[str, Any],
) -> None:
    queued_generation_ids = _cleanup_string_list(cleanup, "queued_generation_ids")
    running_generation_ids = _cleanup_string_list(cleanup, "running_generation_ids")
    streaming_completion_ids = _cleanup_string_list(cleanup, "streaming_completion_ids")
    released_holds = cleanup.get("holds_released")
    if (
        not queued_generation_ids
        and not running_generation_ids
        and not streaming_completion_ids
    ):
        if isinstance(released_holds, int) and released_holds > 0:
            try:
                await invalidate_balance_cache(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "workflow delete balance cache invalidation failed user=%s err=%s",
                    user_id,
                    exc,
                )
        return

    redis = get_redis()
    for task_id in queued_generation_ids:
        try:
            await _release_workflow_generation_queue_state(redis, task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow delete image_queue release failed task=%s err=%s",
                task_id,
                exc,
            )
    for task_id in [*running_generation_ids, *streaming_completion_ids]:
        try:
            await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow delete cancel signal failed task=%s err=%s",
                task_id,
                exc,
            )
    if isinstance(released_holds, int) and released_holds > 0:
        try:
            await invalidate_balance_cache(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workflow delete balance cache invalidation failed user=%s err=%s",
                user_id,
                exc,
            )


def _cancel_workflow_generation_rows(
    generation_rows: Iterable[Generation],
    *,
    deleted_at: datetime,
    cancel_message: str,
) -> tuple[list[Generation], list[str], list[str]]:
    queued_rows: list[Generation] = []
    queued_ids: list[str] = []
    running_ids: list[str] = []
    for generation in generation_rows:
        # Running rows retain their hold until the worker confirms that the
        # upstream awaitable stopped; only queued rows finalize here.
        if generation.status == GenerationStatus.QUEUED.value:
            queued_rows.append(generation)
            queued_ids.append(generation.id)
            generation.status = GenerationStatus.CANCELED.value
            generation.progress_stage = "finalizing"
            generation.finished_at = deleted_at
            generation.error_code = "cancelled"
            generation.error_message = cancel_message
        elif generation.status == GenerationStatus.RUNNING.value:
            running_ids.append(generation.id)
    return queued_rows, queued_ids, running_ids


async def _soft_delete_workflow_generated_images(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    deleted_at: datetime,
    cancel_message: str,
    account_mode: str = "wallet",
) -> dict[str, Any]:
    """Soft-delete images produced by a workflow and cancel its active tasks.

    Images explicitly saved into the user's model library are preserved; those
    are no longer just transient task outputs.
    """
    if getattr(run, "deleted_at", None) is not None:
        return _empty_workflow_generated_cleanup()

    steps, candidates = await _workflow_steps_and_candidates(db, run)
    task_ids = _workflow_direct_task_ids(steps, candidates)
    image_ids = _workflow_direct_image_ids(steps, candidates)
    generation_rows = await _workflow_generation_rows_from_task_ids(
        db,
        user_id=run.user_id,
        task_ids=task_ids,
        include_dual_bonus=True,
    )
    generation_ids = _dedupe_nonempty(generation.id for generation in generation_rows)

    canceled_generations = 0
    canceled_generation_rows: list[Generation] = []
    queued_generation_rows: list[Generation] = []
    queued_generation_ids: list[str] = []
    running_generation_ids: list[str] = []
    if generation_ids:
        canceled_generation_rows = list(
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == run.user_id,
                        Generation.id.in_(generation_ids),
                        Generation.status.in_(
                            [
                                GenerationStatus.QUEUED.value,
                                GenerationStatus.RUNNING.value,
                            ]
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        (
            queued_generation_rows,
            queued_generation_ids,
            running_generation_ids,
        ) = _cancel_workflow_generation_rows(
            canceled_generation_rows,
            deleted_at=deleted_at,
            cancel_message=cancel_message,
        )
        canceled_generations = len(canceled_generation_rows)

    canceled_completions = 0
    canceled_completion_rows: list[Completion] = []
    queued_completion_rows: list[Completion] = []
    streaming_completion_ids: list[str] = []
    if task_ids:
        canceled_completion_rows = list(
            (
                await db.execute(
                    select(Completion)
                    .where(
                        Completion.user_id == run.user_id,
                        Completion.id.in_(task_ids),
                        Completion.status.in_(
                            [
                                CompletionStatus.QUEUED.value,
                                CompletionStatus.STREAMING.value,
                            ]
                        ),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for completion in canceled_completion_rows:
            if completion.status == CompletionStatus.QUEUED.value:
                queued_completion_rows.append(completion)
                completion.status = CompletionStatus.CANCELED.value
                completion.progress_stage = "finalizing"
                completion.finished_at = deleted_at
                completion.error_code = "cancelled"
                completion.error_message = cancel_message
            elif completion.status == CompletionStatus.STREAMING.value:
                streaming_completion_ids.append(completion.id)
        canceled_completions = len(canceled_completion_rows)

    released_holds = 0
    should_release_queued_holds = account_mode == "wallet"
    if (
        not should_release_queued_holds
        and (queued_generation_rows or queued_completion_rows)
        and await _workflow_wallet_exists(db, run.user_id)
    ):
        should_release_queued_holds = True
    if should_release_queued_holds:
        for generation in queued_generation_rows:
            released_holds += int(
                await _release_soft_deleted_task_hold(
                    db,
                    user_id=run.user_id,
                    ref_type="generation",
                    ref_id=billing_core.generation_billing_ref_id(generation),
                    reason=cancel_message,
                )
            )
        for completion in queued_completion_rows:
            released_holds += int(
                await _release_soft_deleted_task_hold(
                    db,
                    user_id=run.user_id,
                    ref_type="completion",
                    ref_id=billing_core.completion_billing_ref_id(completion),
                    reason=cancel_message,
                )
            )

    deleted_images = 0
    image_matchers = []
    if generation_ids:
        image_matchers.append(Image.owner_generation_id.in_(generation_ids))
    if image_ids:
        image_matchers.append(Image.id.in_(image_ids))
    if image_matchers:
        preserved_library_images = select(ModelLibraryItem.image_id).where(
            ModelLibraryItem.user_id == run.user_id,
            ModelLibraryItem.image_id.is_not(None),
        )
        result = await db.execute(
            update(Image)
            .where(
                Image.user_id == run.user_id,
                Image.deleted_at.is_(None),
                or_(*image_matchers),
                ~Image.id.in_(preserved_library_images),
            )
            .values(deleted_at=deleted_at)
            .execution_options(synchronize_session=False)
        )
        deleted_images = affected_rows(result)

    cleanup = _empty_workflow_generated_cleanup()
    cleanup.update(
        {
            "images_deleted": deleted_images,
            "generations_canceled": canceled_generations,
            "completions_canceled": canceled_completions,
            "holds_released": released_holds,
            "queued_generation_ids": queued_generation_ids,
            "running_generation_ids": running_generation_ids,
            "streaming_completion_ids": streaming_completion_ids,
        }
    )
    return cleanup


def _revision_prompt(
    *,
    instruction: str,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = (
        ", ".join(str(x) for x in must_preserve)
        if isinstance(must_preserve, list)
        else ""
    )
    return (
        "请根据用户要求返修这张服饰电商模特图。"
        "【商品 1:1 还原】衣服以白底产品图为准，不要改款、改色、改廓形、改领口袖型衣长、改图案/logo、改纽扣拉链口袋缝线。"
        "保持已确认模特的人脸、发型、身材比例和整体身份不变。"
        "需要逐项保留的商品细节："
        f"{preserve or '颜色、版型、领口、袖型、长度、logo/图案、口袋、纽扣、缝线'}。"
        f"返修要求：{instruction}，仅按此改动，不动商品和模特身份。"
        f"参考模特方案：{selected_candidate.id}。"
    )


def _accessory_preview_prompt(
    *,
    accessory_plan: dict[str, Any],
    style_prompt: str,
    age_context: str = "",
) -> str:
    items = accessory_plan.get("items")
    item_list = _clean_string_list(
        (str(item) for item in items) if isinstance(items, list) else [],
        max_items=8,
        max_len=80,
    )
    item_text = "、".join(item_list)
    strength = str(accessory_plan.get("strength") or "subtle")
    enabled = bool(accessory_plan.get("enabled", True))
    accessory_line = (
        f"只添加这些配饰：{item_text}。不要自动新增未列出的包、帽子、腰带、眼镜、首饰、鞋子或道具。"
        if enabled and item_text
        else "不添加新配饰；保持参考图里的基础造型干净稳定。"
    )
    style = style_prompt.strip() or "干净高级的电商参考图，克制自然"
    age_direction = _accessory_age_direction(" ".join([age_context, style]).strip())
    return (
        "请根据上传的已确认模特四宫格参考图，生成一张新的白底模特配饰四宫格参考图。"
        "核心目标是在同一个模特、同一套基础中性服装上预览配饰效果，供后续商品融合图参考；"
        "不要生成最终商品穿搭图。"
        "画面必须保持 2x2 四宫格参考图，不要拆成多张图；"
        "四格内容固定为：正面全身、侧面全身、背面全身、近景头像；"
        "布局顺序为左上正面全身、右上侧面全身、左下背面全身、右下近景头像。"
        "每一格都用白底或近白底、同一摄影棚光线、清晰边界；"
        "不要文字标签、编号、边框标题或水印。"
        "严格保持参考图里的同一张脸、发型、肤色、年龄感、身高、身材比例、肢体长度、"
        "体态和基础服装；不要换人，不要美颜成网红脸，不要改成时装大片造型。"
        "模特只穿原参考图中的简单中性基础服装，不要穿商品图中的衣服，"
        "不要出现任何商品服饰、logo、图案或新衣服细节。"
        f"配饰要求：{accessory_line}"
        f"配饰强度：{_accessory_strength_direction(strength)}。"
        "配饰必须真实贴合身体和透视：耳饰在耳垂位置，项链贴合颈部，包带、腰带、鞋帽与姿态一致；"
        "不能漂浮、变形、穿模，不能遮挡脸、手、脚和身体轮廓。"
        "不要让配饰遮挡未来商品展示区域；不要添加多余道具、家具、背景场景或手持物，"
        "除非明确列在配饰里。"
        f"年龄与风格：{age_direction} "
        f"补充方向：{style}。"
        "输出风格：高质量真实商业摄影参考图，清晰、干净、可作为后续服饰电商生成的稳定参考。"
    )


def _accessory_plan_from_product_analysis(
    product_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    raw_items = (product_analysis or {}).get("styling_recommendations")
    items = _clean_string_list(_coerce_string_list(raw_items), max_items=3, max_len=80)
    return {
        "enabled": True,
        "items": items,
        "strength": "subtle",
    }


def _coerce_accessory_plan_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    enabled = bool(value.get("enabled", True))
    strength = str(value.get("strength") or "subtle")
    if strength not in {"subtle", "medium", "strong"}:
        strength = "subtle"
    items = value.get("items")
    return {
        "enabled": enabled,
        "items": _clean_string_list(
            (str(item) for item in items) if isinstance(items, list) else [],
            max_items=12,
            max_len=80,
        ),
        "strength": strength,
    }


async def _create_workflow_task(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    intent: Intent,
    text: str,
    attachment_ids: list[str],
    idempotency_key: str,
    workflow_run_id: str,
    workflow_step_key: str,
    image_params: ImageParamsIn | None = None,
    chat_params: ChatParamsIn | None = None,
    workflow_meta: dict[str, Any] | None = None,
) -> tuple[_PublishBundle, str | None, list[str]]:
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": text,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_run_id": workflow_run_id,
            "workflow_step_key": workflow_step_key,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=idempotency_key[:64],
        image_params=image_params or ImageParamsIn(),
        chat_params=chat_params or ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=text,
    )

    meta = {
        "workflow_run_id": workflow_run_id,
        "workflow_type": WORKFLOW_TYPE,
        "workflow_step_key": workflow_step_key,
        **(workflow_meta or {}),
    }
    if result.completion_id:
        comp = await db.get(Completion, result.completion_id)
        if comp is not None:
            req = dict(comp.upstream_request or {})
            req.update(meta)
            comp.upstream_request = req
    for generation_id in result.generation_ids:
        gen = await db.get(Generation, generation_id)
        if gen is not None:
            req = dict(gen.upstream_request or {})
            req.update(meta)
            gen.upstream_request = req

    bundle = _PublishBundle(
        assistant_msg_id=result.assistant_msg.id,
        message_ids=[user_msg.id, result.assistant_msg.id],
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    return bundle, result.completion_id, result.generation_ids


async def _publish_bundles(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    bundles: list[_PublishBundle],
) -> None:
    redis = get_redis()
    for bundle in bundles:
        await _publish_message_appended(
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            message_ids=bundle.message_ids,
        )
        await _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            assistant_msg_id=bundle.assistant_msg_id,
            outbox_payloads=bundle.outbox_payloads,
            outbox_rows=bundle.outbox_rows,
        )


def _fixed_size_for_quality(aspect_ratio: str, final_quality: str) -> str | None:
    if final_quality == "standard":
        return None
    high: dict[str, str] = {
        "1:1": "2048x2048",
        "4:5": "1600x2000",
        "3:4": "1536x2048",
        "4:3": "2048x1536",
        "16:9": "2560x1440",
        "9:16": "1440x2560",
        "3:2": "2016x1344",
        "2:3": "1344x2016",
        "21:9": "2688x1152",
        "9:21": "1152x2688",
    }
    four_k: dict[str, str] = {
        "1:1": "2880x2880",
        "4:5": "2560x3200",
        "3:4": "2448x3264",
        "4:3": "3264x2448",
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "3:2": "3504x2336",
        "2:3": "2336x3504",
        "21:9": "3808x1632",
        "9:21": "1632x3808",
    }
    return (four_k if final_quality == "4k" else high).get(aspect_ratio, high["4:5"])


def _image_params(
    *,
    aspect_ratio: str = "4:5",
    count: int = 1,
    render_quality: str = "high",
    final_quality: str | None = None,
    fast: bool = False,
) -> ImageParamsIn:
    fixed = _fixed_size_for_quality(aspect_ratio, final_quality or "high")
    return ImageParamsIn(
        aspect_ratio=aspect_ratio,  # type: ignore[arg-type]
        size_mode="fixed" if fixed else "auto",
        fixed_size=fixed,
        count=count,
        fast=fast,
        render_quality=render_quality,  # type: ignore[arg-type]
        output_format="jpeg",
        output_compression=100,
        background="opaque",
        moderation="low",
    )


def _candidate_image_params() -> ImageParamsIn:
    params = _image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
    )


def _accessory_preview_image_params() -> ImageParamsIn:
    params = _image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        final_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
    )


def _merge_product_corrections(
    product_output: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    final = dict(product_output or {})
    raw_corrections = corrections if isinstance(corrections, dict) else {}
    for key, value in raw_corrections.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw_corrections
    final["confirmed_at"] = _now().isoformat()
    return final


def _next_action_for(run: WorkflowRun) -> str:
    if run.status == "completed":
        return "查看交付"
    if run.type == POSTER_WORKFLOW_TYPE:
        return {
            "copy_analysis": "确认海报文案",
            "master_generation": "生成母版方案",
            "master_approval": "选定母版",
            "multi_size_generation": "生成/确认多尺寸",
            "delivery": "下载海报成品",
        }.get(run.current_step, "继续海报项目")
    return {
        "product_analysis": "确认商品约束",
        "model_settings": "生成模特候选",
        "model_candidates": "等待模特候选",
        "model_approval": "确认模特",
        "showcase_generation": "开始生成展示图",
        "quality_review": "查看质检",
        "delivery": "下载最终图",
    }.get(run.current_step, "继续项目")


def _workflow_asset_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("workflow_run_id") or ""),
        str(record.get("image_id") or ""),
        str(record.get("asset_type") or ""),
        str(record.get("source_step_key") or ""),
    )


def _workflow_asset_records(
    *,
    run: WorkflowRun,
    image_ids: list[str],
    asset_type: str,
    source_step_key: str,
    label: str | None,
    added_at: datetime,
) -> list[dict[str, Any]]:
    clean_label = (label or "").strip() or None
    records: list[dict[str, Any]] = []
    for image_id in image_ids:
        record: dict[str, Any] = {
            "workflow_run_id": run.id,
            "workflow_type": run.type,
            "project_title": run.title,
            "image_id": image_id,
            "asset_type": asset_type,
            "source_step_key": source_step_key,
            "added_at": added_at.isoformat(),
        }
        if clean_label:
            record["label"] = clean_label
        records.append(record)
    return records


def _merge_workflow_asset_metadata(
    metadata: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(metadata or {})
    existing_raw = payload.get("assets")
    existing = (
        [
            dict(record)
            for record in existing_raw
            if isinstance(record, dict) and isinstance(record.get("image_id"), str)
        ]
        if isinstance(existing_raw, list)
        else []
    )
    replace_keys = {_workflow_asset_key(record) for record in records}
    merged = [
        record for record in existing if _workflow_asset_key(record) not in replace_keys
    ]
    merged.extend(records)
    payload["assets"] = merged[-200:]
    payload["asset_image_ids"] = _dedupe_nonempty(
        str(record.get("image_id") or "") for record in payload["assets"]
    )
    payload["asset_count"] = len(payload["asset_image_ids"])
    return payload


def _merge_image_workflow_asset_metadata(
    metadata: dict[str, Any] | None,
    record: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(metadata or {})
    existing_raw = payload.get("workflow_assets")
    existing = (
        [
            dict(item)
            for item in existing_raw
            if isinstance(item, dict) and isinstance(item.get("workflow_run_id"), str)
        ]
        if isinstance(existing_raw, list)
        else []
    )
    key = _workflow_asset_key(record)
    merged = [item for item in existing if _workflow_asset_key(item) != key]
    merged.append(record)
    payload["workflow_assets"] = merged[-50:]
    payload["latest_workflow_asset"] = record
    return payload


async def _attach_workflow_assets(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    user_id: str,
    image_ids: list[str],
    asset_type: str,
    source_step_key: str,
    label: str | None = None,
    added_at: datetime | None = None,
) -> list[dict[str, Any]]:
    clean_asset_type = (asset_type or "").strip()
    if not _WORKFLOW_ASSET_TYPE_RE.fullmatch(clean_asset_type):
        raise _http("invalid_asset_type", "asset_type is invalid", 422)
    clean_step_key = (source_step_key or "").strip()
    if not clean_step_key:
        raise _http("missing_source_step", "source_step_key is required", 422)
    deduped_image_ids = _dedupe_nonempty(image_ids)
    if not deduped_image_ids:
        raise _http("missing_images", "image_ids cannot be empty", 422)

    images = list(
        (
            await db.execute(
                select(Image).where(
                    Image.user_id == user_id,
                    Image.id.in_(deduped_image_ids),
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    image_by_id = {image.id: image for image in images}
    missing = [
        image_id for image_id in deduped_image_ids if image_id not in image_by_id
    ]
    if missing:
        raise _http(
            "image_not_found",
            "one or more images are not available for this workflow",
            404,
        )

    step = await _step(db, run.id, clean_step_key)
    now = added_at or _now()
    records = _workflow_asset_records(
        run=run,
        image_ids=deduped_image_ids,
        asset_type=clean_asset_type,
        source_step_key=clean_step_key,
        label=label,
        added_at=now,
    )
    run.metadata_jsonb = _merge_workflow_asset_metadata(run.metadata_jsonb, records)
    step.image_ids = _dedupe_nonempty([*(step.image_ids or []), *deduped_image_ids])
    existing_step_asset_ids = (step.output_json or {}).get("asset_image_ids")
    if not isinstance(existing_step_asset_ids, list):
        existing_step_asset_ids = []
    step_asset_ids = _dedupe_nonempty([*existing_step_asset_ids, *deduped_image_ids])
    step.output_json = {
        **(step.output_json or {}),
        "asset_image_ids": step_asset_ids,
        "asset_count": len(step_asset_ids),
        "asset_updated_at": now.isoformat(),
    }
    for record in records:
        image = image_by_id[record["image_id"]]
        image.metadata_jsonb = _merge_image_workflow_asset_metadata(
            image.metadata_jsonb,
            record,
        )
    return records


async def _image_out_map(db: AsyncSession, images: list[Image]) -> dict[str, ImageOut]:
    if not images:
        return {}
    variant_rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_([image.id for image in images])
            )
        )
    ).all()
    variant_map: dict[str, set[str]] = {}
    for image_id, kind in variant_rows:
        variant_map.setdefault(image_id, set()).add(kind)
    return {
        image.id: _image_to_out(image, variant_map.get(image.id)) for image in images
    }


def _image_to_out(img: Image, variant_kinds: set[str] | None = None) -> ImageOut:
    variant_kinds = variant_kinds or set()
    metadata = img.metadata_jsonb if isinstance(img.metadata_jsonb, dict) else {}
    billing_label = (
        metadata.get("billing_label")
        if isinstance(metadata.get("billing_label"), str)
        else None
    )
    billing_exempt_reason = (
        metadata.get("billing_exempt_reason")
        if isinstance(metadata.get("billing_exempt_reason"), str)
        else None
    )
    is_dual_race_bonus = metadata.get("is_dual_race_bonus") is True
    billing_free = (
        metadata.get("billing_free") is True
        or is_dual_race_bonus
        or billing_label == "free"
    )
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=f"/api/images/{img.id}/binary",
        display_url=f"/api/images/{img.id}/variants/display2048",
        preview_url=(
            f"/api/images/{img.id}/variants/preview1024"
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            f"/api/images/{img.id}/variants/thumb256"
            if "thumb256" in variant_kinds
            else None
        ),
        metadata_jsonb=metadata,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> WorkflowRunOut:
    """Build a response projection without reconciling or writing workflow state."""
    steps = await _load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        )
        .scalars()
        .all()
    )
    reports = await _load_quality_reports(db, run.id)

    # 先拉海报相关行；poster_masters/renders 的 image_id 和 task_ids 要
    # 加入下面 owned_images / generations 的扫描集合。
    poster_masters_rows: list[PosterMaster] = []
    poster_renders_rows: list[PosterRender] = []
    if run.type == POSTER_WORKFLOW_TYPE:
        poster_masters_rows = list(
            (
                await db.execute(
                    select(PosterMaster)
                    .where(PosterMaster.workflow_run_id == run.id)
                    .order_by(PosterMaster.candidate_index.asc())
                )
            )
            .scalars()
            .all()
        )
        poster_renders_rows = list(
            (
                await db.execute(
                    select(PosterRender)
                    .where(PosterRender.workflow_run_id == run.id)
                    .order_by(PosterRender.created_at.asc(), PosterRender.id.asc())
                )
            )
            .scalars()
            .all()
        )

    all_task_ids: set[str] = set()
    image_ids: set[str] = set(run.product_image_ids or [])
    for step in steps:
        all_task_ids.update(step.task_ids or [])
        image_ids.update(step.image_ids or [])
    for candidate in candidates:
        all_task_ids.update(candidate.task_ids or [])
        image_ids.update(_candidate_reference_image_ids(candidate))
    for report in reports:
        image_ids.add(report.image_id)
    for master in poster_masters_rows:
        all_task_ids.update(master.task_ids or [])
        if master.image_id:
            image_ids.add(master.image_id)
    for render in poster_renders_rows:
        all_task_ids.update(render.task_ids or [])
        if render.image_id:
            image_ids.add(render.image_id)

    generations: list[Generation] = []
    if all_task_ids:
        generations = await _workflow_generation_rows_from_task_ids(
            db,
            user_id=run.user_id,
            task_ids=list(all_task_ids),
            include_dual_bonus=True,
        )
    if all_task_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        or_(
                            Image.id.in_(image_ids)
                            if image_ids
                            else Image.id == "__none__",
                            Image.owner_generation_id.in_(all_task_ids),
                        ),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            )
            .scalars()
            .all()
        )
    elif image_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.id.in_(image_ids),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            )
            .scalars()
            .all()
        )
    else:
        owned_images = []

    image_map = await _image_out_map(db, owned_images)
    product_image_ids = set(run.product_image_ids or [])
    product_images = [
        image_map[iid] for iid in (run.product_image_ids or []) if iid in image_map
    ]
    generated_images = [
        image_map[image.id]
        for image in owned_images
        # 项目内的“非商品图”要都能被前端按 id 找到：
        # 包括候选图、展示图，以及从模特库选入并 materialize 到当前用户空间的参考图。
        if image.id not in product_image_ids and image.id in image_map
    ]

    poster_masters_out = [
        PosterMasterOut.model_validate(m) for m in poster_masters_rows
    ]
    poster_renders_out = [
        PosterRenderOut.model_validate(r) for r in poster_renders_rows
    ]

    return WorkflowRunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        type=run.type,
        status=run.status,
        title=run.title,
        user_prompt=run.user_prompt,
        product_image_ids=run.product_image_ids or [],
        current_step=run.current_step,
        quality_mode=run.quality_mode,
        metadata_jsonb=run.metadata_jsonb or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
        steps=[WorkflowStepOut.model_validate(step) for step in steps],
        model_candidates=[ModelCandidateOut.model_validate(c) for c in candidates],
        quality_reports=[QualityReportOut.model_validate(r) for r in reports],
        poster_masters=poster_masters_out,
        poster_renders=poster_renders_out,
        product_images=product_images,
        generated_images=generated_images,
        generations=[GenerationOut.model_validate(g) for g in generations],
    )


def _list_item_from_run(
    run: WorkflowRun, output_count: int = 0
) -> WorkflowRunListItemOut:
    return WorkflowRunListItemOut(
        id=run.id,
        conversation_id=run.conversation_id,
        type=run.type,
        status=run.status,
        title=run.title,
        user_prompt=run.user_prompt,
        product_image_ids=run.product_image_ids or [],
        current_step=run.current_step,
        quality_mode=run.quality_mode,
        metadata_jsonb=run.metadata_jsonb or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
        output_count=output_count,
        next_action=_next_action_for(run),
    )


router.include_router(_apparel_routes.entry_router)


@router.get("", response_model=WorkflowRunListOut)
async def list_workflows(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: str | None = Query(default=None),  # noqa: A002 - API field name
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> WorkflowRunListOut:
    stmt = select(WorkflowRun).where(
        WorkflowRun.user_id == user.id,
        WorkflowRun.deleted_at.is_(None),
    )
    if type:
        stmt = stmt.where(WorkflowRun.type == type)
    else:
        # ProjectsIndex 默认隐藏独立库生成 workflow——它们是后台任务实体，
        # 不是用户感知的"项目"。调用方明确传 type 才会返回。
        stmt = stmt.where(WorkflowRun.type.notin_(HIDDEN_PROJECT_WORKFLOW_TYPES))
    decoded_cursor = _decode_workflow_cursor(cursor, workflow_type=type)
    if decoded_cursor is not None:
        updated_at, row_id = decoded_cursor
        stmt = stmt.where(
            or_(
                WorkflowRun.updated_at < updated_at,
                and_(
                    WorkflowRun.updated_at == updated_at,
                    WorkflowRun.id < row_id,
                ),
            )
        )
    runs = list(
        (
            await db.execute(
                stmt.order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id)).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    page = runs[:limit]
    output_counts: dict[str, int] = {}
    if page:
        rows = (
            await db.execute(
                select(WorkflowStep.workflow_run_id, WorkflowStep.image_ids).where(
                    WorkflowStep.workflow_run_id.in_([run.id for run in page]),
                    WorkflowStep.step_key.in_(
                        ["showcase_generation", "multi_size_generation"]
                    ),
                )
            )
        ).all()
        for run_id, image_ids in rows:
            output_counts[run_id] = output_counts.get(run_id, 0) + len(image_ids or [])
    return WorkflowRunListOut(
        items=[_list_item_from_run(run, output_counts.get(run.id, 0)) for run in page],
        next_cursor=(
            _encode_workflow_cursor(page[-1], workflow_type=type)
            if len(runs) > limit and page
            else None
        ),
    )


@router.get("/{workflow_run_id}", response_model=WorkflowRunOut)
async def get_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    return await _build_run_out(db, run)


@router.post(
    "/{workflow_run_id}/reconcile",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def reconcile_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type == POSTER_WORKFLOW_TYPE:
        await _sync_poster_workflow_outputs(db, run)
    else:
        await _sync_workflow_outputs(db, run)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{workflow_run_id}",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_workflow(
    workflow_run_id: str,
    body: WorkflowRunPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise _http("invalid_title", "title cannot be empty", 422)
        run.title = title
        if run.conversation_id:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.id == run.conversation_id,
                        Conversation.user_id == user.id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conv is not None:
                conv.title = title
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.delete(
    "/{workflow_run_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    deleted_at = _now()
    cleanup = await _soft_delete_workflow_generated_images(
        db,
        run=run,
        deleted_at=deleted_at,
        cancel_message="workflow deleted",
        account_mode=getattr(user, "account_mode", "wallet"),
    )
    run.deleted_at = deleted_at
    if run.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == run.conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conv is not None:
            conv.deleted_at = deleted_at
    await db.commit()
    await _post_commit_workflow_generated_cleanup(user_id=user.id, cleanup=cleanup)
    return {"ok": True}


@router.post(
    "/{workflow_run_id}/assets",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def add_workflow_assets(
    workflow_run_id: str,
    body: WorkflowAssetsAddIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type == POSTER_WORKFLOW_TYPE:
        await _sync_poster_workflow_outputs(db, run)
    else:
        await _sync_workflow_outputs(db, run)
    source_step_key = (body.source_step_key or run.current_step or "").strip()
    await _attach_workflow_assets(
        db,
        run=run,
        user_id=user.id,
        image_ids=body.image_ids,
        asset_type=body.asset_type,
        source_step_key=source_step_key,
        label=body.label,
    )
    out = await _build_run_out(db, run)
    await db.commit()
    return out


router.include_router(_apparel_routes.project_router)


@router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/save-to-library",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_model_candidate_to_library(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateSaveToLibraryIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    image_id = _primary_candidate_image_id(candidate)
    if not image_id:
        raise _http("candidate_image_missing", "candidate has no image to save", 422)
    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source="favorite",
        image_id=image_id,
        title=body.title,
        age_segment=body.age_segment or _infer_age_segment_from_workflow(run),
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    brief = dict(candidate.model_brief_json or {})
    raw_saved_ids = brief.get("saved_library_item_ids")
    existing_saved_ids = (
        [value for value in raw_saved_ids if isinstance(value, str)]
        if isinstance(raw_saved_ids, list)
        else []
    )
    saved_ids = _dedupe_nonempty(
        [
            *existing_saved_ids,
            str(item.get("id") or ""),
        ]
    )
    brief["saved_library_item_ids"] = saved_ids
    candidate.model_brief_json = brief
    await db.commit()
    # 项目流程里收藏到模特库：用户已经在标注里填了字段，但仍后台触发一次 vision
    # 校正/补全（appearance_direction / style_tags 默认空时常见）。
    item_id = str(item.get("id") or "")
    if item_id:
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


@router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_model_candidate(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "ready" or not candidate.contact_sheet_image_id:
        raise _http(
            "candidate_not_ready", "model candidate is not ready to approve", 409
        )
    selected_accessory_image_id = body.selected_accessory_image_id
    approval = await _step(db, run.id, "model_approval")
    if selected_accessory_image_id:
        await _validate_accessory_preview_image(
            db,
            user_id=user.id,
            run_id=run.id,
            approval_step=approval,
            image_id=selected_accessory_image_id,
        )
    all_candidates = (
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    for row in all_candidates:
        if row.id == candidate.id:
            row.status = "selected"
            row.selected_at = now
            brief = dict(row.model_brief_json or {})
            brief["adjustments"] = body.adjustments
            brief["accessory_plan"] = body.accessory_plan.model_dump()
            brief["selected_accessory_image_id"] = selected_accessory_image_id
            row.model_brief_json = brief
        elif row.status != "failed":
            row.status = "rejected"
    approval.status = "approved"
    approval.approved_at = now
    approval.approved_by = user.id
    approval.input_json = {
        "candidate_id": candidate.id,
        "adjustments": body.adjustments,
        "accessory_plan": body.accessory_plan.model_dump(),
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    approval.output_json = {
        "selected_candidate_id": candidate.id,
        "contact_sheet_image_id": candidate.contact_sheet_image_id,
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    showcase = await _step(db, run.id, "showcase_generation")
    if showcase.status == "waiting_input":
        showcase.status = "needs_review"
    run.current_step = "model_approval"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/reopen",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def reopen_model_selection(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidates = (
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if candidate.status in {"selected", "rejected"}:
            candidate.status = (
                "ready" if candidate.contact_sheet_image_id else "generating"
            )
            candidate.selected_at = None
    approval = await _step(db, run.id, "model_approval")
    previous_approval_input = dict(approval.input_json or {})
    candidate_step = await _step(db, run.id, "model_candidates")
    model_settings = await _step(db, run.id, "model_settings")
    product_step = await _step(db, run.id, "product_analysis")
    preserved_accessory_plan = (
        _coerce_accessory_plan_payload(previous_approval_input.get("accessory_plan"))
        or _coerce_accessory_plan_payload(
            (candidate_step.input_json or {}).get("accessory_plan")
        )
        or _coerce_accessory_plan_payload(
            (model_settings.output_json or {}).get("accessory_plan")
        )
        or _accessory_plan_from_product_analysis(product_step.output_json or {})
    )
    preserved_style_prompt = (
        str(previous_approval_input.get("style_prompt") or "").strip()
        or str((candidate_step.input_json or {}).get("style_prompt") or "").strip()
        or str((model_settings.output_json or {}).get("style_prompt") or "").strip()
    )
    if candidate_step.status != "running":
        candidate_step.status = "needs_review"
    approval.status = "needs_review"
    approval.approved_at = None
    approval.approved_by = None
    approval.input_json = {
        **(
            {"accessory_plan": preserved_accessory_plan}
            if preserved_accessory_plan
            else {}
        ),
        **({"style_prompt": preserved_style_prompt} if preserved_style_prompt else {}),
    }
    approval.output_json = {}
    approval.task_ids = []
    approval.image_ids = []
    showcase = await _step(db, run.id, "showcase_generation")
    showcase.status = "waiting_input"
    showcase.input_json = {}
    showcase.output_json = {}
    showcase.task_ids = []
    showcase.image_ids = []
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    await db.execute(
        delete(QualityReport).where(QualityReport.workflow_run_id == run.id)
    )
    delivery = await _step(db, run.id, "delivery")
    delivery.status = "waiting_input"
    delivery.input_json = {}
    delivery.output_json = {}
    delivery.task_ids = []
    delivery.image_ids = []
    run.current_step = "model_candidates"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/accessory-previews",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_accessory_previews(
    workflow_run_id: str,
    body: AccessoryPreviewCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == body.candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "selected" or not candidate.contact_sheet_image_id:
        raise _http(
            "model_not_selected",
            "select and approve a model candidate before generating accessory previews",
            409,
        )
    approval = await _step(db, run.id, "model_approval")
    accessory_plan_payload = body.accessory_plan.model_dump()
    preview_request_key = _accessory_preview_request_key(
        candidate_id=candidate.id,
        accessory_plan=accessory_plan_payload,
        style_prompt=body.style_prompt,
    )
    existing_task_ids = _dedupe_nonempty(approval.task_ids or [])
    existing_input = approval.input_json or {}
    if approval.status == "running" and existing_task_ids:
        if existing_input.get("accessory_preview_request_key") == preview_request_key:
            run.current_step = "model_approval"
            run.status = "running"
            out = await _build_run_out(db, run)
            await db.commit()
            return out
        raise _http(
            "already_running",
            "accessory preview generation already running",
            409,
        )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    brief = candidate.model_brief_json or {}
    age_context = " ".join(
        str(part)
        for part in (
            run.user_prompt,
            brief.get("summary") if isinstance(brief, dict) else None,
            body.style_prompt,
        )
        if part
    )
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_accessory_preview_prompt(
            accessory_plan=accessory_plan_payload,
            style_prompt=body.style_prompt,
            age_context=age_context,
        ),
        attachment_ids=[candidate.contact_sheet_image_id],
        idempotency_key=f"wf:{run.id[:12]}:acc:{candidate.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="model_approval",
        image_params=_accessory_preview_image_params(),
        workflow_meta={
            "workflow_action": "accessory_preview",
            "workflow_candidate_id": candidate.id,
        },
    )
    approval.status = "running"
    approval.task_ids = _dedupe_nonempty([*existing_task_ids, *gen_ids])
    approval.input_json = {
        **(approval.input_json or {}),
        "candidate_id": candidate.id,
        "accessory_plan": accessory_plan_payload,
        "style_prompt": body.style_prompt,
        "accessory_preview_request_key": preview_request_key,
        "accessory_preview_started_at": _iso_now(),
    }
    run.current_step = "model_approval"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/accessory-selection",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_accessory_selection(
    workflow_run_id: str,
    body: AccessorySelectionIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    approval = await _step(db, run.id, "model_approval")
    selected_image_id = body.selected_accessory_image_id
    if selected_image_id:
        await _validate_accessory_preview_image(
            db,
            user_id=user.id,
            run_id=run.id,
            approval_step=approval,
            image_id=selected_image_id,
        )
    approval.input_json = {
        **(approval.input_json or {}),
        "selected_accessory_image_id": selected_image_id,
    }
    approval.output_json = {
        **(approval.output_json or {}),
        "selected_accessory_image_id": selected_image_id,
    }
    run.current_step = "model_approval"
    if run.status not in {"running", "failed"}:
        run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _selected_candidate(db: AsyncSession, run_id: str) -> ModelCandidate:
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.workflow_run_id == run_id,
                ModelCandidate.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("model_not_approved", "approve a model candidate first", 409)
    return candidate


async def _dispatch_showcase_images_generation(
    *,
    db: AsyncSession,
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: User,
) -> WorkflowRun:
    context = await _showcase_generation_context(
        db=db,
        user=user,
        workflow_run_id=workflow_run_id,
        body=body,
    )
    run: WorkflowRun = context["run"]
    showcase: WorkflowStep = context["showcase"]
    if showcase.status == "running" and _dedupe_nonempty(showcase.task_ids or []):
        await db.commit()
        return run

    request_id = new_uuid7()
    preflight_started_at = _iso_now()
    preflight = await _prepare_durable_showcase_preflight(
        db=db,
        context=context,
        body=body,
    )
    product_step: WorkflowStep = context["product_step"]
    candidate: ModelCandidate = context["candidate"]
    conv: Conversation = context["conv"]
    scene_cards = list(preflight.get("scene_cards") or [])
    final_prompts = list(preflight.get("final_prompts") or [])
    existing_image_ids = _dedupe_nonempty(showcase.image_ids or [])
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    for idx, (shot_type, variant) in enumerate(context["shot_picks"], start=1):
        scene_card = scene_cards[idx - 1] if idx - 1 < len(scene_cards) else {}
        final_prompt = (
            str(final_prompts[idx - 1])
            if idx - 1 < len(final_prompts) and final_prompts[idx - 1]
            else _showcase_prompt(
                product_analysis=product_step.output_json or {},
                selected_candidate=candidate,
                accessory_plan=context["accessory_plan"],
                template=body.template,
                shot_type=shot_type,
                shot_variant=variant,
                age_segment=context["age_segment"],
                final_quality=body.final_quality,
                user_prompt=run.user_prompt,
                aspect_ratio=body.aspect_ratio,
                scene_environment=body.scene_environment,
                scene_card=scene_card,
                garment_lock=preflight.get("garment_lock"),
                allow_pet=body.allow_pet,
                allow_background_people=body.allow_background_people,
            )
        )
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.IMAGE_TO_IMAGE,
            text=final_prompt,
            attachment_ids=context["ref_ids"],
            idempotency_key=f"wf:{run.id[:12]}:show:{request_id[:12]}:{idx}",
            workflow_run_id=run.id,
            workflow_step_key="showcase_generation",
            image_params=_image_params(
                aspect_ratio=body.aspect_ratio,
                count=1,
                render_quality="high" if body.final_quality != "standard" else "medium",
                final_quality=body.final_quality,
                fast=False,
            ),
            workflow_meta={
                "workflow_action": "showcase_image",
                "workflow_candidate_id": candidate.id,
                "workflow_shot_type": shot_type,
                "workflow_shot_variant": variant["label"],
                "workflow_shot_framing": variant["framing"],
                "workflow_template": body.template,
                "workflow_age_segment": context["age_segment"],
                "workflow_final_quality": body.final_quality,
                "workflow_scene_environment": body.scene_environment,
                "workflow_scene_strategy": body.scene_strategy,
                "workflow_scene_variety": body.scene_variety,
                "workflow_scene_planner": body.scene_planner,
                "workflow_scene_planner_effective": (
                    preflight.get("planning") or {}
                ).get("planner"),
                "workflow_scene_card_id": scene_card.get("id"),
                "workflow_scene_family": scene_card.get("scene_family"),
                "workflow_camera_angle": (scene_card.get("camera") or {}).get("angle")
                if isinstance(scene_card.get("camera"), dict)
                else None,
                "workflow_micro_event": scene_card.get("micro_event"),
                "workflow_scene_fingerprint": scene_card.get("fingerprint")
                or _scene_fingerprint(scene_card),
            },
        )
        task_ids.extend(gen_ids)
        bundles.append(bundle)

    if not task_ids:
        raise _http(
            "showcase_dispatch_failed",
            "showcase generation produced no durable tasks",
            500,
        )

    showcase.status = "running"
    showcase.task_ids = _dedupe_nonempty(task_ids)
    showcase.image_ids = existing_image_ids
    showcase.input_json = {
        **_showcase_request_input_json(
            body=body,
            request_id=request_id,
            shot_picks=context["shot_picks"],
            age_segment=context["age_segment"],
            ref_ids=context["ref_ids"],
            existing_image_ids=existing_image_ids,
            preflight_status="dispatched",
            active_task_ids=task_ids,
            preflight=preflight,
        ),
        "dispatch_mode": "transactional_outbox",
        "preflight_started_at": preflight_started_at,
        "preflight_completed_at": _iso_now(),
        "preflight_phase": "dispatching",
        "preflight_phase_detail": "生成任务已持久化，等待 worker 执行",
        "preflight_phase_current": len(task_ids),
        "preflight_phase_total": len(task_ids),
    }
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    try:
        await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "showcase fast-path publish failed; outbox will retry "
            "user=%s run=%s request=%s err=%s",
            user.id,
            workflow_run_id,
            request_id,
            exc,
            exc_info=True,
        )
    return run


@router.post(
    "/{workflow_run_id}/showcase-images",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_showcase_images(
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _dispatch_showcase_images_generation(
        db=db,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
    )
    return await _build_run_out(db, run)


@router.post(
    "/{workflow_run_id}/images/{image_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def revise_showcase_image(
    workflow_run_id: str,
    image_id: str,
    body: ImageRevisionIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if image_id not in set(showcase.image_ids or []):
        raise _http(
            "invalid_image", "image is not a showcase output for this workflow", 404
        )
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise _http("not_found", "image not found", 404)
    product_step = await _step(db, run.id, "product_analysis")
    candidate = await _selected_candidate(db, run.id)
    refs = _dedupe_nonempty(
        [*run.product_image_ids, candidate.contact_sheet_image_id or "", image_id]
    )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    revision_index = len(showcase.task_ids or []) + 1
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_revision_prompt(
            instruction=body.instruction,
            product_analysis=product_step.output_json or {},
            selected_candidate=candidate,
        ),
        attachment_ids=refs,
        idempotency_key=f"wf:{run.id[:22]}:rev:{revision_index}",
        workflow_run_id=run.id,
        workflow_step_key="showcase_generation",
        image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
        workflow_meta={
            "workflow_action": "revision",
            "workflow_revision_source_image_id": image_id,
            "workflow_revision_scope": body.scope,
        },
    )
    showcase.task_ids = [*(showcase.task_ids or []), *gen_ids]
    showcase.status = "running"
    showcase.input_json = {
        **(showcase.input_json or {}),
        "active_task_ids": gen_ids,
        "active_output_count": len(gen_ids) or 1,
        "active_task_kind": "revision",
        "baseline_image_count": len(_dedupe_nonempty(showcase.image_ids or [])),
        "preflight_status": "dispatched",
    }
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {
        **(quality.input_json or {}),
        "latest_revision": {
            "source_image_id": image_id,
            "instruction": body.instruction,
            "scope": body.scope,
        },
    }
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/delivery/complete",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def complete_delivery(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type == POSTER_WORKFLOW_TYPE:
        await _sync_poster_workflow_outputs(db, run)
        multi_step = await _step(db, run.id, "multi_size_generation")
        image_ids = _dedupe_nonempty(multi_step.image_ids or [])
        if not image_ids:
            raise _http("no_outputs", "generate poster renders before delivery", 409)
        delivery = await _step(db, run.id, "delivery")
        now = _now()
        multi_step.status = "completed"
        multi_step.approved_at = now
        multi_step.approved_by = user.id
        delivery.status = "completed"
        delivery.approved_at = now
        delivery.approved_by = user.id
        delivery.input_json = {
            **(delivery.input_json or {}),
            "final_image_ids": image_ids,
        }
        delivery.output_json = {
            **(delivery.output_json or {}),
            "download_image_ids": image_ids,
            "completed_at": now.isoformat(),
        }
        await _attach_workflow_assets(
            db,
            run=run,
            user_id=user.id,
            image_ids=image_ids,
            asset_type="poster_delivery",
            source_step_key="delivery",
            label="海报交付",
            added_at=now,
        )
        run.status = "completed"
        run.current_step = "delivery"
        out = await _build_run_out(db, run)
        await db.commit()
        return out

    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if not showcase.image_ids:
        raise _http("no_outputs", "generate showcase images before delivery", 409)
    quality = await _step(db, run.id, "quality_review")
    delivery = await _step(db, run.id, "delivery")
    now = _now()
    quality.status = "approved"
    quality.approved_at = now
    quality.approved_by = user.id
    delivery.status = "completed"
    delivery.approved_at = now
    delivery.approved_by = user.id
    delivery.input_json = {"final_image_ids": showcase.image_ids}
    delivery.output_json = {
        "download_image_ids": showcase.image_ids,
        "completed_at": now.isoformat(),
    }
    run.status = "completed"
    run.current_step = "delivery"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


router.include_router(_model_library_routes.router)
router.include_router(_poster_routes.router)
