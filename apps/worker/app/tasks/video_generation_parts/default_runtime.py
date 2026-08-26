"""Video generation worker composition and execution support."""

from __future__ import annotations

from .runtime import (
    VideoBillingEventPorts,
    VideoGenerationPorts,
    VideoGenerationRuntime,
    VideoLeaseQueuePorts,
    VideoOperationPorts,
    VideoPolicyPorts,
    VideoProviderPorts,
    VideoStorePorts,
    install_video_generation_ports,
)

# Runtime dependencies are assembled once and passed through explicit ports.

import logging
import time

from lumen_core.constants import (
    VideoGenerationStatus,
)
from lumen_core.models import new_uuid7

from ... import runtime_settings, video_submit_cache
from ...db import SessionLocal
from ...storage import storage
from ...storage_writes import StorageWriteCoordinator
from ...video_artifacts import (
    copy_video_file_exclusive_result,
    postprocess_video_bytes as _postprocess_video_bytes,
    postprocess_video_file as _postprocess_video_file,
)
from ...video_billing import resolve_video_billing
from ...video_events import (
    publish_video_event as _publish,
    publish_video_event_after_commit as _publish_after_commit,
    queue_video_event as _queue_video_event,
)
from ...video_provider_slots import (
    MAX_PROVIDER_POLL_DURATION_S as _MAX_PROVIDER_POLL_DURATION_S,
    acquire_provider_slot as _acquire_provider_slot,
    provider_submit_concurrency as _provider_submit_concurrency,
    provider_submit_is_exclusive as _provider_submit_is_exclusive,
    release_provider_slot as _release_provider_slot,
)
from ...video_submit_cache import (
    cached_submit_provider_kind as _cached_submit_provider_kind,
    cached_submit_provider_name as _cached_submit_provider_name,
    cached_submit_result as _cached_submit_result,
    load_submit_result as _load_submit_result,
    store_submit_result as _store_submit_result,
)
from ...video_upstream_service import (
    adapter_for_provider,
)
from .contracts import (
    VideoLeaseLost as _VideoLeaseLost,
)
from .errors import (
    append_bounded_history as _append_bounded_history,
    exception_log_info as _exception_log_info,
    generation_attempt as _generation_attempt,
    generation_diagnostics as _generation_diagnostics,
    is_retryable_video_exception as _is_retryable_video_exception,
    submit_failure_billable_hint as _submit_failure_billable_hint,
    submit_outcome_unknown as _submit_outcome_unknown,
    submit_retry_delay_s as _submit_retry_delay_s,
    video_exception_code as _video_exception_code,
    video_exception_message as _video_exception_message,
)
from .lifecycle import (
    acquire_lease as _acquire_lease,
    enqueue_cached_submit_recovery as _enqueue_cached_submit_recovery,
    enqueue_job_id as _enqueue_job_id,
    enqueue_poll as _enqueue_poll,
    enqueue_submit as _enqueue_submit,
    lease_active as _lease_active,
    lease_renewer as _lease_renewer,
    now as _now,
    poll_elapsed_s as _poll_elapsed_s,
    poll_window_exhausted as _poll_window_exhausted,
    provider_tracking_window_exhausted as _provider_tracking_window_exhausted,
    raise_if_video_lease_lost as _raise_if_video_lease_lost,
    release_lease as _release_lease,
    renew_lease as _renew_lease,
)
from .persistence import (
    delete_video_storage_keys as _delete_video_storage_keys,
    error_message as _error_message,
    finish_success as _finish_success,
    finish_terminal_failure as _finish_terminal_failure,
    put_video_storage_bytes as _put_video_storage_bytes,
    store_downloaded_video_asset as _store_downloaded_video_asset,
    store_video_asset as _store_video_asset,
    video_artifact_keys as _video_artifact_keys,
    video_for_generation as _video_for_generation,
    worker_flush_balance_cache,
)
from .polling import (
    apply_poll_result as _apply_poll_result,
    continue_running_poll as _continue_running_poll,
    finish_cancelled_after_provider_poll_error as _finish_cancelled_after_provider_poll_error,
    handle_unexpected_poll_exception as _handle_unexpected_poll_exception,
    handle_video_upstream_poll_error as _handle_video_upstream_poll_error,
    run_video_poll,
    schedule_poll_retry as _schedule_poll_retry,
    try_provider_cancel as _try_provider_cancel,
)
from .poll_results import (
    cancelled_poll_during_finalization as _cancelled_poll_during_finalization,
    invalid_video_artifact_poll as _invalid_video_artifact_poll,
)
from .providers import (
    input_image_bytes as _input_image_bytes,
    input_image_url as _input_image_url,
    persist_provider_snapshot as _persist_provider_snapshot,
    provider_binding_error as _provider_binding_error,
    provider_binding_fingerprint as _provider_binding_fingerprint,
    provider_config as _provider_config,
    provider_for_generation as _provider_for_generation,
    provider_snapshot as _provider_snapshot,
    reference_media_bytes as _reference_media_bytes,
)
from .reconciliation import (
    finalize_submit_unknown as _finalize_submit_unknown,
    reconcile_submit_unknown as _reconcile_submit_unknown,
    reconcile_video_tasks,
)
from .submission import (
    fail_before_submit as _fail_before_submit,
    handle_existing_pre_submit_state as _handle_existing_pre_submit_state,
    handle_video_submit_exception as _handle_video_submit_exception,
    mark_pre_submit_canceled as _mark_pre_submit_canceled,
    mark_pre_submit_expired as _mark_pre_submit_expired,
    mark_submit_unknown as _mark_submit_unknown,
    persist_video_submit_receipt as _persist_video_submit_receipt,
    reserve_video_submit_slot as _reserve_video_submit_slot,
    restore_cached_provider_identity as _restore_cached_provider_identity,
    restore_pre_submit_after_lease_loss as _restore_pre_submit_after_lease_loss,
    resume_existing_provider_task as _resume_existing_provider_task,
    run_video_generation,
    run_video_generation_with_lease as _run_video_generation_with_lease,
    schedule_submit_retry as _schedule_submit_retry,
    transition_submit_unknown as _transition_submit_unknown,
)


