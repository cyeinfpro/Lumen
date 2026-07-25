from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...task_runtime import RuntimeSlot


@dataclass(frozen=True, slots=True)
class GenerationPorts:
    Conversation: Any
    DEFAULT_IMAGE_RESPONSES_MODEL: Any
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST: Any
    EC: Any
    EV_GEN_ATTACHED: Any
    EV_GEN_FAILED: Any
    EV_GEN_PROGRESS: Any
    EV_GEN_QUEUED: Any
    EV_GEN_RETRYING: Any
    EV_GEN_STARTED: Any
    EV_GEN_SUCCEEDED: Any
    Generation: Any
    GenerationAction: Any
    GenerationStage: Any
    GenerationStatus: Any
    Image: Any
    ImageSource: Any
    ImageVariant: Any
    Message: Any
    MessageStatus: Any
    RETRY_BACKOFF_SECONDS: Any
    RetryDecision: Any
    SessionLocal: Any
    StorageDiskFullError: Any
    UpstreamError: Any
    _ACQUIRE_LUA: Any
    _DUAL_RACE_SENTINEL_PREFIX: Any
    _IMAGE_BACKGROUND_VALUES: Any
    _IMAGE_GENERATION_CONCURRENCY_SETTING: Any
    _IMAGE_INFLIGHT_PREFIX: Any
    _IMAGE_MODERATION_VALUES: Any
    _IMAGE_OUTPUT_FORMAT_VALUES: Any
    _IMAGE_PROVIDER_UNAVAILABLE_RETRY_S: Any
    _IMAGE_QUEUE_ACTIVE_KEY: Any
    _IMAGE_QUEUE_AVOID_PREFIX: Any
    _IMAGE_QUEUE_AVOID_TTL_S: Any
    _IMAGE_QUEUE_DEFAULT_LANE: Any
    _IMAGE_QUEUE_ENQUEUE_DEDUPE_PREFIX: Any
    _IMAGE_QUEUE_ENQUEUE_DEDUPE_TTL_S: Any
    _IMAGE_QUEUE_FAIR_SCAN_LIMIT: Any
    _IMAGE_QUEUE_LANE_CURSOR_KEY: Any
    _IMAGE_QUEUE_LANE_RANK: Any
    _IMAGE_QUEUE_LANE_WEIGHTS: Any
    _IMAGE_QUEUE_LOCK_KEY: Any
    _IMAGE_QUEUE_LOCK_TTL_S: Any
    _IMAGE_QUEUE_LOCK_WAIT_S: Any
    _IMAGE_QUEUE_NOT_BEFORE_GRACE_S: Any
    _IMAGE_QUEUE_NOT_BEFORE_PREFIX: Any
    _IMAGE_QUEUE_PROVIDER_LOCK_PREFIX: Any
    _IMAGE_QUEUE_REDIS_ERROR_COOLDOWN_S: Any
    _IMAGE_QUEUE_SCAN_LIMIT: Any
    _IMAGE_QUEUE_TASK_PROVIDER_PREFIX: Any
    _IMAGE_RENDER_QUALITY_VALUES: Any
    _IMAGE_SEMAPHORE_KEY_TTL_S: Any
    _LEASE_REACQUIRED_SUBSTAGE: Any
    _LEASE_RENEW_S: Any
    _LEASE_TTL_S: Any
    _LeaseLost: Any
    _MAX_ATTEMPTS: Any
    _PROVIDER_COOLDOWN_LOCAL: Any
    _QueuedGenerationCandidate: Any
    _RELEASE_LEASE_LUA: Any
    _RELEASE_LUA: Any
    _RENEW_LEASE_LUA: Any
    _RESERVE_IMAGE_SLOT_LUA: Any
    _RETRY_BACKOFF_MAX_SECONDS: Any
    _RUNNING_GENERATION_STATUSES: Any
    _RUN_GENERATION_TIMEOUT_S: Any
    _StageTimer: Any
    _StaleGenerationAttempt: Any
    _TaskCancelled: Any
    _acquire_lease: Any
    _active_image_provider_names: Any
    _anext_image_with_guards: Any
    _aspect_ratio_prompt_constraint: Any
    _await_with_lease_guard: Any
    _base_retry_backoff_seconds: Any
    _bounded_next_attempt: Any
    _cancel_renewer_task: Any
    _clean_model_style_tags: Any
    _cleanup_image_queue_active: Any
    _cleanup_storage_on_error: Any
    _clear_avoided_providers: Any
    _clear_image_queue_enqueue_dedupe: Any
    _coerce_image_queue_capacity: Any
    _consume_image_iter_close_result: Any
    _decode_upstream_image_b64: Any
    _delete_storage_keys: Any
    _deliver_generation_event: Any
    _deliver_generation_events: Any
    _dual_race_sentinel_name: Any
    _enqueue_generation_once: Any
    _ensure_generation_conversation_alive: Any
    _ensure_generation_updated: Any
    _fallback_queued_candidate: Any
    _find_existing_generated_image: Any
    _generation_attempt_update: Any
    _generation_trace_id: Any
    _get_avoided_providers: Any
    _image_endpoint_kind_for_engine: Any
    _image_inflight_key: Any
    _image_provider_active_key: Any
    _image_provider_lock_key: Any
    _image_queue_avoid_key: Any
    _image_queue_capacity: Any
    _image_queue_enqueue_dedupe_key: Any
    _image_queue_lock: Any
    _image_queue_not_before_key: Any
    _image_request_options: Any
    _image_requested_count: Any
    _image_requested_params_snapshot: Any
    _image_task_provider_key: Any
    _inflight_clear: Any
    _inflight_set_fields: Any
    _inpaint_size_from_reference: Any
    _is_cancelled: Any
    _is_dual_race_sentinel: Any
    _kick_image_queue: Any
    _lease_renewer: Any
    _load_mask_image: Any
    _load_reference_images: Any
    _mark_generation_attempt_failed: Any
    _maybe_embed_model_image_metadata_bytes: Any
    _maybe_record_model_library_candidate_image: Any
    _model_image_metadata_from_request: Any
    _parse_aspect_ratio_value: Any
    _parse_size_string: Any
    _postprocess_raw_generated_image: Any
    _primary_input_image_id_valid: Any
    _prompt_with_aspect_ratio_constraint: Any
    _provider_active_count: Any
    _queue_lane_sort_key: Any
    _queue_lane_weight: Any
    _queue_wait_ms: Any
    _queued_candidate_from_mapping: Any
    _queued_generation_candidates: Any
    _queued_generation_ids: Any
    _ready_queued_generation_ids: Any
    _redis_text: Any
    _reference_pixel_size: Any
    _release_generation_runtime_resources: Any
    _release_image_queue_slot: Any
    _release_lease: Any
    _request_compression: Any
    _request_option: Any
    _request_render_quality: Any
    _request_responses_model: Any
    _reserve_image_queue_slot: Any
    _resize_mask_to_reference: Any
    _resolve_image_primary_route: Any
    _resolve_image_queue_capacity: Any
    _retry_not_before_ttl: Any
    _sanitize_transparent_qc_payload: Any
    _select_ready_generation_ids_by_lane: Any
    _settle_existing_generated_image: Any
    _sha256: Any
    _stage_generation_event: Any
    _tracer: Any
    _validate_resolved_size: Any
    _weighted_queue_lane_slots: Any
    _write_generation_files: Any
    build_model_image_metadata: Any
    byok_error_message: Any
    byok_error_to_generation_code: Any
    classify_user_credential_error: Any
    edit_image: Any
    generate_image: Any
    generation_queue_metadata: Any
    httpx: Any
    is_generation_terminal: Any
    is_moderation_block: Any
    is_retriable: Any
    logger: Any
    merge_queue_metadata: Any
    model_image_filename: Any
    new_uuid7: Any
    parse_provider_bool: Any
    pop_image_quota_context: Any
    pop_image_retry_attempt: Any
    pop_image_trace_id: Any
    publish_event: Any
    push_image_quota_context: Any
    push_image_retry_attempt: Any
    push_image_trace_id: Any
    record_user_credential_runtime_error: Any
    resolve_size: Any
    resolve_user_credential_runtime: Any
    runtime_settings: Any
    safe_outcome: Any
    save_image_with_model_metadata: Any
    select: Any
    settings: Any
    storage: Any
    task_channel: Any
    task_duration_seconds: Any
    time: Any
    update: Any
    upstream_calls_total: Any
    validate_explicit_size: Any
    worker_billing: Any


