# ruff: noqa: F405

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
from functools import partial
from typing import Annotated

import httpx  # noqa: F401 - library sync facade dependency
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Query,
)
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.providers import (  # noqa: F401 - library sync facade dependencies
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec  # noqa: F401

from lumen_core.constants import (
    GenerationStatus,  # noqa: F401 - historical workflow facade export
    Intent,
    MAX_PROMPT_CHARS,  # noqa: F401 - showcase facade dependency
)
from lumen_core.models import (
    Conversation,
    Image,
    ModelCandidate,
    new_uuid7,
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

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..billing_cache_state import invalidate_balance_cache  # noqa: F401
from ..config import settings  # noqa: F401 - library facade dependency
from ..redis_client import get_redis  # noqa: F401
from ..runtime_settings import get_setting  # noqa: F401 - library facade dependency
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
from ..workflow_services import workflow_runtime as _workflow_runtime_service
from ..workflow_services.workflow_runtime import *  # noqa: F403,F401
from ..workflow_services import library_sync as _library_sync_service
from ..workflow_services import showcase_preflight as _showcase_preflight_service
from ..workflow_services.facade import bind_facade
from ..workflow_domain.workflow_policy_exports import *  # noqa: F403,F401
from .workflow_routes import apparel as _apparel_routes
from .workflow_routes import model_library as _model_library_routes
from .workflow_routes import poster as _poster_routes
from .workflow_routes._facade import _PublishBundle
from .messages import (  # noqa: F401
    _create_assistant_task,
    _publish_assistant_task,
    _publish_message_appended,
)


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
_workflow_runtime_service.export_to_facade(sys.modules[__name__])

HIDDEN_PROJECT_WORKFLOW_TYPES = frozenset(
    {
        WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        "poster_style_library_generate",
    }
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