logger = logging.getLogger(__name__)

_SUBMIT_RESULT_CACHE_TTL_S = video_submit_cache.SUBMIT_RESULT_CACHE_TTL_S
_LEASE_TTL_S = 120
_LEASE_RENEW_S = 30
_LEASE_RENEW_MAX_TRANSIENT_FAILURES = 3
_POLL_INTERVAL_S = 8
_MAX_POLL_DURATION_S = 30 * 60
_MAX_POLL_COUNT = max(1, _MAX_POLL_DURATION_S // _POLL_INTERVAL_S)
_EXTENDED_POLL_INTERVAL_S = 60
_MAX_SUBMIT_ATTEMPTS = 4
_POLL_RETRY_DELAY_S = 12
_MAX_UNEXPECTED_POLL_ATTEMPTS = 4
_MAX_MISSING_RESULT_URL_POLLS = 8
_RECON_STALE_AFTER_S = 30
_SUBMIT_UNKNOWN_AFTER_S = max(_LEASE_TTL_S * 2, 5 * 60)
_SUBMIT_UNKNOWN_FINALIZE_AFTER_S = 60 * 60
_INVALID_VIDEO_ARTIFACT_REASON = "invalid_video_artifact_after_upstream_success"
_RECON_LIMIT = 100
_TERMINAL_STATUSES = frozenset(
    {
        VideoGenerationStatus.SUCCEEDED.value,
        VideoGenerationStatus.FAILED.value,
        VideoGenerationStatus.CANCELED.value,
        VideoGenerationStatus.EXPIRED.value,
    }
)
_NON_RESUBMIT_STATUSES = frozenset(
    {*_TERMINAL_STATUSES, VideoGenerationStatus.SUBMIT_UNKNOWN.value}
)


def build_video_generation_runtime(
    *,
    storage_writes: StorageWriteCoordinator | None = None,
    install_default: bool = False,
) -> VideoGenerationRuntime:
    ports = VideoGenerationPorts(
        policy=VideoPolicyPorts(
            _EXTENDED_POLL_INTERVAL_S=_EXTENDED_POLL_INTERVAL_S,
            _INVALID_VIDEO_ARTIFACT_REASON=_INVALID_VIDEO_ARTIFACT_REASON,
            _LEASE_RENEW_MAX_TRANSIENT_FAILURES=_LEASE_RENEW_MAX_TRANSIENT_FAILURES,
            _LEASE_RENEW_S=_LEASE_RENEW_S,
            _LEASE_TTL_S=_LEASE_TTL_S,
            _MAX_MISSING_RESULT_URL_POLLS=_MAX_MISSING_RESULT_URL_POLLS,
            _MAX_POLL_COUNT=_MAX_POLL_COUNT,
            _MAX_POLL_DURATION_S=_MAX_POLL_DURATION_S,
            _MAX_PROVIDER_POLL_DURATION_S=_MAX_PROVIDER_POLL_DURATION_S,
            _MAX_SUBMIT_ATTEMPTS=_MAX_SUBMIT_ATTEMPTS,
            _MAX_UNEXPECTED_POLL_ATTEMPTS=_MAX_UNEXPECTED_POLL_ATTEMPTS,
            _NON_RESUBMIT_STATUSES=_NON_RESUBMIT_STATUSES,
            _POLL_INTERVAL_S=_POLL_INTERVAL_S,
            _POLL_RETRY_DELAY_S=_POLL_RETRY_DELAY_S,
            _RECON_LIMIT=_RECON_LIMIT,
            _RECON_STALE_AFTER_S=_RECON_STALE_AFTER_S,
            _SUBMIT_UNKNOWN_AFTER_S=_SUBMIT_UNKNOWN_AFTER_S,
            _SUBMIT_UNKNOWN_FINALIZE_AFTER_S=_SUBMIT_UNKNOWN_FINALIZE_AFTER_S,
            _TERMINAL_STATUSES=_TERMINAL_STATUSES,
            _VideoLeaseLost=_VideoLeaseLost,
        ),
        store=VideoStorePorts(
            SessionLocal=SessionLocal,
            _delete_video_storage_keys=_delete_video_storage_keys,
            _postprocess_video_bytes=_postprocess_video_bytes,
            _postprocess_video_file=_postprocess_video_file,
            _put_video_storage_bytes=_put_video_storage_bytes,
            _store_downloaded_video_asset=_store_downloaded_video_asset,
            _video_artifact_keys=_video_artifact_keys,
            _video_for_generation=_video_for_generation,
            copy_video_file_exclusive_result=copy_video_file_exclusive_result,
            new_uuid7=new_uuid7,
            storage=storage,
            storage_writes=storage_writes,
        ),
        lease_queue=VideoLeaseQueuePorts(
            _acquire_lease=_acquire_lease,
            _acquire_provider_slot=_acquire_provider_slot,
            _enqueue_cached_submit_recovery=_enqueue_cached_submit_recovery,
            _enqueue_job_id=_enqueue_job_id,
            _enqueue_poll=_enqueue_poll,
            _enqueue_submit=_enqueue_submit,
            _lease_active=_lease_active,
            _lease_renewer=_lease_renewer,
            _poll_elapsed_s=_poll_elapsed_s,
            _poll_window_exhausted=_poll_window_exhausted,
            _provider_submit_concurrency=_provider_submit_concurrency,
            _provider_submit_is_exclusive=_provider_submit_is_exclusive,
            _provider_tracking_window_exhausted=_provider_tracking_window_exhausted,
            _raise_if_video_lease_lost=_raise_if_video_lease_lost,
            _release_lease=_release_lease,
            _release_provider_slot=_release_provider_slot,
            _renew_lease=_renew_lease,
            _reserve_video_submit_slot=_reserve_video_submit_slot,
            _schedule_poll_retry=_schedule_poll_retry,
            _schedule_submit_retry=_schedule_submit_retry,
        ),
        provider=VideoProviderPorts(
            _cached_submit_provider_kind=_cached_submit_provider_kind,
            _cached_submit_provider_name=_cached_submit_provider_name,
            _cached_submit_result=_cached_submit_result,
            _input_image_bytes=_input_image_bytes,
            _input_image_url=_input_image_url,
            _load_submit_result=_load_submit_result,
            _persist_provider_snapshot=_persist_provider_snapshot,
            _provider_binding_error=_provider_binding_error,
            _provider_binding_fingerprint=_provider_binding_fingerprint,
            _provider_config=_provider_config,
            _provider_for_generation=_provider_for_generation,
            _provider_snapshot=_provider_snapshot,
            _reference_media_bytes=_reference_media_bytes,
            _restore_cached_provider_identity=_restore_cached_provider_identity,
            _store_submit_result=_store_submit_result,
            adapter_for_provider=adapter_for_provider,
            runtime_settings=runtime_settings,
        ),
        billing_events=VideoBillingEventPorts(
            _error_message=_error_message,
            _finish_success=_finish_success,
            _finish_terminal_failure=_finish_terminal_failure,
            _publish=_publish,
            _publish_after_commit=_publish_after_commit,
            _queue_video_event=_queue_video_event,
            resolve_video_billing=resolve_video_billing,
            worker_flush_balance_cache=worker_flush_balance_cache,
        ),
        operations=VideoOperationPorts(
            _append_bounded_history=_append_bounded_history,
            _apply_poll_result=_apply_poll_result,
            _cancelled_poll_during_finalization=_cancelled_poll_during_finalization,
            _continue_running_poll=_continue_running_poll,
            _exception_log_info=_exception_log_info,
            _fail_before_submit=_fail_before_submit,
            _finalize_submit_unknown=_finalize_submit_unknown,
            _finish_cancelled_after_provider_poll_error=_finish_cancelled_after_provider_poll_error,
            _generation_attempt=_generation_attempt,
            _generation_diagnostics=_generation_diagnostics,
            _handle_existing_pre_submit_state=_handle_existing_pre_submit_state,
            _handle_unexpected_poll_exception=_handle_unexpected_poll_exception,
            _handle_video_submit_exception=_handle_video_submit_exception,
            _handle_video_upstream_poll_error=_handle_video_upstream_poll_error,
            _invalid_video_artifact_poll=_invalid_video_artifact_poll,
            _is_retryable_video_exception=_is_retryable_video_exception,
            _mark_pre_submit_canceled=_mark_pre_submit_canceled,
            _mark_pre_submit_expired=_mark_pre_submit_expired,
            _mark_submit_unknown=_mark_submit_unknown,
            _now=_now,
            _persist_video_submit_receipt=_persist_video_submit_receipt,
            _reconcile_submit_unknown=_reconcile_submit_unknown,
            _restore_pre_submit_after_lease_loss=_restore_pre_submit_after_lease_loss,
            _resume_existing_provider_task=_resume_existing_provider_task,
            _run_video_generation_with_lease=_run_video_generation_with_lease,
            _store_video_asset=_store_video_asset,
            _submit_failure_billable_hint=_submit_failure_billable_hint,
            _submit_outcome_unknown=_submit_outcome_unknown,
            _submit_retry_delay_s=_submit_retry_delay_s,
            _transition_submit_unknown=_transition_submit_unknown,
            _try_provider_cancel=_try_provider_cancel,
            _video_exception_code=_video_exception_code,
            _video_exception_message=_video_exception_message,
            logger=logger,
            time=time,
        ),
    )
    if install_default:
        install_video_generation_ports(ports)
    return VideoGenerationRuntime(
        ports=ports,
        submission=run_video_generation,
        polling=run_video_poll,
        reconciliation=reconcile_video_tasks,
    )


DEFAULT_VIDEO_GENERATION_RUNTIME = build_video_generation_runtime(install_default=True)
