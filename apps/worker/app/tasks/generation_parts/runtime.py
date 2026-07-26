from __future__ import annotations

import asyncio
from contextlib import ExitStack
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ...task_runtime import RuntimeSlot


@dataclass(frozen=True, slots=True)
class GenerationDomainPorts:
    EC: Any
    GenerationAction: Any
    GenerationStage: Any
    GenerationStatus: Any
    ImageSource: Any
    MessageStatus: Any
    RetryDecision: Any
    RETRY_BACKOFF_SECONDS: Any
    _MAX_ATTEMPTS: Any
    _RETRY_BACKOFF_MAX_SECONDS: Any
    _RUNNING_GENERATION_STATUSES: Any
    _RUN_GENERATION_TIMEOUT_S: Any
    _StageTimer: Any
    _StaleGenerationAttempt: Any
    _base_retry_backoff_seconds: Any
    _bounded_next_attempt: Any
    _retry_delay_seconds: Any
    _retry_not_before_ttl: Any
    is_generation_terminal: Any
    new_uuid7: Any
    time: Any


@dataclass(frozen=True, slots=True)
class GenerationPersistencePorts:
    Conversation: Any
    Generation: Any
    Image: Any
    ImageVariant: Any
    Message: Any
    SessionLocal: Any
    StorageDiskFullError: Any
    _clean_model_style_tags: Any
    _cleanup_storage_on_error: Any
    _decode_upstream_image_b64: Any
    _delete_storage_keys: Any
    _ensure_generation_attempt_current: Any
    _ensure_generation_conversation_alive: Any
    _ensure_generation_updated: Any
    _finalize_running_generation_cancel: Any
    _find_existing_generated_image: Any
    _generation_attempt_update: Any
    _handle_dual_race_bonus_image: Any
    _mark_generation_attempt_failed: Any
    _mark_generation_attempt_retrying: Any
    _maybe_embed_model_image_metadata_bytes: Any
    _maybe_record_model_library_generate_image: Any
    _maybe_record_model_library_candidate_image: Any
    _maybe_record_poster_style_library_generate_image: Any
    _maybe_record_poster_workflow_image: Any
    _maybe_requeue_stale_generation_attempt: Any
    _model_image_metadata_from_request: Any
    _postprocess_raw_generated_image: Any
    _settle_existing_generated_image: Any
    _sha256: Any
    _write_generation_files: Any
    build_model_image_metadata: Any
    model_image_filename: Any
    save_image_with_model_metadata: Any
    select: Any
    storage: Any
    update: Any


@dataclass(frozen=True, slots=True)
class GenerationQueuePorts:
    _DUAL_RACE_SENTINEL_PREFIX: Any
    _IMAGE_GENERATION_CONCURRENCY_SETTING: Any
    _IMAGE_INFLIGHT_PREFIX: Any
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
    _PROVIDER_COOLDOWN_LOCAL: Any
    _QueuedGenerationCandidate: Any
    _active_image_provider_names: Any
    _cleanup_image_queue_active: Any
    _classify_inflight_lane: Any
    _clear_avoided_providers: Any
    _clear_image_queue_enqueue_dedupe: Any
    _coerce_image_queue_capacity: Any
    _dual_race_sentinel_name: Any
    _enqueue_generation_once: Any
    _fallback_queued_candidate: Any
    _get_avoided_providers: Any
    _image_inflight_key: Any
    _image_provider_active_key: Any
    _image_provider_lock_key: Any
    _image_queue_avoid_key: Any
    _image_queue_capacity: Any
    _image_queue_enqueue_dedupe_key: Any
    _image_queue_lock: Any
    _image_queue_not_before_key: Any
    _image_task_provider_key: Any
    _inflight_clear: Any
    _inflight_set_fields: Any
    _is_dual_race_sentinel: Any
    _kick_image_queue: Any
    _provider_active_count: Any
    _queue_lane_sort_key: Any
    _queue_lane_weight: Any
    _queue_wait_ms: Any
    _queued_candidate_from_mapping: Any
    _queued_generation_candidates: Any
    _queued_generation_ids: Any
    _ready_queued_generation_ids: Any
    _redis_text: Any
    _resolve_image_queue_capacity: Any
    _select_ready_generation_ids_by_lane: Any
    _weighted_queue_lane_slots: Any
    _avoid_provider_for_task: Any
    generation_queue_metadata: Any
    merge_queue_metadata: Any
    runtime_settings: Any
    settings: Any