_GENERATION_PORTS: RuntimeSlot[GenerationPorts] = RuntimeSlot("generation-ports")


def install_generation_ports(ports: GenerationPorts) -> None:
    _GENERATION_PORTS.install_default(ports)


def generation_ports() -> GenerationPorts:
    return _GENERATION_PORTS.current()


@dataclass(frozen=True, slots=True)
class GenerationRuntime:
    ports: GenerationPorts
    runner: Any

    async def run(self, ctx: dict[str, Any], task_id: str) -> None:
        with _GENERATION_PORTS.use(self.ports):
            await self.runner(ctx, task_id)


@dataclass(slots=True)
class GenerationRunState:
    """Mutable state shared by the generation runner phases."""

    ctx: dict[str, Any]
    task_id: str
    redis: Any
    worker_id: str
    lease_token: str
    task_start: float
    task_deadline: float
    channel: str
    trace_id: str
    stage_timer: Any
    task_outcome: str = "unknown"
    attempt: int = 0
    renewer: asyncio.Task[None] | None = None
    lease_lost: asyncio.Event = field(default_factory=asyncio.Event)
    reserved_provider: Any | None = None
    reserved_provider_name: str | None = None
    user_api_credential_id: str | None = None
    user_runtime_provider: Any | None = None
    loaded_attempt: int = 0
    queue_metadata_payload: dict[str, Any] = field(default_factory=dict)
    route_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    gen_created_at: datetime | None = None

    generation: Any | None = None
    user_id: str = ""
    message_id: str = ""
    action: Any = None
    prompt: str = ""
    aspect_ratio: str = ""
    size_requested: str | None = None
    input_image_ids: list[str] = field(default_factory=list)
    primary_input_image_id: str | None = None
    mask_image_id: str | None = None
    gen_idempotency_key: str | None = None
    gen_model: str | None = None
    gen_upstream_request_snapshot: dict[str, Any] | None = None
    image_request_options: dict[str, Any] = field(default_factory=dict)

    raw_image_route: str = "responses"
    image_route: str = "responses"
    requires_mask_provider: bool = False
    is_dual_race: bool = False
    endpoint_kind: str | None = None
    upstream_provider_label: str | None = None
    lease_reacquired: bool = False

    has_partial: bool = False
    image_iter: AsyncIterator[tuple[str, str | None]] | None = None
    provider_attempt_log: list[dict[str, Any]] = field(default_factory=list)
    upstream_duration_ms: int | None = None
    requested_image_count: int = 1
    batch_extra_pairs: list[tuple[int, tuple[str, str | None]]] = field(
        default_factory=list
    )
    requested_params_for_diag: dict[str, Any] = field(default_factory=dict)

    resolved: Any | None = None
    references: list[tuple[str, bytes]] = field(default_factory=list)
    ref_for_body: list[tuple[str, bytes]] = field(default_factory=list)
    mask_bytes: bytes | None = None
    inpaint_size_override: str | None = None
    prompt_for_upstream: str = ""
    progress_publisher: Any | None = None

    b64_result: str | None = None
    revised_prompt: str | None = None
    actual_upstream_provider: str | None = None
    actual_upstream_route: str | None = None
    actual_upstream_source: str | None = None
    actual_upstream_endpoint: str | None = None
    image_job_meta: dict[str, Any] = field(default_factory=dict)
    provider_used_events: list[dict[str, str]] = field(default_factory=list)

    conversation_id_for_title: str | None = None
    parent_upstream_request_for_bonus: dict[str, Any] | None = None
    resource_lease: Any | None = None
