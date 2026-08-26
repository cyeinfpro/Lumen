from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from ...task_runtime import RuntimeSlot


PortCallable = Callable[..., object]


@dataclass(frozen=True, slots=True)
class VideoPolicyPorts:
    _EXTENDED_POLL_INTERVAL_S: int
    _INVALID_VIDEO_ARTIFACT_REASON: str
    _LEASE_RENEW_MAX_TRANSIENT_FAILURES: int
    _LEASE_RENEW_S: int
    _LEASE_TTL_S: int
    _MAX_MISSING_RESULT_URL_POLLS: int
    _MAX_POLL_COUNT: int
    _MAX_POLL_DURATION_S: int
    _MAX_PROVIDER_POLL_DURATION_S: int
    _MAX_SUBMIT_ATTEMPTS: int
    _MAX_UNEXPECTED_POLL_ATTEMPTS: int
    _NON_RESUBMIT_STATUSES: frozenset[str]
    _POLL_INTERVAL_S: int
    _POLL_RETRY_DELAY_S: int
    _RECON_LIMIT: int
    _RECON_STALE_AFTER_S: int
    _SUBMIT_UNKNOWN_AFTER_S: int
    _SUBMIT_UNKNOWN_FINALIZE_AFTER_S: int
    _TERMINAL_STATUSES: frozenset[str]
    _VideoLeaseLost: type[BaseException]


@dataclass(frozen=True, slots=True)
class VideoStorePorts:
    SessionLocal: PortCallable
    _delete_video_storage_keys: PortCallable
    _postprocess_video_bytes: PortCallable
    _postprocess_video_file: PortCallable
    _put_video_storage_bytes: PortCallable
    _store_downloaded_video_asset: PortCallable
    _video_artifact_keys: PortCallable
    _video_for_generation: PortCallable
    copy_video_file_exclusive_result: PortCallable
    new_uuid7: PortCallable
    storage: object
    storage_writes: object | None


@dataclass(frozen=True, slots=True)
class VideoLeaseQueuePorts:
    _acquire_lease: PortCallable
    _acquire_provider_slot: PortCallable
    _enqueue_cached_submit_recovery: PortCallable
    _enqueue_job_id: PortCallable
    _enqueue_poll: PortCallable
    _enqueue_submit: PortCallable
    _lease_active: PortCallable
    _lease_renewer: PortCallable
    _poll_elapsed_s: PortCallable
    _poll_window_exhausted: PortCallable
    _provider_submit_concurrency: PortCallable
    _provider_submit_is_exclusive: PortCallable
    _provider_tracking_window_exhausted: PortCallable
    _raise_if_video_lease_lost: PortCallable
    _release_lease: PortCallable
    _release_provider_slot: PortCallable
    _renew_lease: PortCallable
    _reserve_video_submit_slot: PortCallable
    _schedule_poll_retry: PortCallable
    _schedule_submit_retry: PortCallable


@dataclass(frozen=True, slots=True)
class VideoProviderPorts:
    _cached_submit_provider_kind: PortCallable
    _cached_submit_provider_name: PortCallable
    _cached_submit_result: PortCallable
    _input_image_bytes: PortCallable
    _input_image_url: PortCallable
    _load_submit_result: PortCallable
    _persist_provider_snapshot: PortCallable
    _provider_binding_error: PortCallable
    _provider_binding_fingerprint: PortCallable
    _provider_config: PortCallable
    _provider_for_generation: PortCallable
    _provider_snapshot: PortCallable
    _reference_media_bytes: PortCallable
    _restore_cached_provider_identity: PortCallable
    _store_submit_result: PortCallable
    adapter_for_provider: PortCallable
    runtime_settings: object


@dataclass(frozen=True, slots=True)
class VideoBillingEventPorts:
    _error_message: PortCallable
    _finish_success: PortCallable
    _finish_terminal_failure: PortCallable
    _publish: PortCallable
    _publish_after_commit: PortCallable
    _queue_video_event: PortCallable
    resolve_video_billing: PortCallable
    worker_flush_balance_cache: PortCallable


@dataclass(frozen=True, slots=True)
class VideoOperationPorts:
    _append_bounded_history: PortCallable
    _apply_poll_result: PortCallable
    _cancelled_poll_during_finalization: PortCallable
    _continue_running_poll: PortCallable
    _exception_log_info: PortCallable
    _fail_before_submit: PortCallable
    _finalize_submit_unknown: PortCallable
    _finish_cancelled_after_provider_poll_error: PortCallable
    _generation_attempt: PortCallable
    _generation_diagnostics: PortCallable
    _handle_existing_pre_submit_state: PortCallable
    _handle_unexpected_poll_exception: PortCallable
    _handle_video_submit_exception: PortCallable
    _handle_video_upstream_poll_error: PortCallable
    _invalid_video_artifact_poll: PortCallable
    _is_retryable_video_exception: PortCallable
    _mark_pre_submit_canceled: PortCallable
    _mark_pre_submit_expired: PortCallable
    _mark_submit_unknown: PortCallable
    _now: PortCallable
    _persist_video_submit_receipt: PortCallable
    _reconcile_submit_unknown: PortCallable
    _restore_pre_submit_after_lease_loss: PortCallable
    _resume_existing_provider_task: PortCallable
    _run_video_generation_with_lease: PortCallable
    _store_video_asset: PortCallable
    _submit_failure_billable_hint: PortCallable
    _submit_outcome_unknown: PortCallable
    _submit_retry_delay_s: PortCallable
    _transition_submit_unknown: PortCallable
    _try_provider_cancel: PortCallable
    _video_exception_code: PortCallable
    _video_exception_message: PortCallable
    logger: object
    time: object


@dataclass(frozen=True, slots=True)
class VideoGenerationPorts:
    policy: VideoPolicyPorts
    store: VideoStorePorts
    lease_queue: VideoLeaseQueuePorts
    provider: VideoProviderPorts
    billing_events: VideoBillingEventPorts
    operations: VideoOperationPorts


_VIDEO_PORTS: RuntimeSlot[VideoGenerationPorts] = RuntimeSlot("video_generation-ports")


def install_video_generation_ports(ports: VideoGenerationPorts) -> None:
    _VIDEO_PORTS.install_default(ports)


def video_ports() -> VideoGenerationPorts:
    return _VIDEO_PORTS.current()


class VideoTaskRunner(Protocol):
    def __call__(self, ctx: dict[str, Any], task_id: str) -> Awaitable[None]: ...


class VideoReconciliationRunner(Protocol):
    def __call__(self, ctx: dict[str, Any]) -> Awaitable[int]: ...


@dataclass(frozen=True, slots=True)
class VideoGenerationRuntime:
    ports: VideoGenerationPorts
    submission: VideoTaskRunner
    polling: VideoTaskRunner
    reconciliation: VideoReconciliationRunner

    async def run_submission(self, ctx: dict[str, Any], task_id: str) -> None:
        with _VIDEO_PORTS.use(self.ports):
            await self.submission(ctx, task_id)

    async def run_poll(self, ctx: dict[str, Any], task_id: str) -> None:
        with _VIDEO_PORTS.use(self.ports):
            await self.polling(ctx, task_id)

    async def reconcile(self, ctx: dict[str, Any]) -> int:
        with _VIDEO_PORTS.use(self.ports):
            return await self.reconciliation(ctx)
