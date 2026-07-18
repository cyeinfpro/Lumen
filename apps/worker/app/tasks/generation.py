"""Generation worker facade and arq entrypoint.

Stateful helpers live in ``generation_parts`` while private imports remain stable.
"""

from __future__ import annotations

import asyncio
import binascii
import io
import logging
import os
import random as random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from typing import Any, cast

import httpx as httpx
from PIL import Image as PILImage
from sqlalchemy import select, update

from .. import image_artifacts as _image_artifacts
from ..background_removal import (
    TransparentPipelineFailure,
    process_transparent_request,
)
from .. import billing as worker_billing
from .. import runtime_settings as runtime_settings
from ..image_artifacts import (
    _ALLOWED_UPSTREAM_IMAGE_FORMATS as _ALLOWED_UPSTREAM_IMAGE_FORMATS,
    _GeneratedImageInspection as _GeneratedImageInspection,
    _ImageVariantBundle as _ImageVariantBundle,
    _MAX_UPSTREAM_IMAGE_SIDE as _MAX_UPSTREAM_IMAGE_SIDE,
    _PostprocessedGeneratedImage as _PostprocessedGeneratedImage,
    _VariantPayload as _VariantPayload,
    _compute_blurhash as _compute_blurhash,
    _decode_upstream_image_b64 as _decode_upstream_image_b64,
    _image_has_alpha as _image_has_alpha,
    _image_has_transparency as _image_has_transparency,
    _inspect_generated_image_sync as _inspect_generated_image_sync,
    _make_display as _make_display,
    _make_preview as _make_preview,
    _make_thumb as _make_thumb,
    _make_variants_with_pil_sync as _make_variants_with_pil_sync,
    _make_variants_with_vips_sync as _make_variants_with_vips_sync,
    _resize_vips_image as _resize_vips_image,
    _rgb_image_for_flat_variant as _rgb_image_for_flat_variant,
    _sha256 as _sha256,
    _validate_generated_image_metadata as _validate_generated_image_metadata,
    _webp_image_for_variant as _webp_image_for_variant,
)

