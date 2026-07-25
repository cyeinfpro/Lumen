from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...task_runtime import RuntimeSlot


@dataclass(frozen=True, slots=True)
class VideoGenerationPorts:
    SessionLocal: Any
    _EXTENDED_POLL_INTERVAL_S: Any
    _INVALID_VIDEO_ARTIFACT_REASON: Any
    _LEASE_RENEW_MAX_TRANSIENT_FAILURES: Any
    _LEASE_RENEW_S: Any
    _LEASE_TTL_S: Any
    _MAX_MISSING_RESULT_URL_POLLS: Any
    _MAX_POLL_COUNT: Any
    _MAX_POLL_DURATION_S: Any
    _MAX_PROVIDER_POLL_DURATION_S: Any
    _MAX_SUBMIT_ATTEMPTS: Any
    _MAX_UNEXPECTED_POLL_ATTEMPTS: Any
    _NON_RESUBMIT_STATUSES: Any
    _POLL_INTERVAL_S: Any
    _POLL_RETRY_DELAY_S: Any
    _RECON_LIMIT: Any
    _RECON_STALE_AFTER_S: Any
    _SUBMIT_UNKNOWN_AFTER_S: Any
    _SUBMIT_UNKNOWN_FINALIZE_AFTER_S: Any
    _TERMINAL_STATUSES: Any
    _VideoLeaseLost: Any
    _acquire_lease: Any
    _acquire_provider_slot: Any
    _append_bounded_history: Any
    _apply_poll_result: Any
    _cached_submit_provider_kind: Any
    _cached_submit_provider_name: Any
    _cached_submit_result: Any
    _cancelled_poll_during_finalization: Any
    _continue_running_poll: Any
    _delete_video_storage_keys: Any
    _enqueue_cached_submit_recovery: Any
    _enqueue_job_id: Any
    _enqueue_poll: Any
    _enqueue_submit: Any
    _error_message: Any
    _exception_log_info: Any
    _fail_before_submit: Any
    _finalize_submit_unknown: Any
    _finish_cancelled_after_provider_poll_error: Any
    _finish_success: Any
    _finish_terminal_failure: Any
    _generation_attempt: Any
    _generation_diagnostics: Any
    _handle_existing_pre_submit_state: Any
    _handle_unexpected_poll_exception: Any
    _handle_video_submit_exception: Any
    _handle_video_upstream_poll_error: Any
    _input_image_bytes: Any
    _input_image_url: Any
    _invalid_video_artifact_poll: Any
    _is_retryable_video_exception: Any
    _lease_active: Any
    _lease_renewer: Any
    _load_submit_result: Any
    _mark_pre_submit_canceled: Any
    _mark_pre_submit_expired: Any
    _mark_submit_unknown: Any
    _now: Any
    _persist_provider_snapshot: Any
    _persist_video_submit_receipt: Any
    _poll_elapsed_s: Any
    _poll_window_exhausted: Any
    _postprocess_video_bytes: Any
    _postprocess_video_file: Any
    _provider_binding_error: Any
    _provider_binding_fingerprint: Any
    _provider_config: Any
    _provider_for_generation: Any
    _provider_snapshot: Any
    _provider_submit_concurrency: Any
    _provider_submit_is_exclusive: Any
    _provider_tracking_window_exhausted: Any
    _publish: Any
    _publish_after_commit: Any
    _put_video_storage_bytes: Any
    _queue_video_event: Any
    _raise_if_video_lease_lost: Any
    _reconcile_submit_unknown: Any
    _reference_media_bytes: Any
    _release_lease: Any
    _release_provider_slot: Any
    _renew_lease: Any
    _reserve_video_submit_slot: Any
    _restore_cached_provider_identity: Any
    _restore_pre_submit_after_lease_loss: Any
    _resume_existing_provider_task: Any
    _run_video_generation_with_lease: Any
    _schedule_poll_retry: Any
    _schedule_submit_retry: Any
    _store_downloaded_video_asset: Any
    _store_submit_result: Any
    _store_video_asset: Any
    _submit_failure_billable_hint: Any
    _submit_outcome_unknown: Any
    _submit_retry_delay_s: Any
    _transition_submit_unknown: Any
    _try_provider_cancel: Any
    _video_artifact_keys: Any
    _video_exception_code: Any
    _video_exception_message: Any
    _video_for_generation: Any
    adapter_for_provider: Any
    copy_video_file_exclusive_result: Any
    logger: Any
    new_uuid7: Any
    resolve_video_billing: Any
    runtime_settings: Any
    storage: Any
    time: Any
    worker_flush_balance_cache: Any


_VIDEO_PORTS: RuntimeSlot[VideoGenerationPorts] = RuntimeSlot("video_generation-ports")


def install_video_generation_ports(ports: VideoGenerationPorts) -> None:
    _VIDEO_PORTS.install_default(ports)


def video_ports() -> VideoGenerationPorts:
    return _VIDEO_PORTS.current()


@dataclass(frozen=True, slots=True)
class VideoGenerationRuntime:
    ports: VideoGenerationPorts
    submission: Any
    polling: Any
    reconciliation: Any

    async def run_submission(self, ctx: dict[str, Any], task_id: str) -> None:
        with _VIDEO_PORTS.use(self.ports):
            await self.submission(ctx, task_id)

    async def run_poll(self, ctx: dict[str, Any], task_id: str) -> None:
        with _VIDEO_PORTS.use(self.ports):
            await self.polling(ctx, task_id)

    async def reconcile(self, ctx: dict[str, Any]) -> int:
        with _VIDEO_PORTS.use(self.ports):
            return await self.reconciliation(ctx)