@dataclass(frozen=True, slots=True)
class GenerationBillingPorts:
    byok_error_message: Any
    byok_error_to_generation_code: Any
    classify_user_credential_error: Any
    record_user_credential_runtime_error: Any
    resolve_user_credential_runtime: Any
    worker_billing: Any


@dataclass(frozen=True, slots=True)
class GenerationEventPorts:
    EV_GEN_ATTACHED: Any
    EV_GEN_FAILED: Any
    EV_GEN_PARTIAL_IMAGE: Any
    EV_GEN_PROGRESS: Any
    EV_GEN_QUEUED: Any
    EV_GEN_RETRYING: Any
    EV_GEN_STARTED: Any
    EV_GEN_SUCCEEDED: Any
    _deliver_generation_event: Any
    _deliver_generation_events: Any
    _build_generation_diagnostics: Any
    _compact_image_payload_meta: Any
    _generation_trace_id: Any
    _provider_attempt_from_progress: Any
    _request_event_provider_from_attempts: Any
    _sanitize_provider_progress_payload: Any
    _stage_generation_failure_event: Any
    _stage_generation_event: Any
    _stage_generation_success_event: Any
    _tracer: Any
    logger: Any
    publish_event: Any
    safe_outcome: Any
    task_channel: Any
    task_duration_seconds: Any
    upstream_calls_total: Any


@dataclass(frozen=True, slots=True)
class GenerationProviderPorts:
    DEFAULT_IMAGE_RESPONSES_MODEL: Any
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST: Any
    PILImage: Any
    UpstreamError: Any
    binascii: Any
    io: Any
    _MODERATION_RETRY_CAP: Any
    _IMAGE_BACKGROUND_VALUES: Any
    _IMAGE_MODERATION_VALUES: Any
    _IMAGE_OUTPUT_FORMAT_VALUES: Any
    _IMAGE_RENDER_QUALITY_VALUES: Any
    _anext_image_with_guards: Any
    _aspect_ratio_prompt_constraint: Any
    _await_with_lease_guard: Any
    _classify_exception: Any
    _consume_image_iter_close_result: Any
    _decide_moderation_retry_upgrade: Any
    _image_effective_params_snapshot: Any
    _image_endpoint_kind_for_engine: Any
    _image_postprocess_runtime: Any
    _image_request_options: Any
    _image_requested_count: Any
    _image_requested_params_snapshot: Any
    _inpaint_size_from_reference: Any
    _load_mask_image: Any
    _load_reference_images: Any
    _parse_aspect_ratio_value: Any
    _parse_size_string: Any
    _primary_input_image_id_valid: Any
    _prompt_with_aspect_ratio_constraint: Any
    _reference_pixel_size: Any
    _request_compression: Any
    _request_option: Any
    _request_render_quality: Any
    _request_responses_model: Any
    _resize_mask_to_reference: Any
    _resolve_image_primary_route: Any
    _safe_generation_error_details: Any
    _safe_generation_error_summary: Any
    _sanitize_generation_upstream_request: Any
    _sanitize_transparent_qc_payload: Any
    _validate_resolved_size: Any
    edit_image: Any
    generate_image: Any
    httpx: Any
    is_moderation_block: Any
    is_retriable: Any
    parse_provider_bool: Any
    pop_image_quota_context: Any
    pop_image_retry_attempt: Any
    pop_image_trace_id: Any
    push_image_quota_context: Any
    push_image_retry_attempt: Any
    push_image_trace_id: Any
    resolve_size: Any
    validate_explicit_size: Any


@dataclass(frozen=True, slots=True)
class GenerationLeasePorts:
    _ACQUIRE_LUA: Any
    _IMAGE_SEMAPHORE_KEY_TTL_S: Any
    _LEASE_REACQUIRED_SUBSTAGE: Any
    _LEASE_RENEW_S: Any
    _LEASE_TTL_S: Any
    _LeaseLost: Any
    _RELEASE_LEASE_LUA: Any
    _RELEASE_LUA: Any
    _RENEW_LEASE_LUA: Any
    _RESERVE_IMAGE_SLOT_LUA: Any
    _TaskCancelled: Any
    _acquire_lease: Any
    _cancel_renewer_task: Any
    _is_cancelled: Any
    _lease_renewer: Any
    _raise_if_generation_interrupted: Any
    _release_generation_runtime_resources: Any
    _release_image_queue_slot: Any
    _release_lease: Any
    _reserve_image_queue_slot: Any


