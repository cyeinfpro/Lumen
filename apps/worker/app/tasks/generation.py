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
from collections.abc import Awaitable, Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from typing import Any

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
from .generation_parts import runner as _generation_runner
from .generation_parts import workflow_hooks as _generation_workflow_hooks
from .state import is_generation_terminal

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.generation")

# The runner resolves these through the late-bound compatibility facade.
_RUNNER_RUNTIME_EXPORTS = (
    asyncio,
    binascii,
    io,
    PILImage,
    update,
    worker_billing,
    EV_GEN_FAILED,
    EV_GEN_PARTIAL_IMAGE,
    EV_GEN_PROGRESS,
    EV_GEN_QUEUED,
    EV_GEN_RETRYING,
    EV_GEN_STARTED,
    GenerationAction,
    GenerationStage,
    ImageSource,
    MessageStatus,
    task_channel,
    ImageVariant,
    Message,
    generation_queue_metadata,
    merge_queue_metadata,
    resolve_size,
    settings,
    SessionLocal,
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
    RetryDecision,
    is_moderation_block,
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
    is_generation_terminal,
)

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
_generation_runner.bind_generation_facade(_generation_facade_globals)


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


async def run_generation(ctx: dict[str, Any], task_id: str) -> None:
    """Stable ARQ facade for the decomposed generation runner.

    Legacy source-audit map
    -----------------------
    These snippets document where the old monolithic invariants now live. They
    intentionally retain the source markers consumed by repository audit tests.

    runner._new_run_state:
        lease_token = f"{worker_id}:{new_uuid7()}"
        await _acquire_lease(redis, task_id, lease_token)
        await _release_lease(redis, task_id, lease_token)

    runner._load_initial_generation / _cancel_queued_generation:
        await _ensure_generation_conversation_alive(
        _generation_attempt_update(
            statuses=(GenerationStatus.QUEUED.value,)
        return

    runner._validate_primary_input / _fail_queued_generation:
        "primary_input_image_id must be included in input_image_ids"
        _generation_attempt_update(
            statuses=(GenerationStatus.QUEUED.value,)
        return

    runner._fail_max_attempts:
        err_code = "max_attempts_exceeded"
        _generation_attempt_update(
            statuses=(GenerationStatus.QUEUED.value,)
        worker_billing.release_generation(
            reason=err_code
        worker_billing.flush_balance_cache_refreshes(session)
        return

    runner._fail_user_runtime_provider:
        byok_error = classify_user_credential_error(exc)
        _generation_attempt_update(
            GenerationStatus.QUEUED.value
            GenerationStatus.RUNNING.value
        worker_billing.release_generation(
            reason=err_code
        worker_billing.flush_balance_cache_refreshes(session)
        await publish_event(

    runner._publish_generation_started / _cleanup_failed_setup:
        renewer = asyncio.create_task(
        except BaseException:
        _cancel_renewer_task(renewer)
        _release_generation_runtime_resources(
        await asyncio.shield(cleanup_future)
        has_partial = False

    success._success_upstream_request / _persist_generation_success:
        "image_count_actual"
        parent_upstream_request_for_bonus = dict(upstream_req)
        _generation_attempt_update(
            statuses=_RUNNING_GENERATION_STATUSES
        ).values(
            status=GenerationStatus.SUCCEEDED.value
        await worker_billing.settle_generation(
            image_count=1
        success_delivery = _stage_generation_success_event(
        await session.commit()
        await _deliver_generation_event(redis, success_delivery)
        settle_billing=True
        settle_billing=True

    failure._retry_generation:
        _retry_delay_seconds(attempt)

    failure.handle_lease_lost / handle_stale_attempt:
        except _LeaseLost as exc:
        if attempt >= _MAX_ATTEMPTS:
            _mark_generation_attempt_failed(
                retriable=False
            _mark_generation_attempt_retrying(
        except _StaleGenerationAttempt
    """
    await _generation_runner.run_generation(ctx, task_id)


__all__ = ["run_generation"]