from lumen_core.constants import (
    DEFAULT_IMAGE_RESPONSES_MODEL as DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST as DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_ASPECT,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
    EV_GEN_ATTACHED as EV_GEN_ATTACHED,
    EV_GEN_FAILED,
    EV_GEN_PARTIAL_IMAGE,
    EV_GEN_PROGRESS,
    EV_GEN_QUEUED,
    EV_GEN_RETRYING,
    EV_GEN_STARTED,
    EV_GEN_SUCCEEDED as EV_GEN_SUCCEEDED,
    GenerationAction,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    ImageSource,
    MessageStatus,
    RETRY_BACKOFF_SECONDS as RETRY_BACKOFF_SECONDS,
    task_channel,
)
from lumen_core.image_reference import normalized_ref_from_metadata
from lumen_core.models import (
    Conversation as Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    PosterMaster,
    PosterRender,
    PosterStyleItem,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.model_image_metadata import (
    build_model_image_metadata as build_model_image_metadata,
    model_image_filename as model_image_filename,
    save_image_with_model_metadata as save_image_with_model_metadata,
)
from lumen_core.queue_metadata import generation_queue_metadata, merge_queue_metadata
from lumen_core.sizing import (
    AspectRatio,
    SizeMode,
    resolve_size,
    validate_explicit_size,
)
from lumen_core.providers import parse_provider_bool as parse_provider_bool

from ..config import settings
from ..db import SessionLocal
from ..byok_runtime import (
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
)
from ..observability import (
    get_tracer,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
)
from ..retry import (
    RetryDecision,
    is_moderation_block,
    is_retriable as is_retriable,
)
from ..storage import StorageDiskFullError as StorageDiskFullError
from ..storage import storage
from ..upstream import (
    UpstreamCancelled,
    UpstreamError,
    _image_endpoint_kind_for_engine,
    _resolve_image_primary_route,
    edit_image,
    generate_image,
    pop_image_quota_context,
    pop_image_trace_id,
    pop_image_retry_attempt,
    push_image_quota_context,
    push_image_trace_id,
    push_image_retry_attempt,
)
from .generation_parts import diagnostics as _generation_diagnostics
from .generation_parts import event_delivery as _generation_event_delivery
from .generation_parts import lease as _generation_lease
from .generation_parts import lifecycle as _generation_lifecycle
from .generation_parts import persistence as _generation_persistence
from .generation_parts import postprocess as _generation_postprocess
from .generation_parts import queue as _generation_queue
from .generation_parts import queue_claim as _generation_queue_claim
from .generation_parts import references as _generation_references
from .generation_parts import request_options as _generation_request_options
from .generation_parts import retry_state as _generation_retry_state
from .generation_parts import workflow_hooks as _generation_workflow_hooks
from .state import is_generation_terminal

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.generation")

_stage_generation_event = _generation_event_delivery.stage_generation_event
_stage_generation_success_event = (
    _generation_event_delivery.stage_generation_success_event
)
_stage_generation_failure_event = (
    _generation_event_delivery.stage_generation_failure_event
)
_deliver_generation_events = _generation_event_delivery.deliver_generation_events
_deliver_generation_event = _generation_event_delivery.deliver_generation_event
publish_event = _generation_event_delivery.publish_event


def _generation_facade_globals() -> dict[str, Any]:
    return globals()


_generation_lease.bind_generation_facade(_generation_facade_globals)
_generation_lifecycle.bind_generation_facade(_generation_facade_globals)
_generation_persistence.bind_generation_facade(_generation_facade_globals)
_generation_queue.bind_generation_facade(_generation_facade_globals)
_generation_queue_claim.bind_generation_facade(_generation_facade_globals)
_generation_request_options.bind_generation_facade(_generation_facade_globals)
_generation_retry_state.bind_generation_facade(_generation_facade_globals)


# --- Constants ---

_LEASE_TTL_S = _generation_lease.LEASE_TTL_S
_LEASE_RENEW_S = _generation_lease.LEASE_RENEW_S
_MAX_ATTEMPTS = _generation_retry_state.MAX_ATTEMPTS
_REFERENCE_LOAD_TIMEOUT_S = 30.0
# Keep the worker-level generation budget below arq's 1800s job_timeout so the
# task can release leases/semaphores and persist a retriable state itself.
_RUN_GENERATION_TIMEOUT_S = 1500.0
_IMAGE_QUEUE_LOCK_KEY = _generation_queue.IMAGE_QUEUE_LOCK_KEY
_IMAGE_QUEUE_ACTIVE_KEY = _generation_queue.IMAGE_QUEUE_ACTIVE_KEY
_IMAGE_QUEUE_PROVIDER_LOCK_PREFIX = _generation_queue.IMAGE_QUEUE_PROVIDER_LOCK_PREFIX
_IMAGE_QUEUE_TASK_PROVIDER_PREFIX = _generation_queue.IMAGE_QUEUE_TASK_PROVIDER_PREFIX
_IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX = _generation_queue.IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX
_IMAGE_QUEUE_NOT_BEFORE_PREFIX = _generation_queue.IMAGE_QUEUE_NOT_BEFORE_PREFIX
_IMAGE_QUEUE_AVOID_PREFIX = _generation_queue.IMAGE_QUEUE_AVOID_PREFIX
_IMAGE_QUEUE_LANE_CURSOR_KEY = _generation_queue.IMAGE_QUEUE_LANE_CURSOR_KEY
_IMAGE_INFLIGHT_PREFIX = _generation_queue.IMAGE_INFLIGHT_PREFIX
_IMAGE_QUEUE_LOCK_TTL_S = _generation_queue.IMAGE_QUEUE_LOCK_TTL_S
_IMAGE_QUEUE_LOCK_WAIT_S = _generation_queue.IMAGE_QUEUE_LOCK_WAIT_S
_IMAGE_QUEUE_SCAN_LIMIT = _generation_queue.IMAGE_QUEUE_SCAN_LIMIT
_IMAGE_QUEUE_FAIR_SCAN_LIMIT = _generation_queue.IMAGE_QUEUE_FAIR_SCAN_LIMIT
_IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S = _generation_queue.IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S
_IMAGE_QUEUE_NOT_BEFORE_GRACE_S = _generation_queue.IMAGE_QUEUE_NOT_BEFORE_GRACE_S
_IMAGE_PROVIDER_UNAVAILABLE_RETRY_S = (
    _generation_queue.IMAGE_PROVIDER_UNAVAILABLE_RETRY_S
)
_STALE_ATTEMPT_REQUEUE_DELAY_S = _generation_retry_state.STALE_ATTEMPT_REQUEUE_DELAY_S
_IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S = (
    _generation_queue.IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S
)
_PROVIDER_COOLDOWN_LOCAL = _generation_queue.PROVIDER_COOLDOWN_LOCAL
_RUNNING_GENERATION_STATUSES = (GenerationStatus.RUNNING.value,)
_IMAGE_QUEUE_AVOID_TTL_S = _generation_queue.IMAGE_QUEUE_AVOID_TTL_S
_IMAGE_QUEUE_DEFAULT_LANE = _generation_queue.IMAGE_QUEUE_DEFAULT_LANE
_IMAGE_QUEUE_LANE_WEIGHTS = _generation_queue.IMAGE_QUEUE_LANE_WEIGHTS
_IMAGE_QUEUE_LANE_ORDER = _generation_queue.IMAGE_QUEUE_LANE_ORDER
_IMAGE_QUEUE_LANE_RANK = _generation_queue.IMAGE_QUEUE_LANE_RANK
_MODERATION_RETRY_CAP = _generation_retry_state.MODERATION_RETRY_CAP
_RETRY_JITTER_RATIO = _generation_retry_state.RETRY_JITTER_RATIO
_RETRY_BACKOFF_MAX_SECONDS = _generation_retry_state.RETRY_BACKOFF_MAX_SECONDS
_LEASE_REACQUIRED_SUBSTAGE = "lease_reacquired"
_IMAGE_RENDER_QUALITY_VALUES = _generation_request_options.IMAGE_RENDER_QUALITY_VALUES
_IMAGE_OUTPUT_FORMAT_VALUES = _generation_request_options.IMAGE_OUTPUT_FORMAT_VALUES
_IMAGE_BACKGROUND_VALUES = _generation_request_options.IMAGE_BACKGROUND_VALUES
_IMAGE_MODERATION_VALUES = _generation_request_options.IMAGE_MODERATION_VALUES


class _TaskCancelled(UpstreamCancelled):
    """GEN-P1-4: 用户取消信号——复用 upstream.UpstreamCancelled（BaseException 子类），
    便于 race / fallback 各层正确透传；外层 generation 任务再捕获标终态。"""


class _LeaseLost(UpstreamCancelled):
    """Lease renewer gave up; this worker must stop before another attempt runs."""


class _StaleGenerationAttempt(Exception):
    """This worker's attempt epoch no longer owns the generation row."""


_QueuedGenerationCandidate = _generation_queue.QueuedGenerationCandidate

_RELEASE_LEASE_LUA = _generation_lease.RELEASE_LEASE_LUA
_RENEW_LEASE_LUA = _generation_lease.RENEW_LEASE_LUA
_ACQUIRE_LUA = _generation_lease.ACQUIRE_LUA
_RELEASE_LUA = _generation_lease.RELEASE_LUA
_IMAGE_SEMAPHORE_KEY_TTL_S = _generation_lease.IMAGE_SEMAPHORE_KEY_TTL_S
_RESERVE_IMAGE_SLOT_LUA = _generation_queue_claim.RESERVE_IMAGE_SLOT_LUA
_DUAL_RACE_SENTINEL_PREFIX = _generation_queue_claim.DUAL_RACE_SENTINEL_PREFIX
_IMAGE_GENERATION_CONCURRENCY_SETTING = (
    _generation_queue.IMAGE_GENERATION_CONCURRENCY_SETTING
)

_is_cancelled = _generation_lease.is_cancelled
_acquire_lease = _generation_lease.acquire_lease
_release_lease = _generation_lease.release_lease
_lease_renewer = _generation_lease.lease_renewer
_cancel_renewer_task = _generation_lease.cancel_renewer_task
_RedisSemaphore = _generation_lease.RedisSemaphore

_redis_text = _generation_queue.redis_text
_coerce_image_queue_capacity = _generation_queue.coerce_image_queue_capacity
_image_queue_capacity = _generation_queue.image_queue_capacity
_resolve_image_queue_capacity = _generation_queue.resolve_image_queue_capacity
_image_provider_lock_key = _generation_queue.image_provider_lock_key
_image_provider_active_key = _generation_queue.image_provider_active_key
_image_task_provider_key = _generation_queue.image_task_provider_key
_image_queue_enqueue_dedupe_key = _generation_queue.image_queue_enqueue_dedupe_key
_image_queue_not_before_key = _generation_queue.image_queue_not_before_key
_image_queue_avoid_key = _generation_queue.image_queue_avoid_key
_avoid_provider_for_task = _generation_queue.avoid_provider_for_task
_get_avoided_providers = _generation_queue.get_avoided_providers
_clear_avoided_providers = _generation_queue.clear_avoided_providers
_image_inflight_key = _generation_queue.image_inflight_key
_classify_inflight_lane = _generation_queue.classify_inflight_lane
_inflight_set_fields = _generation_queue.inflight_set_fields
_inflight_clear = _generation_queue.inflight_clear
_image_queue_lock = _generation_queue.image_queue_lock
_cleanup_image_queue_active = _generation_queue.cleanup_image_queue_active
_active_image_provider_names = _generation_queue.active_image_provider_names
_provider_active_count = _generation_queue.provider_active_count
_queued_generation_ids = _generation_queue.queued_generation_ids
_queue_lane_weight = _generation_queue.queue_lane_weight
_queue_lane_sort_key = _generation_queue.queue_lane_sort_key
_weighted_queue_lane_slots = _generation_queue.weighted_queue_lane_slots
_queued_candidate_from_mapping = _generation_queue.queued_candidate_from_mapping
_fallback_queued_candidate = _generation_queue.fallback_queued_candidate
_queued_generation_candidates = _generation_queue.queued_generation_candidates
_select_ready_generation_ids_by_lane = (
    _generation_queue.select_ready_generation_ids_by_lane
)
_advance_image_queue_lane_cursor = _generation_queue.advance_image_queue_lane_cursor
_ready_queued_generation_ids = _generation_queue.ready_queued_generation_ids
_enqueue_generation_once = _generation_queue.enqueue_generation_once
_clear_image_queue_enqueue_dedupe = _generation_queue.clear_image_queue_enqueue_dedupe
_kick_image_queue = _generation_queue.kick_image_queue

_dual_race_sentinel_name = _generation_queue_claim.dual_race_sentinel_name
_is_dual_race_sentinel = _generation_queue_claim.is_dual_race_sentinel
_reserve_image_queue_slot = _generation_queue_claim.reserve_image_queue_slot
_release_image_queue_slot = _generation_queue_claim.release_image_queue_slot
_release_generation_runtime_resources = (
    _generation_queue_claim.release_generation_runtime_resources
)

# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

_IMAGE_POSTPROCESS_MODES = _generation_postprocess._IMAGE_POSTPROCESS_MODES
_IMAGE_POSTPROCESS_EXECUTOR: ProcessPoolExecutor | None = None


def _make_image_variants_sync(raw_image: bytes) -> _ImageVariantBundle:
    return _image_artifacts._make_image_variants_sync(raw_image)


def _make_image_variants_pil_only_sync(raw_image: bytes) -> _ImageVariantBundle:
    return _image_artifacts._make_image_variants_pil_only_sync(raw_image)


def _resolve_image_postprocess_mode(mode: str | None = None) -> str:
    return _generation_postprocess._resolve_image_postprocess_mode(
        mode,
        environ=os.environ,
        allowed_modes=_IMAGE_POSTPROCESS_MODES,
        logger=logger,
    )


def _resolve_image_postprocess_workers() -> int:
    return _generation_postprocess._resolve_image_postprocess_workers(
        environ=os.environ,
        cpu_count=os.cpu_count,
    )


def _get_image_postprocess_executor() -> ProcessPoolExecutor:
    global _IMAGE_POSTPROCESS_EXECUTOR
    _IMAGE_POSTPROCESS_EXECUTOR = (
        _generation_postprocess._get_image_postprocess_executor(
            _IMAGE_POSTPROCESS_EXECUTOR,
            resolve_workers=_resolve_image_postprocess_workers,
            executor_type=ProcessPoolExecutor,
        )
    )
    return _IMAGE_POSTPROCESS_EXECUTOR


def _reset_image_postprocess_executor() -> None:
    global _IMAGE_POSTPROCESS_EXECUTOR
    executor = _IMAGE_POSTPROCESS_EXECUTOR
    _IMAGE_POSTPROCESS_EXECUTOR = None
    _generation_postprocess._reset_image_postprocess_executor(executor)


async def _postprocess_image_variants(
    raw_image: bytes,
    *,
    mode: str | None = None,
) -> tuple[_ImageVariantBundle, str]:
    return await _generation_postprocess._postprocess_image_variants(
        raw_image,
        mode=mode,
        hooks=_generation_postprocess.ImageVariantExecutionHooks(
            resolve_mode=_resolve_image_postprocess_mode,
            get_executor=_get_image_postprocess_executor,
            reset_executor=_reset_image_postprocess_executor,
            make_variants_sync=_make_image_variants_sync,
            make_variants_pil_only_sync=_make_image_variants_pil_only_sync,
            broken_process_pool_type=BrokenProcessPool,
        ),
        logger=logger,
    )


def _image_decode_upstream_error(exc: Exception) -> UpstreamError:
    return _generation_postprocess._image_decode_upstream_error(
        exc,
        upstream_error_type=UpstreamError,
        bad_response_error_code=EC.BAD_RESPONSE.value,
    )


async def _postprocess_raw_generated_image(
    raw_image: bytes,
    *,
    prompt: str,
    transparent_requested: bool,
    mode: str | None = None,
) -> _PostprocessedGeneratedImage:
    return await _generation_postprocess._postprocess_raw_generated_image(
        raw_image,
        prompt=prompt,
        transparent_requested=transparent_requested,
        mode=mode,
        hooks=_generation_postprocess.GeneratedImagePostprocessHooks(
            inspect_generated_image_sync=_inspect_generated_image_sync,
            sha256=_sha256,
            process_transparent_request=process_transparent_request,
            transparent_pipeline_failure_type=TransparentPipelineFailure,
            sanitize_transparent_qc_payload=_sanitize_transparent_qc_payload,
            postprocess_image_variants=_postprocess_image_variants,
            compute_blurhash=_compute_blurhash,
            image_decode_upstream_error=_image_decode_upstream_error,
            upstream_error_type=UpstreamError,
            bad_response_error_code=EC.BAD_RESPONSE.value,
            generated_image_inspection_type=_GeneratedImageInspection,
            postprocessed_generated_image_type=_PostprocessedGeneratedImage,
        ),
    )


_clean_model_style_tags = _generation_persistence.clean_model_style_tags
_model_image_metadata_from_request = (
    _generation_persistence.model_image_metadata_from_request
)
_compact_image_payload_meta = _generation_persistence.compact_image_payload_meta
_maybe_embed_model_image_metadata_bytes = (
    _generation_persistence.maybe_embed_model_image_metadata_bytes
)

# ---------------------------------------------------------------------------
# Upstream body assembly
# ---------------------------------------------------------------------------


_MASK_MAX_BYTES = 50 * 1024 * 1024


def _reference_hooks() -> _generation_references.ReferenceHooks:
    return _generation_references.ReferenceHooks(
        select=select,
        image_model=Image,
        normalized_ref_from_metadata=normalized_ref_from_metadata,
        storage_get_bytes=storage.aget_bytes,
        upstream_error_factory=UpstreamError,
        upstream_error_type=UpstreamError,
        reference_missing_code=EC.REFERENCE_MISSING.value,
        reference_timeout_code=EC.REFERENCE_TIMEOUT.value,
        reference_image_too_large_code=EC.REFERENCE_IMAGE_TOO_LARGE.value,
        bad_reference_image_code=EC.BAD_REFERENCE_IMAGE.value,
        logger=logger,
        reference_load_timeout_s=_REFERENCE_LOAD_TIMEOUT_S,
        mask_max_bytes=_MASK_MAX_BYTES,
    )


async def _load_reference_images(
    session: Any, image_ids: list[str]
) -> list[tuple[str, bytes]]:
    return await _generation_references.load_reference_images(
        session, image_ids, hooks=_reference_hooks()
    )


async def _load_mask_image(session: Any, mask_image_id: str) -> bytes:
    return await _generation_references.load_mask_image(
        session, mask_image_id, hooks=_reference_hooks()
    )


_mask_alpha_is_binary = _generation_references.mask_alpha_is_binary
_binarize_mask_alpha = _generation_references.binarize_mask_alpha


def _resize_mask_to_reference(
    mask_bytes: bytes,
    reference_bytes: bytes,
) -> bytes:
    return _generation_references.resize_mask_to_reference(
        mask_bytes,
        reference_bytes,
        upstream_error_factory=UpstreamError,
        upstream_error_type=UpstreamError,
        bad_reference_image_code=EC.BAD_REFERENCE_IMAGE.value,
        alpha_is_binary=_mask_alpha_is_binary,
        binarize_alpha=_binarize_mask_alpha,
    )


_reference_pixel_size = _generation_references.reference_pixel_size


def _inpaint_size_from_reference(ref_w: int, ref_h: int) -> str | None:
    return _generation_references.inpaint_size_from_reference(
        ref_w,
        ref_h,
        limits=_generation_references.InpaintSizingLimits(
            explicit_align=EXPLICIT_ALIGN,
            max_explicit_aspect=MAX_EXPLICIT_ASPECT,
            max_explicit_pixels=MAX_EXPLICIT_PIXELS,
            max_explicit_side=MAX_EXPLICIT_SIDE,
            min_explicit_pixels=MIN_EXPLICIT_PIXELS,
            validate_explicit_size=validate_explicit_size,
        ),
    )


_bounded_next_attempt = _generation_retry_state.bounded_next_attempt
_parse_size_string = _generation_request_options.parse_size_string
_validate_resolved_size = _generation_request_options.validate_resolved_size
_parse_aspect_ratio_value = _generation_request_options.parse_aspect_ratio_value
_aspect_ratio_prompt_constraint = (
    _generation_request_options.aspect_ratio_prompt_constraint
)
_prompt_with_aspect_ratio_constraint = (
    _generation_request_options.prompt_with_aspect_ratio_constraint
)
_base_retry_backoff_seconds = _generation_retry_state.base_retry_backoff_seconds
_retry_delay_seconds = _generation_retry_state.retry_delay_seconds
_retry_not_before_ttl = _generation_retry_state.retry_not_before_ttl
_generation_attempt_update = _generation_retry_state.generation_attempt_update


def _load_model_library_tagger() -> Callable[..., Awaitable[Any]]:
    from .model_library_tagging import auto_tag_model_image

    return auto_tag_model_image


def _load_poster_style_tagger() -> Callable[..., Awaitable[Any]]:
    from .poster_style_tagging import auto_tag_poster_style_image

    return auto_tag_poster_style_image


def _workflow_hook_dependencies() -> (
    _generation_workflow_hooks.WorkflowHookDependencies
):
    return _generation_workflow_hooks.WorkflowHookDependencies(
        select=select,
        workflow_run_model=WorkflowRun,
        workflow_step_model=WorkflowStep,
        poster_style_item_model=PosterStyleItem,
        poster_master_model=PosterMaster,
        poster_render_model=PosterRender,
        new_uuid7=new_uuid7,
        logger=logger,
        utcnow=lambda: datetime.now(timezone.utc),
        load_model_library_tagger=_load_model_library_tagger,
        load_poster_style_tagger=_load_poster_style_tagger,
        model_library_requested_count=_model_library_requested_count_from_step,
    )


_model_library_requested_count_from_step = (
    _generation_workflow_hooks.model_library_requested_count_from_step
)


async def _maybe_record_model_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    await _generation_workflow_hooks.maybe_record_model_library_generate_image(
        session=session,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        deps=_workflow_hook_dependencies(),
    )


async def _maybe_record_poster_style_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    await _generation_workflow_hooks.maybe_record_poster_style_library_generate_image(
        session=session,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        deps=_workflow_hook_dependencies(),
    )


async def _maybe_record_model_library_candidate_image(
    *,
    session: Any,
    user_id: str,
    parent_upstream_request: dict[str, Any],
    bonus_image_id: str,
) -> None:
    await _generation_workflow_hooks.maybe_record_model_library_candidate_image(
        session=session,
        user_id=user_id,
        parent_upstream_request=parent_upstream_request,
        bonus_image_id=bonus_image_id,
        deps=_workflow_hook_dependencies(),
    )


async def _maybe_record_poster_workflow_image(
    *,
    session: Any,
    user_id: str,
    generation: Generation,
    image_id: str,
) -> None:
    await _generation_workflow_hooks.maybe_record_poster_workflow_image(
        session=session,
        user_id=user_id,
        generation=generation,
        image_id=image_id,
        deps=_workflow_hook_dependencies(),
    )


_primary_input_image_id_valid = _generation_request_options.primary_input_image_id_valid
_ensure_generation_updated = _generation_retry_state.ensure_generation_updated
_request_option = _generation_request_options.request_option
_request_compression = _generation_request_options.request_compression
_request_render_quality = _generation_request_options.request_render_quality
_request_responses_model = _generation_request_options.request_responses_model
_image_request_options = _generation_request_options.image_request_options
_image_requested_count = _generation_request_options.image_requested_count

_DIAG_STRING_LIMIT = _generation_diagnostics.DIAG_STRING_LIMIT
_DIAG_COLLECTION_LIMIT = _generation_diagnostics.DIAG_COLLECTION_LIMIT
_PROVIDER_ATTEMPT_ERROR_LIMIT = _generation_diagnostics.PROVIDER_ATTEMPT_ERROR_LIMIT
_PRIVATE_DIAGNOSTIC_KEYS = _generation_diagnostics.PRIVATE_DIAGNOSTIC_KEYS
_PRIVATE_PROVIDER_ATTEMPT_KEYS = _generation_diagnostics.PRIVATE_PROVIDER_ATTEMPT_KEYS
_PRIVATE_PROVIDER_PROGRESS_KEYS = _generation_diagnostics.PRIVATE_PROVIDER_PROGRESS_KEYS
_PROVIDER_ATTEMPT_PROGRESS_KEYS = _generation_diagnostics.PROVIDER_ATTEMPT_PROGRESS_KEYS


class _StageTimer(_generation_diagnostics.StageTimer):
    def __init__(self) -> None:
        super().__init__(monotonic=time.monotonic)


_generation_trace_id = _generation_diagnostics.generation_trace_id


def _queue_wait_ms(
    created_at: datetime | None, *, now: datetime | None = None
) -> int | None:
    return _generation_diagnostics.queue_wait_ms(
        created_at,
        now=now,
        now_factory=lambda: datetime.now(timezone.utc),
    )


_is_byok_provider_name = _generation_diagnostics.is_byok_provider_name


def _provider_attempt_from_progress(
    event: dict[str, Any],
    *,
    status: str,
    attempt_epoch: int,
    provider_key: str = "provider",
    route_default: str | None = None,
) -> dict[str, Any]:
    return _generation_diagnostics.provider_attempt_from_progress(
        event,
        status=status,
        attempt_epoch=attempt_epoch,
        redis_text=_redis_text,
        is_byok_provider=_is_byok_provider_name,
        provider_key=provider_key,
        route_default=route_default,
    )


_compact_diag_value = _generation_diagnostics.compact_diag_value
_compact_provider_attempt = _generation_diagnostics.compact_provider_attempt
_compact_provider_attempts = _generation_diagnostics.compact_provider_attempts
_sanitize_generation_diagnostics_payload = (
    _generation_diagnostics.sanitize_generation_diagnostics_payload
)
_sanitize_generation_upstream_request = (
    _generation_diagnostics.sanitize_generation_upstream_request
)


def _request_event_provider_from_attempts(
    attempts: list[dict[str, Any]] | None,
) -> str | None:
    return _generation_diagnostics.request_event_provider_from_attempts(
        attempts,
        redis_text=_redis_text,
    )


_sanitize_provider_progress_payload = (
    _generation_diagnostics.sanitize_provider_progress_payload
)
_image_requested_params_snapshot = (
    _generation_diagnostics.image_requested_params_snapshot
)
_image_effective_params_snapshot = (
    _generation_diagnostics.image_effective_params_snapshot
)
_safe_generation_error_summary = _generation_diagnostics.safe_generation_error_summary
_build_generation_diagnostics = _generation_diagnostics.build_generation_diagnostics


_ensure_generation_attempt_current = (
    _generation_retry_state.ensure_generation_attempt_current
)
_mark_generation_attempt_failed = _generation_retry_state.mark_generation_attempt_failed
_mark_generation_attempt_retrying = (
    _generation_retry_state.mark_generation_attempt_retrying
)
_maybe_requeue_stale_generation_attempt = (
    _generation_retry_state.maybe_requeue_stale_generation_attempt
)
_await_with_lease_guard = _generation_retry_state.await_with_lease_guard
_consume_image_iter_close_result = (
    _generation_retry_state.consume_image_iter_close_result
)
_anext_image_with_guards = _generation_retry_state.anext_image_with_guards

_find_existing_generated_image = _generation_persistence.find_existing_generated_image
_ensure_generation_conversation_alive = (
    _generation_persistence.ensure_generation_conversation_alive
)

# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------


_classify_exception = _generation_retry_state.classify_exception
_safe_generation_error_details = _generation_retry_state.safe_generation_error_details
_sanitize_transparent_qc_payload = (
    _generation_retry_state.sanitize_transparent_qc_payload
)
_decide_moderation_retry_upgrade = (
    _generation_retry_state.decide_moderation_retry_upgrade
)

_delete_storage_keys = _generation_persistence.delete_storage_keys
_write_generation_files = _generation_persistence.write_generation_files
_cleanup_storage_on_error = _generation_persistence.cleanup_storage_on_error
_handle_dual_race_bonus_image = _generation_persistence.handle_dual_race_bonus_image
_raise_if_generation_interrupted = _generation_lifecycle.raise_if_generation_interrupted
_settle_existing_generated_image = _generation_lifecycle.settle_existing_generated_image
_finalize_running_generation_cancel = (
    _generation_lifecycle.finalize_running_generation_cancel
)

# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def run_generation(ctx: dict[str, Any], task_id: str) -> None:  # noqa: PLR0915, PLR0912
    """arq entry for generation task."""
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    lease_token = f"{worker_id}:{new_uuid7()}"
    _task_start = asyncio.get_event_loop().time()
    _task_deadline = _task_start + _RUN_GENERATION_TIMEOUT_S
    _task_outcome = "unknown"
    attempt = 0
    renewer: asyncio.Task[None] | None = None
    lease_lost = asyncio.Event()
    reserved_provider: Any | None = None
    reserved_provider_name: str | None = None
    user_api_credential_id: str | None = None
    user_runtime_provider: Any | None = None
    loaded_attempt = 0
    channel = task_channel(task_id)
    queue_metadata_payload: dict[str, Any] = {}
    trace_id = f"gen_{task_id}"
    stage_timer = _StageTimer()
    route_diagnostics: list[dict[str, Any]] = []
    gen_created_at: datetime | None = None

    # --- 1. 读 generation 行，幂等判断 ---
    async with SessionLocal() as session:
        # P1-10: skip_locked=True 让并发 worker 不会因为另一个事务已锁住此行
        # 而无限等待。锁不到和真实缺失都会返回 None，需用无锁读取区分。
        gen: Generation | None = (
            await session.execute(
                select(Generation)
                .where(Generation.id == task_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if gen is None:
            existing_id = (
                await session.execute(
                    select(Generation.id).where(Generation.id == task_id)
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                # 普通 MVCC SELECT 不等待行锁。此时尚未领取 provider 槽或 Redis
                # lease，静默退出即可；持锁方负责继续处理，幂等状态不被改写。
                logger.info(
                    "generation initial claim skipped locked row task_id=%s",
                    task_id,
                )
            else:
                logger.warning("generation not found task_id=%s", task_id)
            return
        if is_generation_terminal(gen.status):
            logger.info(
                "generation already terminal task_id=%s status=%s", task_id, gen.status
            )
            return
        if gen.status == GenerationStatus.RUNNING.value:
            logger.info("generation already running task_id=%s", task_id)
            return

        loaded_attempt = gen.attempt
        gen_created_at = getattr(gen, "created_at", None)
        user_id = gen.user_id
        message_id = gen.message_id
        action = gen.action
        prompt = gen.prompt
        aspect_ratio = gen.aspect_ratio
        size_requested = gen.size_requested
        input_image_ids = list(gen.input_image_ids or [])
        primary_input_image_id = gen.primary_input_image_id
        user_api_credential_id = getattr(gen, "user_api_credential_id", None)
        # 局部 inpaint mask（PostMessageIn.mask_image_id）。EDIT 任务可选；GENERATE
        # 任务忽略（schema 不允许，但防御性 detach 一份不影响）。worker 在 reference
        # images 加载阶段从 Image.storage_key 取 mask 字节。
        mask_image_id: str | None = getattr(gen, "mask_image_id", None)
        # session 关闭后仍要在 dual_race bonus 处理里读这两个字段，提前 detach 取值
        gen_idempotency_key = gen.idempotency_key
        gen_model = gen.model
        gen_upstream_request_snapshot: dict[str, Any] | None = (
            dict(gen.upstream_request)
            if isinstance(gen.upstream_request, dict)
            else None
        )
        trace_id = _generation_trace_id(task_id, gen_upstream_request_snapshot)
        stage_timer.set_ms("queue_wait", _queue_wait_ms(gen_created_at))
        image_request_options = _image_request_options(
            gen.upstream_request,
            size=size_requested,
        )

        try:
            await _ensure_generation_conversation_alive(
                session,
                message_id=message_id,
                user_id=user_id,
            )
        except _TaskCancelled as exc:
            result = await session.execute(
                _generation_attempt_update(
                    task_id,
                    gen.attempt,
                    statuses=(GenerationStatus.QUEUED.value,),
                ).values(
                    status=GenerationStatus.CANCELED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=EC.CANCELLED.value,
                    error_message=str(exc),
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_deleted = await session.get(Message, message_id)
            if msg_deleted is not None and msg_deleted.status not in (
                MessageStatus.SUCCEEDED,
                MessageStatus.FAILED,
                MessageStatus.CANCELED,
            ):
                msg_deleted.status = MessageStatus.FAILED
            await worker_billing.release_generation(
                session,
                gen,
                reason=EC.CANCELLED.value,
            )
            await session.commit()
            await worker_billing.flush_balance_cache_refreshes(session)
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": EC.CANCELLED.value,
                    "message": str(exc),
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            return

        if not _primary_input_image_id_valid(primary_input_image_id, input_image_ids):
            err_code = EC.INVALID_PARAM.value
            err_msg = "primary_input_image_id must be included in input_image_ids"
            result = await session.execute(
                _generation_attempt_update(
                    task_id,
                    gen.attempt,
                    statuses=(GenerationStatus.QUEUED.value,),
                ).values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=err_code,
                    error_message=err_msg,
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_invalid = await session.get(Message, message_id)
            if msg_invalid is not None and msg_invalid.status != MessageStatus.CANCELED:
                msg_invalid.status = MessageStatus.FAILED
            await worker_billing.release_generation(
                session,
                gen,
                reason=err_code,
            )
            await session.commit()
            await worker_billing.flush_balance_cache_refreshes(session)
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            return

        # Why: 重试时若上一次已经写过 Image 行（崩在 commit 之后、状态更新之前），
        # 不要再新建一份；直接复用旧记录并 publish succeeded，避免双图。
        existing_img = await _find_existing_generated_image(
            session, task_id=task_id, user_id=user_id
        )
        if existing_img is not None:
            _task_outcome = await _settle_existing_generated_image(
                session,
                redis=redis,
                task_id=task_id,
                user_id=user_id,
                message_id=message_id,
                generation=gen,
                existing_image=existing_img,
                task_started_at=_task_start,
            )
            return

        # --- 2. Max-attempt guard; actual running transition happens after
        # the unified image queue admits this task.
        attempt, attempt_may_run = _bounded_next_attempt(gen.attempt)
        if not attempt_may_run:
            err_code = "max_attempts_exceeded"
            err_msg = f"generation exceeded max attempts ({_MAX_ATTEMPTS})"
            result = await session.execute(
                _generation_attempt_update(
                    task_id,
                    gen.attempt,
                    statuses=(GenerationStatus.QUEUED.value,),
                ).values(
                    status=GenerationStatus.FAILED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    attempt=attempt,
                    finished_at=datetime.now(timezone.utc),
                    error_code=err_code,
                    error_message=err_msg,
                )
            )
            _ensure_generation_updated(result, task_id, gen.attempt)
            msg_failed = await session.get(Message, message_id)
            if msg_failed is not None and msg_failed.status != MessageStatus.CANCELED:
                msg_failed.status = MessageStatus.FAILED
            gen_failed = await session.get(Generation, task_id)
            if gen_failed is not None:
                await worker_billing.release_generation(
                    session,
                    gen_failed,
                    reason=err_code,
                )
            await session.commit()
            await worker_billing.flush_balance_cache_refreshes(session)
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            try:
                _duration = asyncio.get_event_loop().time() - _task_start
                task_duration_seconds.labels(
                    kind="generation", outcome=safe_outcome(_task_outcome)
                ).observe(_duration)
            except Exception:  # noqa: BLE001
                pass
            return

    provider_queue_delay = 0
    try:
        raw_image_route = await _resolve_image_primary_route()
    except Exception:  # noqa: BLE001
        raw_image_route = "responses"
    image_route = raw_image_route
    if user_api_credential_id:
        try:
            async with SessionLocal() as session:
                user_runtime_provider = await resolve_user_credential_runtime(
                    session,
                    user_api_credential_id,
                )
            # purpose 守卫：image 任务必须要 supplier purposes 包含 "image"，
            # 否则即便 credential 解析成功也拒掉，避免把 chat-only key 用到 image。
            if "image" not in (getattr(user_runtime_provider, "purposes", ()) or ()):
                raise UpstreamError(
                    "user API key supplier does not allow image purpose",
                    status_code=403,
                    error_code="byok_purpose_mismatch",
                    payload={"credential_id": user_api_credential_id},
                )
        except Exception as exc:  # noqa: BLE001
            byok_error = classify_user_credential_error(exc)[1] or "invalid_api_key"
            await record_user_credential_runtime_error(user_api_credential_id, exc)
            err_code = byok_error_to_generation_code(byok_error)
            err_msg = byok_error_message(byok_error)
            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        _generation_attempt_update(
                            task_id,
                            loaded_attempt,
                            statuses=(
                                GenerationStatus.QUEUED.value,
                                GenerationStatus.RUNNING.value,
                            ),
                        ).values(
                            status=GenerationStatus.FAILED.value,
                            progress_stage=GenerationStage.FINALIZING,
                            # 不要把 attempt 写回成局部 attempt（初值 0）；该任务可能
                            # 已经跑过若干次 retry，gen.attempt > 0 时回退会让监控/重试
                            # 计数错乱。保持原值即可。
                            attempt=loaded_attempt,
                            finished_at=datetime.now(timezone.utc),
                            error_code=err_code,
                            error_message=err_msg,
                        )
                    )
                    _ensure_generation_updated(result, task_id, loaded_attempt)
                    msg_failed = await session.get(Message, message_id)
                    if (
                        msg_failed is not None
                        and msg_failed.status != MessageStatus.CANCELED
                    ):
                        msg_failed.status = MessageStatus.FAILED
                    gen_failed = await session.get(Generation, task_id)
                    if gen_failed is not None:
                        await worker_billing.release_generation(
                            session,
                            gen_failed,
                            reason=err_code,
                        )
                    await session.commit()
                    await worker_billing.flush_balance_cache_refreshes(session)
            except _StaleGenerationAttempt:
                _task_outcome = "stale_attempt"
                return
            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": err_code,
                    "message": err_msg,
                    "retriable": False,
                },
            )
            _task_outcome = "failed"
            return
        if raw_image_route == "dual_race":
            route_diagnostics.append(
                {
                    "route": raw_image_route,
                    "fallback_route": "responses",
                    "reason": "byok_disables_dual_race",
                    "byok": True,
                }
            )
            image_route = "responses"
    requires_mask_provider = bool(mask_image_id) and action == GenerationAction.EDIT
    if requires_mask_provider and raw_image_route in {"dual_race", "responses"}:
        route_diagnostics.append(
            {
                "route": raw_image_route,
                "fallback_route": "generations",
                "reason": "mask_requires_generations_endpoint",
                "has_mask": True,
            }
        )
        image_route = "image2"
    is_dual_race = raw_image_route == "dual_race" and image_route == "dual_race"
    endpoint_kind = (
        "generations"
        if requires_mask_provider
        else None
        if is_dual_race
        else _image_endpoint_kind_for_engine(image_route)
    )
    reserve_queue_metadata = generation_queue_metadata(
        upstream_request=gen_upstream_request_snapshot,
        action=action,
        size_requested=size_requested,
        mask_image_id=mask_image_id,
        created_at=gen_created_at,
    )
    # 只在确认任务存在且可运行后注入 Redis，锁竞争/缺失/终态任务零副作用退出。
    try:
        from ..provider_pool import get_pool

        provider_pool = await get_pool()
        provider_pool.attach_redis(redis)
    except Exception:  # noqa: BLE001
        # attach 失败不致命——limiter 看到 redis=None 会短路放行
        logger.debug("provider_pool attach_redis failed", exc_info=True)
    # mask 不为空 → reserve 阶段把任务标记给 ProviderPool：sidecar 路径优先
    # file-mode provider，file-mode 候选耗尽时允许 url-mode 兜底；direct 路径本身
    # 是 multipart，不依赖 provider 的 image_edit_input_transport 配置。
    try:
        provider_wait_started = time.monotonic()
        reserved_provider = await _reserve_image_queue_slot(
            redis,
            task_id,
            dual_race=is_dual_race,
            endpoint_kind=endpoint_kind,
            requires_mask=requires_mask_provider,
            provider_override=user_runtime_provider,
            queue_lane=reserve_queue_metadata.get("queue_lane"),
            size_bucket=reserve_queue_metadata.get("size_bucket"),
            cost_class=reserve_queue_metadata.get("cost_class"),
        )
        stage_timer.add_elapsed("provider_wait", provider_wait_started)
    except UpstreamError as exc:
        # 兼容旧代码路径：如果仍有老 guard 抛 NO_MASK_CAPABLE_PROVIDER，按 terminal 处理。
        if getattr(exc, "error_code", None) == EC.NO_MASK_CAPABLE_PROVIDER.value:
            raise
        if getattr(exc, "error_code", None) != EC.ALL_ACCOUNTS_FAILED.value:
            raise
        provider_queue_delay = _IMAGE_PROVIDER_UNAVAILABLE_RETRY_S
        await redis.set(
            _image_queue_not_before_key(task_id),
            str(time.time() + provider_queue_delay),
            ex=provider_queue_delay + _IMAGE_QUEUE_NOT_BEFORE_GRACE_S,
        )
        await _enqueue_generation_once(
            redis,
            task_id,
            defer_by=provider_queue_delay,
        )
    if reserved_provider is None:
        await _clear_image_queue_enqueue_dedupe(redis, task_id)
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_QUEUED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "stage": GenerationStage.QUEUED.value,
                "substage": (
                    "waiting_provider" if provider_queue_delay else "waiting_queue"
                ),
                "reason": (
                    "image_provider_unavailable"
                    if provider_queue_delay
                    else "image_queue_waiting"
                ),
            },
        )
        _task_outcome = "queued"
        return

    reserved_provider_name = _redis_text(getattr(reserved_provider, "name", None))
    upstream_provider_label = (
        "dual_race"
        if _is_dual_race_sentinel(reserved_provider_name)
        else reserved_provider_name
    )

    # --- 3. lease + 续租协程 ---
    try:
        await _acquire_lease(redis, task_id, lease_token)
    except _LeaseLost as exc:
        logger.info("generation lease already held task=%s err=%s", task_id, exc)
        _task_outcome = "lease_held"
        await _release_image_queue_slot(
            redis, task_id=task_id, provider_name=reserved_provider_name
        )
        return

    async with SessionLocal() as session:
        # P1-10: skip_locked=True 同上——并发 worker 抢锁失败时不阻塞。
        current: Generation | None = (
            await session.execute(
                select(Generation)
                .where(Generation.id == task_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if current is None or is_generation_terminal(current.status):
            _task_outcome = "stale_attempt"
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id, lease_token)
            return
        attempt, attempt_may_run = _bounded_next_attempt(current.attempt)
        if not attempt_may_run:
            _task_outcome = "stale_attempt"
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id, lease_token)
            return
        running_upstream_request: dict[str, Any] = (
            dict(current.upstream_request)
            if isinstance(current.upstream_request, dict)
            else {}
        )
        lease_reacquired = current.error_code == "lease_lost"
        running_upstream_request["trace_id"] = trace_id
        running_upstream_request["upstream_route"] = image_route
        if route_diagnostics:
            running_upstream_request["route_diagnostics"] = route_diagnostics[:12]
        if is_dual_race:
            running_upstream_request.pop("provider", None)
            running_upstream_request.pop("actual_provider", None)
        elif upstream_provider_label:
            running_upstream_request["provider"] = upstream_provider_label
        started_at = datetime.now(timezone.utc)
        queue_metadata_payload = generation_queue_metadata(
            upstream_request=running_upstream_request,
            action=current.action,
            size_requested=current.size_requested,
            mask_image_id=current.mask_image_id,
            created_at=current.created_at,
            started_at=started_at,
            finished_at=current.finished_at,
            upstream_pixels=current.upstream_pixels,
            now=started_at,
        )
        running_upstream_request = merge_queue_metadata(
            running_upstream_request,
            queue_metadata_payload,
        )
        gen_upstream_request_snapshot = dict(running_upstream_request)
        result = await session.execute(
            update(Generation)
            .where(
                Generation.id == task_id,
                Generation.attempt == current.attempt,
                Generation.status == GenerationStatus.QUEUED.value,
            )
            .values(
                status=GenerationStatus.RUNNING.value,
                progress_stage=GenerationStage.RENDERING,
                started_at=started_at,
                attempt=attempt,
                upstream_request=running_upstream_request,
                error_code=None,
                error_message=None,
            )
        )
        try:
            _ensure_generation_updated(result, task_id, current.attempt)
        except _StaleGenerationAttempt:
            await _release_image_queue_slot(
                redis, task_id=task_id, provider_name=reserved_provider_name
            )
            await _release_lease(redis, task_id, lease_token)
            raise
        await session.commit()

    try:
        renewer = asyncio.create_task(
            _lease_renewer(
                redis,
                task_id,
                lease_token,
                lease_lost,
                # task_provider 反向索引仍是 SET key，需要 EXPIRE 续命；ZSET 槽位
                # 续命交给 lease_renewer 内部按 image_provider_name 分支处理。
                extra_lease_keys=[_image_task_provider_key(task_id)],
                image_provider_name=reserved_provider_name,
            )
        )

        # --- 4. publish started ---
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_STARTED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "attempt": attempt,
                "provider": None if is_dual_race else upstream_provider_label,
                "route": image_route,
                "lease_reacquired": bool(lease_reacquired),
                **queue_metadata_payload,
            },
        )
        if lease_reacquired:
            await publish_event(
                redis,
                user_id,
                channel,
                EV_GEN_PROGRESS,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "trace_id": trace_id,
                    "stage": GenerationStage.QUEUED.value,
                    "substage": _LEASE_REACQUIRED_SUBSTAGE,
                },
            )
        # 写入 in-flight provider 快照初值；后续 publish_image_progress 收到 provider_used 时
        # 覆盖具体 provider 名（dual_race 两条 lane 各占一个 field）。
        initial_inflight: dict[str, str] = {
            "mode": "dual_race" if is_dual_race else "single",
            "route": image_route or "",
            "task_id": task_id,
        }
        if not is_dual_race and reserved_provider_name:
            # 单 provider 模式下 reserve 阶段就锁定了 provider；先把它放进去，避免 admin
            # 在 provider_used 之前的几秒空窗看到"未记录"。
            initial_inflight["provider"] = reserved_provider_name
        await _inflight_set_fields(redis, task_id, initial_inflight)
        await _kick_image_queue(redis)
    except BaseException:
        _task_outcome = "setup_failed"
        await _cancel_renewer_task(renewer)
        renewer = None
        cleanup_future = asyncio.ensure_future(
            _release_generation_runtime_resources(
                redis,
                task_id=task_id,
                lease_token=lease_token,
                provider_name=reserved_provider_name,
                clear_avoided_providers=True,
            )
        )
        try:
            await asyncio.shield(cleanup_future)
        except asyncio.CancelledError:
            cleanup_future.add_done_callback(
                lambda _task: logger.debug(
                    "generation late setup cleanup finished task=%s",
                    task_id,
                )
            )
        raise

    has_partial = (
        False  # 新同步路径不存在 partial，永远为 False（保留变量给下方 classify 用）
    )
    image_iter: AsyncIterator[tuple[str, str | None]] | None = None
    provider_attempt_log: list[dict[str, Any]] = []
    upstream_duration_ms: int | None = None
    requested_image_count = _image_requested_count(gen_upstream_request_snapshot)
    batch_extra_pairs: list[tuple[int, tuple[str, str | None]]] = []
    requested_params_for_diag = _image_requested_params_snapshot(
        gen_upstream_request_snapshot,
        size=size_requested,
        aspect_ratio=aspect_ratio,
        action=action,
        input_count=len(input_image_ids),
        has_mask=bool(mask_image_id),
    )

    try:
        # 新 API 不支持 size="auto"，强制走 fixed 模式（让 resolve_size 走预设/比例回退）
        normalize_started = time.monotonic()
        size_mode: SizeMode = "fixed"
        fixed_size = (
            size_requested if (size_requested and "x" in size_requested) else None
        )
        try:
            resolved = resolve_size(
                cast(AspectRatio, aspect_ratio),
                size_mode,
                fixed_size,
            )
            _validate_resolved_size(
                resolved.size,
                aspect_ratio,
                validate_aspect_ratio=fixed_size is None,
            )
        except ValueError as exc:
            # GEN-P2 size_requested API 层校验补丁：worker 兜底捕获 sizing.validate_explicit_size
            # 抛的 ValueError，转为 terminal UpstreamError 并走 failed 分支。即使 API 漏检
            # （比如旧 task 已落库），这里也不会让 worker 直接崩。
            raise UpstreamError(
                f"invalid size_requested: {exc}",
                status_code=400,
                error_code=EC.INVALID_VALUE.value,
                payload={
                    "size_requested": size_requested,
                    "aspect_ratio": aspect_ratio,
                },
            ) from exc
        # resolved.size 此时必为 "{W}x{H}"（不会是 "auto"）
        image_request_options = _image_request_options(
            gen.upstream_request,
            size=resolved.size,
        )
        prompt_for_upstream = _prompt_with_aspect_ratio_constraint(
            prompt,
            aspect_ratio,
        )

        async with SessionLocal() as session:
            references = await _load_reference_images(session, input_image_ids)
            # mask_image_id 仅 EDIT + 局部 inpaint 任务设置；GENERATE 任务忽略。
            # 与 reference 同 session 加载（少跑一次连接），mask 拿到后立刻按第一张
            # 参考图尺寸 normalize（OpenAI /v1/images/edits + mask 要求等尺寸）。
            mask_bytes_raw: bytes | None = None
            if mask_image_id and action == GenerationAction.EDIT:
                mask_bytes_raw = await _load_mask_image(session, mask_image_id)

        ref_for_body = references if action == GenerationAction.EDIT else []
        # mask normalize：与第一张参考图尺寸对齐，模式统一为 RGBA。reference 解码
        # 在 _normalize_reference_image 还会再过一遍（统一 WebP 编码），所以这里只
        # 用第一张原字节量像素尺寸即可，不重复重编码。
        mask_bytes: bytes | None = None
        # inpaint 输出 size 强制对齐参考图像素尺寸——否则 1024x768 输入要被升采样
        # 到 4K 输出时，gpt-image-2 实测会把 mask 外区域重画 / mask 错位，"局部修改"
        # 退化成"整张重生成"。失败（参考图比例太极端 / 解码失败）保持 None，调用方
        # 走原 resolved.size 兜底。
        inpaint_size_override: str | None = None
        if mask_bytes_raw is not None and ref_for_body:
            mask_bytes = _resize_mask_to_reference(mask_bytes_raw, ref_for_body[0][1])
            ref_size = _reference_pixel_size(ref_for_body[0][1])
            if ref_size is not None:
                inpaint_size_override = _inpaint_size_from_reference(*ref_size)
        stage_timer.add_elapsed("normalize", normalize_started)

        # 即将调用上游（同步 HTTP，20-60s），先推一条 rendering progress 让前端切指示。
        # substage=stream_started 让 DevelopingCard 显影扫光从"占位"切到"真在工作"。
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "stage": GenerationStage.RENDERING.value,
                "substage": GenerationStage.STREAM_STARTED.value,
            },
        )

        b64_result: str | None = None
        revised_prompt: str | None = None
        # dual_race async generator：winner 先 yield 一次，loser 也成功时再 yield bonus。
        # 主流程取首张作 winner；处理完成后再尝试取第二张做 bonus image，复用同 iter。
        provider_used_events: list[dict[str, str]] = []
        # image-job sidecar 把"公网图片地址"通过 image_job_image 事件回传；这里只暂存，
        # 成功提交时再合并到 generation.upstream_request，让 admin 请求事件面板能展示
        # 该 job 对应的 sidecar 临时图 URL（不只 inlined image，方便排查）。
        image_job_meta: dict[str, Any] = {}

        def pop_provider_used_event() -> dict[str, str]:
            if provider_used_events:
                return provider_used_events.pop(0)
            return {}

        async def publish_image_progress(event: dict[str, Any]) -> None:
            nonlocal has_partial
            # GEN-P1-4: 进度回调里检查 cancel——partial / fallback_started 等节点自然
            # 节流（不会每 token），命中后 raise 让 race 任务被 cancel + 终态走 _TaskCancelled。
            if lease_lost.is_set():
                raise _LeaseLost("generation lease renewer failed")
            if await _is_cancelled(redis, task_id):
                raise _TaskCancelled("cancelled during upstream call")
            event_type = event.get("type")
            if event_type == "image_job_image":
                url = _redis_text(event.get("image_job_url"))
                if url:
                    image_job_meta["image_job_url"] = url
                for key in ("job_id", "endpoint_used", "expires_at", "format"):
                    value = event.get(key)
                    if value is not None:
                        image_job_meta[
                            f"image_job_{key}"
                            if not key.startswith("image_job_")
                            else key
                        ] = value
                return
            if event_type == "route_diagnostic":
                diag = {
                    "route": event.get("route"),
                    "fallback_route": event.get("fallback_route"),
                    "reason": event.get("reason"),
                    "byok": event.get("byok"),
                    "status": event.get("status") or "routed",
                }
                route_diagnostics.append(
                    {k: v for k, v in diag.items() if v is not None}
                )
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    _sanitize_provider_progress_payload(
                        {
                            "generation_id": task_id,
                            "message_id": message_id,
                            "trace_id": trace_id,
                            "stage": GenerationStage.RENDERING.value,
                            "substage": GenerationStage.PROVIDER_SELECTED.value,
                            "route_diagnostic": True,
                            "provider": event.get("provider"),
                            "route": event.get("route"),
                            "fallback_route": event.get("fallback_route"),
                            "reason": event.get("reason"),
                            "byok": event.get("byok"),
                        },
                        expose_provider_diagnostics=(
                            settings.expose_provider_diagnostics
                        ),
                    ),
                )
                return
            if event_type == "endpoint_failover":
                provider_attempt_log.append(
                    _provider_attempt_from_progress(
                        event,
                        status="failover",
                        attempt_epoch=attempt,
                        route_default="image_jobs",
                    )
                )
                # Inner-loop endpoint switch (generations ↔ responses) on the
                # same provider — keep semantics close to provider_failover so
                # the front-end can render a similar pill.
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    _sanitize_provider_progress_payload(
                        {
                            "generation_id": task_id,
                            "message_id": message_id,
                            "trace_id": trace_id,
                            "stage": GenerationStage.RENDERING.value,
                            "substage": GenerationStage.PROVIDER_SELECTED.value,
                            "endpoint_failover": True,
                            "provider": event.get("provider"),
                            "from_endpoint": event.get("from_endpoint"),
                            "remaining": event.get("remaining"),
                            "reason": event.get("reason"),
                            "route": event.get("route") or "image_jobs",
                        },
                        expose_provider_diagnostics=(
                            settings.expose_provider_diagnostics
                        ),
                    ),
                )
                return
            if event_type == "provider_used":
                provider = _redis_text(
                    event.get("provider") or event.get("actual_provider")
                )
                if provider:
                    metadata: dict[str, str] = {"provider": provider}
                    for source_key, target_key in (
                        ("route", "route"),
                        ("source", "source"),
                        ("endpoint", "endpoint"),
                    ):
                        value = _redis_text(event.get(source_key))
                        if value:
                            metadata[target_key] = value
                    provider_used_events.append(metadata)
                    provider_attempt_log.append(
                        {
                            **_provider_attempt_from_progress(
                                event,
                                status="used",
                                attempt_epoch=attempt,
                            ),
                            **metadata,
                        }
                    )
                    # 同步把当前 lane 的 provider 落到 in-flight 快照里。
                    inflight_update: dict[str, str] = {}
                    route_text = metadata.get("route") or ""
                    endpoint_text = metadata.get("endpoint") or ""
                    if is_dual_race:
                        lane_field = _classify_inflight_lane(route_text, endpoint_text)
                        inflight_update[f"{lane_field}_provider"] = provider
                        if route_text:
                            inflight_update[f"{lane_field}_route"] = route_text
                        if endpoint_text:
                            inflight_update[f"{lane_field}_endpoint"] = endpoint_text
                    else:
                        inflight_update["provider"] = provider
                        if route_text:
                            inflight_update["actual_route"] = route_text
                        if endpoint_text:
                            inflight_update["endpoint"] = endpoint_text
                    await _inflight_set_fields(redis, task_id, inflight_update)
                return
            if event_type == "partial_image":
                has_partial = True
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PARTIAL_IMAGE,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "trace_id": trace_id,
                        "stage": GenerationStage.RENDERING.value,
                        "substage": GenerationStage.PARTIAL_RECEIVED.value,
                        "index": event.get("index"),
                        "count": event.get("count"),
                    },
                )
                return
            if event_type in {"fallback_started", "final_image", "completed"}:
                stage = (
                    GenerationStage.FINALIZING.value
                    if event_type in {"final_image", "completed"}
                    else GenerationStage.RENDERING.value
                )
                substage = (
                    GenerationStage.FINAL_RECEIVED.value
                    if event_type in {"final_image", "completed"}
                    else GenerationStage.STREAM_STARTED.value
                )
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "trace_id": trace_id,
                        "stage": stage,
                        "substage": substage,
                        "source": event.get("source") or "responses_fallback",
                    },
                )
                return
            # P2 worker 内 failover：上游 retriable 错误时立即换 provider 再试。
            # 推 substage=provider_selected + provider_failover=true，让前端把 DevelopingCard
            # 切到"换号重试"指示，区别于首次进入 stream_started。不发额外 SSE 事件，
            # 复用 generation.progress；旧前端不识别 provider_failover 字段也无影响。
            if event_type == "provider_failover":
                # 把"刚刚失败"的 provider 记进快照，标 status=failover；下一条 provider_used
                # 会覆盖回 active。这样 admin 列表能看到"X 切走了，正在选下一个"。
                from_provider = _redis_text(event.get("from_provider"))
                route_text = _redis_text(event.get("route")) or ""
                provider_attempt_log.append(
                    _provider_attempt_from_progress(
                        event,
                        status="failover",
                        attempt_epoch=attempt,
                        provider_key="from_provider",
                        route_default=route_text or None,
                    )
                )
                failover_inflight_update: dict[str, str] = {}
                if is_dual_race:
                    lane_field = _classify_inflight_lane(route_text, "")
                    failover_inflight_update[f"{lane_field}_status"] = "failover"
                    if from_provider:
                        failover_inflight_update[f"{lane_field}_last_failed"] = (
                            from_provider
                        )
                else:
                    failover_inflight_update["status"] = "failover"
                    if from_provider:
                        failover_inflight_update["last_failed"] = from_provider
                await _inflight_set_fields(
                    redis,
                    task_id,
                    failover_inflight_update,
                )
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    _sanitize_provider_progress_payload(
                        {
                            "generation_id": task_id,
                            "message_id": message_id,
                            "trace_id": trace_id,
                            "stage": GenerationStage.RENDERING.value,
                            "substage": GenerationStage.PROVIDER_SELECTED.value,
                            "provider_failover": True,
                            "from_provider": event.get("from_provider"),
                            "remaining": event.get("remaining"),
                            "reason": event.get("reason"),
                            "route": event.get("route") or "responses",
                        },
                        expose_provider_diagnostics=(
                            settings.expose_provider_diagnostics
                        ),
                    ),
                )

        async with asyncio.timeout_at(_task_deadline):
            # GEN-P1-4: 拿到图片队列槽但还没发上游请求时再确认一次取消。
            if lease_lost.is_set():
                raise _LeaseLost("generation lease renewer failed")
            if await _is_cancelled(redis, task_id):
                raise _TaskCancelled("cancelled before upstream request")
            with _tracer.start_as_current_span("upstream.generate_image") as _span:
                try:
                    _span.set_attribute("lumen.task_id", task_id)
                    _span.set_attribute("lumen.action", action)
                    # inpaint 路径用对齐参考图的 size override；observability 看到的 size
                    # 才和实际下发到 /v1/images/edits 的一致（不然 admin 排查时会困惑）。
                    _span.set_attribute(
                        "lumen.size", inpaint_size_override or resolved.size
                    )
                    if inpaint_size_override:
                        _span.set_attribute("lumen.size_requested", resolved.size)
                    if reserved_provider_name:
                        _span.set_attribute("lumen.provider", reserved_provider_name)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    responses_model = str(image_request_options["responses_model"])
                    # Retry 打散：把当前 task attempt 写入 ContextVar，下游 body 构造点会读到。
                    # 必须用 `attempt`（line 2084 _bounded_next_attempt 算出的新值）而非 `gen.attempt`
                    # （load 时刻的旧值）——后者会让 ContextVar 错位 1 格：数据库 attempt=2 时 push
                    # 旧值 1，cache buster 不触发；数据库 attempt=3 时 push 旧值 2，effort 走 minimal
                    # 而非 high。实测 lane A 反复 server_error 时 image-job 那边 payload 显示
                    # effort=minimal+cache_key=lumen-retry-* 但实际是 attempt=3 的请求 → 证实错位。
                    # attempt == 1 首次（不打散）；>= 2 每次都用不同 prompt_cache_key /
                    # reasoning.effort，绕开 ChatGPT codex 端的"故障 cache"和 sub2api sticky session。
                    retry_attempt_token = push_image_retry_attempt(attempt)
                    trace_token = push_image_trace_id(trace_id)
                    quota_token = push_image_quota_context(task_id, attempt)
                    upstream_started = time.monotonic()
                    try:
                        if action == GenerationAction.EDIT:
                            if not ref_for_body:
                                raise UpstreamError(
                                    "edit action requires at least one reference image",
                                    error_code=EC.INVALID_REQUEST_ERROR.value,
                                    status_code=400,
                                )
                            image_iter = edit_image(
                                prompt=prompt_for_upstream,
                                # mask 不为 None 时优先用对齐到参考图尺寸的 inpaint
                                # override；否则走 user resolved.size（普通 i2i 行为）。
                                size=inpaint_size_override or resolved.size,
                                images=[raw for _sha, raw in ref_for_body],
                                mask=mask_bytes,
                                quality=str(image_request_options["render_quality"]),
                                output_format=str(
                                    image_request_options["output_format"]
                                ),
                                output_compression=image_request_options.get(
                                    "output_compression"
                                ),
                                background=str(image_request_options["background"]),
                                moderation=str(image_request_options["moderation"]),
                                n=requested_image_count,
                                model=responses_model,
                                progress_callback=publish_image_progress,
                                provider_override=(
                                    None if is_dual_race else reserved_provider
                                ),
                                user_id=user_id,
                            )
                        else:
                            image_iter = generate_image(
                                prompt=prompt_for_upstream,
                                size=resolved.size,
                                quality=str(image_request_options["render_quality"]),
                                output_format=str(
                                    image_request_options["output_format"]
                                ),
                                output_compression=image_request_options.get(
                                    "output_compression"
                                ),
                                background=str(image_request_options["background"]),
                                moderation=str(image_request_options["moderation"]),
                                n=requested_image_count,
                                model=responses_model,
                                progress_callback=publish_image_progress,
                                provider_override=(
                                    None if is_dual_race else reserved_provider
                                ),
                                user_id=user_id,
                            )
                        first_pair = await _anext_image_with_guards(
                            image_iter,
                            lease_lost,
                            redis=redis,
                            task_id=task_id,
                        )
                    finally:
                        pop_image_quota_context(quota_token)
                        pop_image_trace_id(trace_token)
                        pop_image_retry_attempt(retry_attempt_token)
                    if first_pair is None:
                        raise UpstreamError(
                            "upstream image generator yielded no result",
                            error_code=EC.NO_IMAGE_RETURNED.value,
                            status_code=200,
                        )
                    b64_result, revised_prompt = first_pair
                    upstream_duration_ms = int(
                        max(0.0, time.monotonic() - upstream_started) * 1000
                    )
                    stage_timer.set_ms("render", upstream_duration_ms)
                    winner_provider_event = pop_provider_used_event()
                    actual_upstream_provider = winner_provider_event.get("provider")
                    actual_upstream_route = winner_provider_event.get("route")
                    actual_upstream_source = winner_provider_event.get("source")
                    actual_upstream_endpoint = winner_provider_event.get("endpoint")
                    if (
                        requested_image_count > 1
                        and image_iter is not None
                        and actual_upstream_source
                        in {"image2_direct", "image2_edit_direct"}
                    ):
                        for batch_index in range(2, requested_image_count + 1):
                            try:
                                extra_pair = await _anext_image_with_guards(
                                    image_iter,
                                    lease_lost,
                                    redis=redis,
                                    task_id=task_id,
                                )
                            except (
                                _LeaseLost,
                                _TaskCancelled,
                                asyncio.CancelledError,
                            ):
                                raise
                            except Exception as exc:  # noqa: BLE001
                                logger.warning(
                                    "image2 n extra iter failed task=%s index=%s err=%r",
                                    task_id,
                                    batch_index,
                                    exc,
                                )
                                break
                            if extra_pair is None:
                                logger.warning(
                                    "image2 n returned fewer images task=%s requested=%s actual=%s",
                                    task_id,
                                    requested_image_count,
                                    batch_index - 1,
                                )
                                break
                            batch_extra_pairs.append((batch_index, extra_pair))
                    upstream_calls_total.labels(kind="generation", outcome="ok").inc()
                except Exception:
                    upstream_calls_total.labels(
                        kind="generation", outcome="error"
                    ).inc()
                    raise

        if not b64_result:
            # 降级到文本了——按 retriable 处理
            raise UpstreamError(
                "upstream returned no image (tool_choice downgrade?)",
                error_code=EC.NO_IMAGE_RETURNED.value,
                status_code=200,
            )
        await _raise_if_generation_interrupted(
            redis,
            task_id,
            lease_lost,
            "cancelled after upstream result",
        )

        # 上游已返回图像 base64，进入本地解码/缩略图/落盘阶段
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": GenerationStage.FINAL_RECEIVED.value,
            },
        )

        # 进入本地处理阶段（解码 / blurhash / 3 个 variant）。
        # 用细 substage=processing 让前端 DevelopingCard 切到"处理中"动画；
        # 粗 stage 仍是 finalizing，保持现有前端兼容。
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": GenerationStage.PROCESSING.value,
            },
        )

        # --- 解码 + 校验 ---
        postprocess_started = time.monotonic()
        try:
            raw_image = _decode_upstream_image_b64(b64_result)
        except binascii.Error as exc:
            raise UpstreamError(
                f"bad base64 from upstream: {exc}",
                error_code=EC.BAD_RESPONSE.value,
                status_code=200,
            ) from exc

        sha = _sha256(raw_image)

        # §7.5 SHA-256 回退检测
        if action == GenerationAction.EDIT:
            if any(sha == ref_sha for ref_sha, _ in references):
                raise UpstreamError(
                    "upstream returned original image unchanged (sha echo)",
                    error_code=EC.SHA_ECHO.value,
                    status_code=200,
                )

        transparent_requested = image_request_options.get("background") == "transparent"
        processed_image = await _await_with_lease_guard(
            _postprocess_raw_generated_image(
                raw_image,
                prompt=prompt,
                transparent_requested=transparent_requested,
            ),
            lease_lost,
            redis=redis,
            task_id=task_id,
        )
        raw_image = processed_image.raw_image
        sha = processed_image.sha256
        orig_format = processed_image.orig_format
        width = processed_image.width
        height = processed_image.height
        actual_image_count = 1 + len(batch_extra_pairs)
        blurhash_str = processed_image.blurhash
        display_bytes = processed_image.display.bytes
        display_size = processed_image.display.size
        preview_bytes = processed_image.preview.bytes
        preview_size = processed_image.preview.size
        thumb_bytes = processed_image.thumb.bytes
        thumb_size = processed_image.thumb.size
        transparent_alpha_recovered = processed_image.transparent_alpha_recovered
        transparent_qc_payload = processed_image.transparent_qc_payload
        transparent_provider = processed_image.transparent_provider
        stage_timer.add_elapsed("normalize", postprocess_started)

        # --- 写存储 ---
        image_id = new_uuid7()
        orig_ext_by_format = {"PNG": "png", "WEBP": "webp", "JPEG": "jpg"}
        orig_mime_by_format = {
            "PNG": "image/png",
            "WEBP": "image/webp",
            "JPEG": "image/jpeg",
        }
        orig_ext = orig_ext_by_format[orig_format]
        orig_mime = orig_mime_by_format[orig_format]
        model_metadata = _model_image_metadata_from_request(
            image_id=image_id,
            mime=orig_mime,
            request=gen_upstream_request_snapshot,
            prompt=prompt,
        )
        effective_params_for_diag = _image_effective_params_snapshot(
            image_request_options,
            size=inpaint_size_override or resolved.size,
            width=width,
            height=height,
            mime=orig_mime,
        )
        image_metadata = dict(model_metadata)
        if model_metadata:
            try:
                with PILImage.open(io.BytesIO(raw_image)) as im:
                    im.load()
                    raw_image = _maybe_embed_model_image_metadata_bytes(
                        image=im,
                        fmt=orig_format,
                        raw_image=raw_image,
                        metadata=model_metadata,
                    )
                sha = _sha256(raw_image)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "model_library image metadata embed skipped task=%s err=%s",
                    task_id,
                    exc,
                )
        key_orig = f"u/{user_id}/g/{task_id}/orig.{orig_ext}"
        key_display = f"u/{user_id}/g/{task_id}/display2048.webp"
        key_preview = f"u/{user_id}/g/{task_id}/preview1024.webp"
        key_thumb = f"u/{user_id}/g/{task_id}/thumb256.jpg"

        await _raise_if_generation_interrupted(
            redis,
            task_id,
            lease_lost,
            "cancelled before storage write",
        )
        # 进入"写盘"细子阶段。线上 IO 通常 100-300ms 即结束，前端可借此让显影扫光收尾。
        await publish_event(
            redis,
            user_id,
            channel,
            EV_GEN_PROGRESS,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "trace_id": trace_id,
                "stage": GenerationStage.FINALIZING.value,
                "substage": GenerationStage.STORING.value,
            },
        )

        upload_started = time.monotonic()
        created_storage_keys = await _await_with_lease_guard(
            _write_generation_files(
                [
                    (key_orig, raw_image),
                    (key_display, display_bytes),
                    (key_preview, preview_bytes),
                    (key_thumb, thumb_bytes),
                ]
            ),
            lease_lost,
            redis=redis,
            task_id=task_id,
        )
        stage_timer.add_elapsed("upload", upload_started)

        generation_diagnostics = _build_generation_diagnostics(
            trace_id=trace_id,
            requested_params=requested_params_for_diag,
            effective_params=effective_params_for_diag,
            revised_prompt=revised_prompt,
            provider=actual_upstream_provider
            or (upstream_provider_label if not is_dual_race else None),
            upstream_route=image_route,
            actual_route=actual_upstream_route,
            actual_source=actual_upstream_source,
            actual_endpoint=actual_upstream_endpoint,
            provider_attempts=provider_attempt_log,
            stage_timings_ms=stage_timer.snapshot(),
            route_diagnostics=route_diagnostics,
            upstream_duration_ms=upstream_duration_ms,
            duration_ms=int(max(0.0, time.monotonic() - _task_start) * 1000),
            debug_id=task_id,
            expose_provider_diagnostics=settings.expose_provider_diagnostics,
        )
        image_metadata["generation_diagnostics"] = generation_diagnostics
        if revised_prompt:
            image_metadata["revised_prompt"] = revised_prompt
        # --- 写 DB ---
        conversation_id_for_title: str | None = None
        parent_upstream_request_for_bonus: dict[str, Any] | None = None
        async with _cleanup_storage_on_error(created_storage_keys):
            await _raise_if_generation_interrupted(
                redis,
                task_id,
                lease_lost,
                "cancelled before generation persistence",
            )
            async with SessionLocal() as session:
                await _ensure_generation_attempt_current(session, task_id, attempt)
                conversation_id_for_title = await _ensure_generation_conversation_alive(
                    session,
                    message_id=message_id,
                    user_id=user_id,
                    lock=True,
                )
                img = Image(
                    id=image_id,
                    user_id=user_id,
                    owner_generation_id=task_id,
                    source=ImageSource.GENERATED.value,
                    parent_image_id=(
                        primary_input_image_id
                        if action == GenerationAction.EDIT
                        else None
                    ),
                    storage_key=key_orig,
                    mime=orig_mime,
                    width=width,
                    height=height,
                    size_bytes=len(raw_image),
                    sha256=sha,
                    blurhash=blurhash_str,
                    visibility="private",
                    metadata_jsonb=image_metadata,
                )
                session.add(img)
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="display2048",
                        storage_key=key_display,
                        width=display_size[0],
                        height=display_size[1],
                    )
                )
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="preview1024",
                        storage_key=key_preview,
                        width=preview_size[0],
                        height=preview_size[1],
                    )
                )
                session.add(
                    ImageVariant(
                        image_id=image_id,
                        kind="thumb256",
                        storage_key=key_thumb,
                        width=thumb_size[0],
                        height=thumb_size[1],
                    )
                )

                # UPDATE generation 成功态
                upstream_req: dict[str, Any] = (
                    dict(gen_upstream_request_snapshot)
                    if isinstance(gen_upstream_request_snapshot, dict)
                    else dict(gen.upstream_request)
                    if isinstance(gen.upstream_request, dict)
                    else {}
                )
                upstream_req.update(image_request_options)
                upstream_req["trace_id"] = trace_id
                upstream_req["size_actual"] = f"{width}x{height}"
                upstream_req["mime"] = orig_mime
                upstream_req["upstream_route"] = image_route
                upstream_req["requested_params"] = requested_params_for_diag
                upstream_req["effective_params"] = effective_params_for_diag
                upstream_req["image_count_requested"] = requested_image_count
                upstream_req["image_count_actual"] = actual_image_count
                upstream_req["generation_diagnostics"] = generation_diagnostics
                if route_diagnostics:
                    upstream_req["route_diagnostics"] = route_diagnostics[:12]
                upstream_req["debug_id"] = task_id
                if upstream_duration_ms is not None:
                    upstream_req["upstream_duration_ms"] = upstream_duration_ms
                if provider_attempt_log:
                    upstream_req["provider_attempts"] = provider_attempt_log[:12]
                request_event_provider = (
                    actual_upstream_provider
                    or (upstream_provider_label if not is_dual_race else None)
                    or _request_event_provider_from_attempts(provider_attempt_log)
                )
                if actual_upstream_provider:
                    upstream_req["provider"] = actual_upstream_provider
                    upstream_req["actual_provider"] = actual_upstream_provider
                elif upstream_provider_label and not is_dual_race:
                    upstream_req["provider"] = upstream_provider_label
                else:
                    upstream_req.pop("provider", None)
                    upstream_req.pop("actual_provider", None)
                if request_event_provider:
                    upstream_req["request_event_provider"] = request_event_provider
                else:
                    upstream_req.pop("request_event_provider", None)
                if actual_upstream_route:
                    upstream_req["actual_route"] = actual_upstream_route
                if actual_upstream_source:
                    upstream_req["actual_source"] = actual_upstream_source
                if actual_upstream_endpoint:
                    upstream_req["actual_endpoint"] = actual_upstream_endpoint
                if transparent_alpha_recovered:
                    upstream_req["transparent_alpha_recovered"] = True
                if transparent_qc_payload is not None:
                    upstream_req["transparent_qc"] = transparent_qc_payload
                if transparent_provider is not None:
                    upstream_req["transparent_pipeline_provider"] = transparent_provider
                if revised_prompt:
                    upstream_req["revised_prompt"] = revised_prompt
                if image_job_meta:
                    for key, value in image_job_meta.items():
                        upstream_req[key] = value
                upstream_req = _sanitize_generation_upstream_request(
                    upstream_req,
                    expose_provider_diagnostics=settings.expose_provider_diagnostics,
                )
                parent_upstream_request_for_bonus = dict(upstream_req)

                result = await session.execute(
                    _generation_attempt_update(
                        task_id,
                        attempt,
                        statuses=_RUNNING_GENERATION_STATUSES,
                    ).values(
                        status=GenerationStatus.SUCCEEDED.value,
                        progress_stage=GenerationStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        upstream_pixels=width * height,
                        upstream_request=upstream_req,
                        error_code=None,
                        error_message=None,
                    )
                )
                _ensure_generation_updated(result, task_id, attempt)

                # UPDATE message.content 把生成图挂进去（§6.6 step 7）
                msg: Message | None = await session.get(Message, message_id)
                if msg is not None and msg.status != MessageStatus.CANCELED:
                    content = dict(msg.content or {})
                    images_list = list(content.get("images") or [])
                    image_ref = {
                        "image_id": image_id,
                        "from_generation_id": task_id,
                        "width": width,
                        "height": height,
                        "mime": orig_mime,
                        "url": storage.public_url(key_orig),
                        "display_url": f"/api/images/{image_id}/variants/display2048",
                        "preview_url": f"/api/images/{image_id}/variants/preview1024",
                        "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                        "filename": model_metadata.get("suggested_filename"),
                        **_compact_image_payload_meta(image_metadata),
                    }
                    images_list.append(image_ref)
                    content["images"] = images_list
                    msg.content = content
                    msg.status = MessageStatus.SUCCEEDED

                try:
                    await _maybe_record_model_library_generate_image(
                        session=session,
                        user_id=user_id,
                        generation=gen,
                        image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 模特库 hook 任何异常都不能让主生成任务从 succeeded 翻成 failed
                    logger.warning(
                        "model_library_generate post-success hook failed task=%s err=%s",
                        task_id,
                        exc,
                    )

                try:
                    await _maybe_record_poster_workflow_image(
                        session=session,
                        user_id=user_id,
                        generation=gen,
                        image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # poster hook 任何异常都不能把 succeeded 翻成 failed
                    logger.warning(
                        "poster_workflow post-success hook failed task=%s err=%s",
                        task_id,
                        exc,
                    )

                try:
                    await _maybe_record_poster_style_library_generate_image(
                        session=session,
                        user_id=user_id,
                        generation=gen,
                        image_id=image_id,
                    )
                except (TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 风格库 hook 任何异常都不能把 succeeded 翻成 failed
                    logger.warning(
                        "poster_style_library_generate post-success hook failed task=%s err=%s",
                        task_id,
                        exc,
                    )

                await _raise_if_generation_interrupted(
                    redis,
                    task_id,
                    lease_lost,
                    "cancelled before billing settlement",
                )
                await worker_billing.settle_generation(
                    session,
                    gen,
                    width=width,
                    height=height,
                    image_count=1,
                )
                await _raise_if_generation_interrupted(
                    redis,
                    task_id,
                    lease_lost,
                    "cancelled before success commit",
                )
                success_delivery = _stage_generation_success_event(
                    session,
                    user_id,
                    channel,
                    generation_id=task_id,
                    message_id=message_id,
                    image_id=image_id,
                    actual_size=f"{width}x{height}",
                    mime=orig_mime,
                    image_url=storage.public_url(key_orig),
                    filename=model_metadata.get("suggested_filename"),
                    image_payload_meta=_compact_image_payload_meta(image_metadata),
                    diagnostics=generation_diagnostics,
                )
                await session.commit()
                await worker_billing.flush_balance_cache_refreshes(session)

        await _deliver_generation_event(redis, success_delivery)
        _task_outcome = "succeeded"

        if batch_extra_pairs:
            for batch_index, (extra_b64, extra_revised) in batch_extra_pairs:
                try:
                    await _handle_dual_race_bonus_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        parent_task_id=task_id,
                        parent_idempotency_key=gen_idempotency_key,
                        parent_upstream_request=(
                            parent_upstream_request_for_bonus
                            or gen_upstream_request_snapshot
                        ),
                        message_id=message_id,
                        action=str(action),
                        model=gen_model,
                        prompt=prompt,
                        size_requested=size_requested,
                        aspect_ratio=aspect_ratio,
                        input_image_ids=input_image_ids,
                        primary_input_image_id=primary_input_image_id,
                        references=references,
                        image_request_options=image_request_options,
                        b64_result=extra_b64,
                        revised_prompt=extra_revised,
                        upstream_provider=actual_upstream_provider,
                        upstream_actual_route=actual_upstream_route,
                        upstream_actual_source=actual_upstream_source,
                        upstream_actual_endpoint=actual_upstream_endpoint,
                        billing_meta={
                            "billing_free": False,
                            "billing_label": "billable",
                            "billing_policy": "batch_extra_settled_separately",
                        },
                        idempotency_suffix=f":n{batch_index}",
                        extra_upstream_fields={
                            "batch_parent_generation_id": task_id,
                            "batch_index": batch_index,
                            "batch_count": actual_image_count,
                        },
                        record_model_library_candidate=False,
                        settle_billing=True,
                        log_label="image2 n result",
                    )
                except (_LeaseLost, _TaskCancelled, asyncio.CancelledError):
                    logger.info(
                        "image2 n result finalize aborted by cancel/lease task=%s index=%s",
                        task_id,
                        batch_index,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "image2 n result finalize unexpected error task=%s index=%s err=%r",
                        task_id,
                        batch_index,
                        exc,
                    )

        # 自动起会话标题（第一轮生成完成后触发；内部幂等）
        if conversation_id_for_title:
            from .auto_title import maybe_enqueue_auto_title

            await maybe_enqueue_auto_title(redis, conversation_id_for_title)

        # dual_race bonus：winner 已成功，尝试从同一 image_iter 取第二份；
        # loser 也成功 → 建独立 generation row 显示第二张；loser 失败/超时 → 静默吞掉。
        # 整段用 try/except 兜底——winner 已成功状态不可逆，bonus 任何错误（包括用户
        # 取消、lease 丢失、上游异常）都只 log warn，不让外层 except 把成功改成失败。
        # 取消信号：bonus 阶段尊重用户意图——直接关 iter 退出，不再创建新 generation row。
        if image_iter is not None:
            bonus_pair: tuple[str, str | None] | None = None
            try:
                bonus_pair = await _anext_image_with_guards(
                    image_iter,
                    lease_lost,
                    redis=redis,
                    task_id=task_id,
                )
            except (_LeaseLost, _TaskCancelled, asyncio.CancelledError):
                logger.info(
                    "dual_race bonus iter aborted by cancel/lease task=%s",
                    task_id,
                )
                await _consume_image_iter_close_result(image_iter, task_id=task_id)
                image_iter = None
                bonus_pair = None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "dual_race bonus iter failed task=%s err=%r", task_id, exc
                )
            if bonus_pair is not None:
                bonus_b64, bonus_revised = bonus_pair
                bonus_provider_event = pop_provider_used_event()
                try:
                    await _handle_dual_race_bonus_image(
                        redis=redis,
                        user_id=user_id,
                        channel=channel,
                        parent_task_id=task_id,
                        parent_idempotency_key=gen_idempotency_key,
                        parent_upstream_request=(
                            parent_upstream_request_for_bonus
                            or gen_upstream_request_snapshot
                        ),
                        message_id=message_id,
                        action=str(action),
                        model=gen_model,
                        prompt=prompt,
                        size_requested=size_requested,
                        aspect_ratio=aspect_ratio,
                        input_image_ids=input_image_ids,
                        primary_input_image_id=primary_input_image_id,
                        references=references,
                        image_request_options=image_request_options,
                        b64_result=bonus_b64,
                        revised_prompt=bonus_revised,
                        upstream_provider=bonus_provider_event.get("provider"),
                        upstream_actual_route=bonus_provider_event.get("route"),
                        upstream_actual_source=bonus_provider_event.get("source"),
                        upstream_actual_endpoint=bonus_provider_event.get("endpoint"),
                        settle_billing=True,
                    )
                except (_LeaseLost, _TaskCancelled, asyncio.CancelledError):
                    logger.info(
                        "dual_race bonus finalize aborted by cancel/lease task=%s",
                        task_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dual_race bonus finalize unexpected error task=%s err=%r",
                        task_id,
                        exc,
                    )

    except _LeaseLost as exc:
        logger.warning(
            "generation lease lost task=%s attempt=%s err=%s", task_id, attempt, exc
        )
        if attempt >= _MAX_ATTEMPTS:
            await _mark_generation_attempt_failed(
                redis,
                task_id=task_id,
                message_id=message_id,
                user_id=user_id,
                attempt=attempt,
                error_code="lease_lost_max_attempts",
                error_message="lease lost after max attempts",
                retriable=False,
            )
            _task_outcome = "failed"
            return
        delay = _retry_delay_seconds(attempt)
        requeued = await _mark_generation_attempt_retrying(
            redis,
            task_id=task_id,
            message_id=message_id,
            user_id=user_id,
            attempt=attempt,
            error_code="lease_lost",
            error_message="generation lease lost; task will be retried",
            delay=delay,
            reason="lease_lost",
            max_attempts=_MAX_ATTEMPTS,
        )
        _task_outcome = "retry" if requeued else "lease_lost"
        return

    except _StaleGenerationAttempt as exc:
        logger.info(
            "generation stale attempt task=%s attempt=%s err=%s", task_id, attempt, exc
        )
        requeued = await _maybe_requeue_stale_generation_attempt(
            redis,
            task_id=task_id,
            attempt=attempt,
            reason=type(exc).__name__,
        )
        _task_outcome = "retry" if requeued else "stale_attempt"
        return

    except _TaskCancelled as exc:
        # GEN-P1-4: 用户取消——标 cancelled 终态、publish failed(retriable=false)。
        _task_outcome = await _finalize_running_generation_cancel(
            redis,
            task_id=task_id,
            message_id=message_id,
            user_id=user_id,
            attempt=attempt,
            reason=exc,
        )
        return

    except Exception as exc:  # noqa: BLE001
        decision = _classify_exception(exc, has_partial)
        _byok_terminal, runtime_byok_error = classify_user_credential_error(exc)
        if user_api_credential_id and runtime_byok_error:
            await record_user_credential_runtime_error(user_api_credential_id, exc)
            decision = RetryDecision(False, f"byok {runtime_byok_error}")
        _err_code_log = getattr(exc, "error_code", None) or type(exc).__name__
        _http_status_log = getattr(exc, "status_code", None)
        _provider_log = (getattr(exc, "payload", None) or {}).get("provider", "")
        # Why: warning 级别只放白名单字段，避免 prompt / api_key 等敏感串入日志
        logger.warning(
            "generation failed task=%s attempt=%s retriable=%s reason=%s "
            "error_code=%s http_status=%s provider=%s",
            task_id,
            attempt,
            decision.retriable,
            decision.reason,
            _err_code_log,
            _http_status_log,
            _provider_log,
        )
        logger.debug("generation exc trace task=%s", task_id, exc_info=True)

        err_code = (
            byok_error_to_generation_code(runtime_byok_error)
            if user_api_credential_id and runtime_byok_error
            else "timeout"
            if isinstance(exc, TimeoutError)
            else getattr(exc, "error_code", None) or type(exc).__name__
        )
        err_msg = (
            byok_error_message(runtime_byok_error)
            if user_api_credential_id and runtime_byok_error
            else str(exc)[:2000]
        )
        error_details = _safe_generation_error_details(exc)
        safe_error_summary = _safe_generation_error_summary(
            code=str(err_code) if err_code else None,
            message=err_msg,
            status_code=getattr(exc, "status_code", None),
        )
        error_diagnostics = _build_generation_diagnostics(
            requested_params=requested_params_for_diag,
            provider=(
                None
                if _is_dual_race_sentinel(reserved_provider_name)
                else reserved_provider_name
            ),
            upstream_route=image_route,
            provider_attempts=provider_attempt_log,
            upstream_duration_ms=upstream_duration_ms,
            duration_ms=int(max(0.0, time.monotonic() - _task_start) * 1000),
            debug_id=task_id,
            error_summary=safe_error_summary,
            expose_provider_diagnostics=settings.expose_provider_diagnostics,
        )
        error_upstream_request = dict(gen_upstream_request_snapshot or {})
        error_upstream_request["upstream_route"] = image_route
        error_upstream_request["generation_diagnostics"] = error_diagnostics
        error_upstream_request["requested_params"] = requested_params_for_diag
        error_upstream_request["debug_id"] = task_id
        error_upstream_request["safe_error_summary"] = safe_error_summary
        if provider_attempt_log:
            error_upstream_request["provider_attempts"] = provider_attempt_log[:12]
        if upstream_duration_ms is not None:
            error_upstream_request["upstream_duration_ms"] = upstream_duration_ms
        error_request_event_provider = (
            None
            if _is_dual_race_sentinel(reserved_provider_name)
            else reserved_provider_name
        ) or _request_event_provider_from_attempts(provider_attempt_log)
        if error_request_event_provider:
            error_upstream_request["request_event_provider"] = (
                error_request_event_provider
            )
        else:
            error_upstream_request.pop("request_event_provider", None)
        error_upstream_request = _sanitize_generation_upstream_request(
            error_upstream_request,
            expose_provider_diagnostics=settings.expose_provider_diagnostics,
        )

        moderation_upgrade = False
        moderation_retry_max_attempts = _MAX_ATTEMPTS
        if (
            not decision.retriable
            and not _is_dual_race_sentinel(reserved_provider_name)
            and reserved_provider_name
            and is_moderation_block(getattr(exc, "error_code", None), err_msg)
        ):
            try:
                from ..provider_pool import get_pool as _get_pool

                _pool = await _get_pool()
                _enabled_count = len(_pool.enabled_provider_names())
            except Exception:  # noqa: BLE001
                _enabled_count = 0
            _avoided_now: set[str] = (
                await _get_avoided_providers(redis, task_id)
                if _enabled_count > 1
                else set()
            )
            upgraded = _decide_moderation_retry_upgrade(
                base_decision=decision,
                err_code=getattr(exc, "error_code", None),
                err_msg=err_msg,
                is_dual_race=is_dual_race,
                reserved_provider_name=reserved_provider_name,
                enabled_provider_count=_enabled_count,
                already_avoided_count=len(_avoided_now),
            )
            if upgraded is not None:
                logger.info(
                    "moderation retry upgrade task=%s attempt=%s from_provider=%s "
                    "enabled=%d avoided=%d cap=%d",
                    task_id,
                    attempt,
                    reserved_provider_name,
                    _enabled_count,
                    len(_avoided_now),
                    _MODERATION_RETRY_CAP,
                )
                decision = upgraded
                moderation_upgrade = True
                moderation_retry_max_attempts = max(
                    attempt + 1,
                    min(_MODERATION_RETRY_CAP, max(1, _enabled_count)),
                )

        effective_max_attempts = (
            moderation_retry_max_attempts if moderation_upgrade else _MAX_ATTEMPTS
        )
        _task_outcome = (
            "retry"
            if (decision.retriable and attempt < effective_max_attempts)
            else "failed"
        )

        if decision.retriable and attempt < effective_max_attempts:
            # 把刚刚失败的 provider 加入 avoid set，下次 reserve 跳过它一次。
            # 解决 858 那种"task 锁单 provider，遇到 model_not_found 反复打"的死循环。
            # dual_race 模式下 reserved_provider 是 sentinel，没有真正绑定的 provider，跳过。
            if reserved_provider_name and not _is_dual_race_sentinel(
                reserved_provider_name
            ):
                await _avoid_provider_for_task(redis, task_id, reserved_provider_name)
            # backoff + 重新 enqueue；arq 自己的 retry 机制也可，我们手动更可控
            delay = _retry_delay_seconds(attempt)

            try:
                async with SessionLocal() as session:
                    result = await session.execute(
                        _generation_attempt_update(
                            task_id,
                            attempt,
                            statuses=_RUNNING_GENERATION_STATUSES,
                        ).values(
                            status=GenerationStatus.QUEUED.value,
                            progress_stage=GenerationStage.QUEUED,
                            error_code=err_code,
                            error_message=err_msg,
                            upstream_request=error_upstream_request,
                        )
                    )
                    _ensure_generation_updated(result, task_id, attempt)
                    await session.commit()
            except _StaleGenerationAttempt as stale_exc:
                logger.info(
                    "generation retry stale attempt task=%s attempt=%s err=%s",
                    task_id,
                    attempt,
                    stale_exc,
                )
                _task_outcome = "stale_attempt"
                return

            await _cancel_renewer_task(renewer)
            renewer = None
            await _release_lease(redis, task_id, lease_token)

            # 用 arq redis 延迟入队（_defer_by 秒）
            try:
                await redis.set(
                    _image_queue_not_before_key(task_id),
                    str(time.time() + delay),
                    ex=_retry_not_before_ttl(delay),
                )
                await redis.enqueue_job(
                    "run_generation", task_id, _defer_by=delay, _job_try=attempt + 1
                )
            except Exception as enq_exc:  # noqa: BLE001
                logger.error("re-enqueue failed task=%s err=%s", task_id, enq_exc)
                enqueue_err = "retry_enqueue_failed"
                enqueue_msg = f"failed to enqueue retry: {enq_exc}"
                await _mark_generation_attempt_failed(
                    redis,
                    task_id=task_id,
                    message_id=message_id,
                    user_id=user_id,
                    attempt=attempt,
                    error_code=enqueue_err,
                    error_message=enqueue_msg[:2000],
                    retriable=False,
                    statuses=(
                        GenerationStatus.QUEUED.value,
                        GenerationStatus.RUNNING.value,
                    ),
                )
                _task_outcome = "failed"
                return

            if moderation_upgrade:
                # 通知前端"换号重试中"——复用 provider_failover 通道，前端 DevelopingCard
                # 已经处理 substage=provider_selected + provider_failover=true 的形态，
                # 加 reason=moderation_retry 让 UI 区分于普通 retriable 换号。
                await publish_event(
                    redis,
                    user_id,
                    channel,
                    EV_GEN_PROGRESS,
                    {
                        "generation_id": task_id,
                        "message_id": message_id,
                        "stage": GenerationStage.RENDERING.value,
                        "substage": GenerationStage.PROVIDER_SELECTED.value,
                        "provider_failover": True,
                        "from_provider": reserved_provider_name,
                        "reason": "moderation_retry",
                        "route": "image",
                    },
                )

            await publish_event(
                redis,
                user_id,
                task_channel(task_id),
                EV_GEN_RETRYING,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "attempt": attempt,
                    "max_attempts": effective_max_attempts,
                    "retry_delay_seconds": delay,
                    "error_code": err_code,
                    "error_message": err_msg,
                    **({"error_details": error_details} if error_details else {}),
                },
            )
            return

        # terminal
        try:
            async with SessionLocal() as session:
                result = await session.execute(
                    _generation_attempt_update(
                        task_id,
                        attempt,
                        statuses=_RUNNING_GENERATION_STATUSES,
                    ).values(
                        status=GenerationStatus.FAILED.value,
                        progress_stage=GenerationStage.FINALIZING,
                        finished_at=datetime.now(timezone.utc),
                        error_code=err_code,
                        error_message=err_msg,
                        upstream_request=error_upstream_request,
                    )
                )
                _ensure_generation_updated(result, task_id, attempt)
                msg = await session.get(Message, message_id)
                if msg is not None and msg.status != MessageStatus.CANCELED:
                    msg.status = MessageStatus.FAILED
                gen_failed = await session.get(Generation, task_id)
                if gen_failed is not None:
                    await worker_billing.release_generation(
                        session,
                        gen_failed,
                        reason=err_code,
                    )
                failure_delivery = _stage_generation_failure_event(
                    session,
                    user_id,
                    channel,
                    generation_id=task_id,
                    message_id=message_id,
                    code=err_code,
                    message=err_msg,
                    diagnostics=error_diagnostics,
                    safe_error_summary=safe_error_summary,
                    error_details=error_details,
                )
                await session.commit()
                await worker_billing.flush_balance_cache_refreshes(session)
        except _StaleGenerationAttempt as stale_exc:
            logger.info(
                "generation terminal stale attempt task=%s attempt=%s err=%s",
                task_id,
                attempt,
                stale_exc,
            )
            _task_outcome = "stale_attempt"
            return

        await _deliver_generation_event(redis, failure_delivery)

    finally:
        # image_iter.aclose() 改在 _critical_release_cleanup 内 await（见下方），
        # 用 shield 跑完，避免外层 cancel 时 generator 关闭被推到下一轮 loop——
        # 4K 高负载 + 失败路径会累积到 fd 不释放。
        if renewer is not None:
            await _cancel_renewer_task(renewer)

        # cancel-safe 关键清理：arq 1800s timeout 触发外层 cancel 时，finally 第一个
        # 普通 await 会立刻重抛 CancelledError，导致后续 release 全被跳过，只能靠
        # zset/lease/inflight 各自的 TTL 60~240s 自然过期兜底（漂浮窗：分布式槽位
        # 长达一分钟看起来还在占用）。把"释放外部 redis 资源 / 防状态泄漏"这些
        # 关键 await 打包进一个协程，用 ensure_future + shield 让外层 cancel 时
        # 仍能跑完；shield 抛 CancelledError 时把 cleanup 挂成 done_callback，
        # 最后再重抛 CancelledError 让 arq 知道任务真的被 cancel。
        # task_duration_seconds 是纯 best-effort 指标，无需 shield。
        async def _critical_release_cleanup() -> None:
            # 先关 image_iter——失败路径下生成器还持有 SSE / curl 子进程 fd，
            # 不 await 关掉它，cancel 后会推到下一轮 loop 才回收。
            await _consume_image_iter_close_result(image_iter, task_id=task_id)
            await _release_generation_runtime_resources(
                redis,
                task_id=task_id,
                lease_token=lease_token,
                provider_name=reserved_provider_name,
                clear_avoided_providers=_task_outcome != "retry",
            )

        cleanup_future = asyncio.ensure_future(_critical_release_cleanup())
        cancel_during_cleanup = False
        try:
            await asyncio.shield(cleanup_future)
        except asyncio.CancelledError:
            cancel_during_cleanup = True
            cleanup_future.add_done_callback(
                lambda _t: logger.debug(
                    "generation late critical cleanup finished task=%s", task_id
                )
            )

        try:
            _duration = asyncio.get_event_loop().time() - _task_start
            task_duration_seconds.labels(
                kind="generation", outcome=safe_outcome(_task_outcome)
            ).observe(_duration)
        except Exception:  # noqa: BLE001
            pass

        if cancel_during_cleanup:
            raise asyncio.CancelledError()


__all__ = ["run_generation"]