@dataclass(frozen=True, slots=True)
class GenerationPorts:
    domain: GenerationDomainPorts
    persistence: GenerationPersistencePorts
    queue: GenerationQueuePorts
    billing: GenerationBillingPorts
    events: GenerationEventPorts
    provider: GenerationProviderPorts
    lease: GenerationLeasePorts


_GENERATION_PORTS: RuntimeSlot[GenerationPorts] = RuntimeSlot("generation-ports")
_GENERATION_DOMAIN_PORTS: RuntimeSlot[GenerationDomainPorts] = RuntimeSlot(
    "generation-domain-ports"
)
_GENERATION_PERSISTENCE_PORTS: RuntimeSlot[GenerationPersistencePorts] = RuntimeSlot(
    "generation-persistence-ports"
)
_GENERATION_QUEUE_PORTS: RuntimeSlot[GenerationQueuePorts] = RuntimeSlot(
    "generation-queue-ports"
)
_GENERATION_BILLING_PORTS: RuntimeSlot[GenerationBillingPorts] = RuntimeSlot(
    "generation-billing-ports"
)
_GENERATION_EVENTS_PORTS: RuntimeSlot[GenerationEventPorts] = RuntimeSlot(
    "generation-events-ports"
)
_GENERATION_PROVIDER_PORTS: RuntimeSlot[GenerationProviderPorts] = RuntimeSlot(
    "generation-provider-ports"
)
_GENERATION_LEASE_PORTS: RuntimeSlot[GenerationLeasePorts] = RuntimeSlot(
    "generation-lease-ports"
)


def install_generation_ports(ports: GenerationPorts) -> None:
    _GENERATION_PORTS.install_default(ports)
    _GENERATION_DOMAIN_PORTS.install_default(ports.domain)
    _GENERATION_PERSISTENCE_PORTS.install_default(ports.persistence)
    _GENERATION_QUEUE_PORTS.install_default(ports.queue)
    _GENERATION_BILLING_PORTS.install_default(ports.billing)
    _GENERATION_EVENTS_PORTS.install_default(ports.events)
    _GENERATION_PROVIDER_PORTS.install_default(ports.provider)
    _GENERATION_LEASE_PORTS.install_default(ports.lease)


def generation_ports() -> GenerationPorts:
    return _GENERATION_PORTS.current()


def generation_domain_ports() -> GenerationDomainPorts:
    return _GENERATION_DOMAIN_PORTS.current()


def generation_persistence_ports() -> GenerationPersistencePorts:
    return _GENERATION_PERSISTENCE_PORTS.current()


def generation_queue_ports() -> GenerationQueuePorts:
    return _GENERATION_QUEUE_PORTS.current()


def generation_billing_ports() -> GenerationBillingPorts:
    return _GENERATION_BILLING_PORTS.current()


def generation_events_ports() -> GenerationEventPorts:
    return _GENERATION_EVENTS_PORTS.current()


def generation_provider_ports() -> GenerationProviderPorts:
    return _GENERATION_PROVIDER_PORTS.current()


def generation_lease_ports() -> GenerationLeasePorts:
    return _GENERATION_LEASE_PORTS.current()


@dataclass(slots=True)
class ImagePostprocessRuntime:
    executor: Any | None = None

    def reset(self) -> None:
        executor = self.executor
        self.executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True, slots=True)
class GenerationRuntime:
    ports: GenerationPorts
    runner: Any
    postprocess_runtime: ImagePostprocessRuntime = field(
        default_factory=ImagePostprocessRuntime
    )

    async def run(self, ctx: dict[str, Any], task_id: str) -> None:
        with ExitStack() as stack:
            stack.enter_context(_GENERATION_PORTS.use(self.ports))
            stack.enter_context(_GENERATION_DOMAIN_PORTS.use(self.ports.domain))
            stack.enter_context(
                _GENERATION_PERSISTENCE_PORTS.use(self.ports.persistence)
            )
            stack.enter_context(_GENERATION_QUEUE_PORTS.use(self.ports.queue))
            stack.enter_context(_GENERATION_BILLING_PORTS.use(self.ports.billing))
            stack.enter_context(_GENERATION_EVENTS_PORTS.use(self.ports.events))
            stack.enter_context(_GENERATION_PROVIDER_PORTS.use(self.ports.provider))
            stack.enter_context(_GENERATION_LEASE_PORTS.use(self.ports.lease))
            await self.runner(ctx, task_id)

    async def shutdown(self) -> None:
        self.postprocess_runtime.reset()


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
