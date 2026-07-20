"""Video generation worker task compatibility facade."""

from __future__ import annotations

# This module intentionally retains the historical private symbol surface.
# Implementation modules resolve these globals through a late-bound facade so
# monkeypatches against ``app.tasks.video_generation`` keep working.
# ruff: noqa: F401

import asyncio
import errno
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from arq.cron import cron
from sqlalchemy import or_, select

from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    EV_VIDEO_FAILED,
    EV_VIDEO_FETCHING,
    EV_VIDEO_PROGRESS,
    EV_VIDEO_SUBMITTED,
    EV_VIDEO_SUCCEEDED,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import Image, Video, VideoGeneration, new_uuid7
from lumen_core.video_providers import (
    parse_video_provider_config_json,
    select_video_provider,
)

from .. import runtime_settings, video_submit_cache
from ..db import SessionLocal
from ..storage import StorageDiskFullError, storage
from ..video_artifacts import (
    DownloadedVideo,
    InvalidVideoArtifactError,
    ProcessedVideoFile as _ProcessedVideoFile,
    copy_video_file_exclusive_result,
    postprocess_video_bytes as _postprocess_video_bytes,
    postprocess_video_file as _postprocess_video_file,
)
from ..video_billing import resolve_video_billing
from ..video_events import (
    publish_video_event as _publish,
    publish_video_event_after_commit as _publish_after_commit,
    queue_video_event as _queue_video_event,
)
from ..video_provider_slots import (
    MAX_PROVIDER_POLL_DURATION_S as _MAX_PROVIDER_POLL_DURATION_S,
    VIDEO_PROVIDER_SLOT_LOCK_PREFIX as _VIDEO_PROVIDER_SLOT_LOCK_PREFIX,
    VIDEO_PROVIDER_SLOT_PREFIX as _VIDEO_PROVIDER_SLOT_PREFIX,
    VIDEO_PROVIDER_SLOT_STALE_AFTER_S as _VIDEO_PROVIDER_SLOT_STALE_AFTER_S,
    VIDEO_PROVIDER_SLOT_TTL_S as _VIDEO_PROVIDER_SLOT_TTL_S,
    acquire_provider_slot as _acquire_provider_slot,
    provider_submit_concurrency as _provider_submit_concurrency,
    provider_submit_is_exclusive as _provider_submit_is_exclusive,
    release_provider_slot as _release_provider_slot,
    release_slot_lock as _release_slot_lock,
)
from ..video_submit_cache import (
    CachedSubmitResult as _CachedSubmitResult,
    cached_submit_provider_kind as _cached_submit_provider_kind,
    cached_submit_provider_name as _cached_submit_provider_name,
    cached_submit_result as _cached_submit_result,
    load_submit_result as _load_submit_result,
    store_submit_result as _store_submit_result,
)
from ..video_upstream import (
    PollResult,
    SubmitResult,
    VideoProviderAdapter,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
    adapter_for_provider,
)
from .video_generation_parts._facade import bind_video_generation_facade
from .video_generation_parts.contracts import (
    StoredVideo as _StoredVideo,
    VideoLeaseLost as _VideoLeaseLost,
)
from .video_generation_parts.errors import (
    RETRYABLE_VIDEO_ERROR_CODES as _RETRYABLE_VIDEO_ERROR_CODES,
    SUBMIT_RETRY_DELAYS_S as _SUBMIT_RETRY_DELAYS_S,
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
from .video_generation_parts.lifecycle import (
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
from .video_generation_parts.persistence import (
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
from .video_generation_parts.polling import (
    apply_poll_result as _apply_poll_result,
    cancelled_poll_during_finalization as _cancelled_poll_during_finalization,
    continue_running_poll as _continue_running_poll,
    finish_cancelled_after_provider_poll_error as _finish_cancelled_after_provider_poll_error,
    handle_unexpected_poll_exception as _handle_unexpected_poll_exception,
    handle_video_upstream_poll_error as _handle_video_upstream_poll_error,
    invalid_video_artifact_poll as _invalid_video_artifact_poll,
    run_video_poll,
    schedule_poll_retry as _schedule_poll_retry,
    try_provider_cancel as _try_provider_cancel,
)
from .video_generation_parts.providers import (
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
from .video_generation_parts.reconciliation import (
    finalize_submit_unknown as _finalize_submit_unknown,
    reconcile_submit_unknown as _reconcile_submit_unknown,
    reconcile_video_tasks,
)
from .video_generation_parts.submission import (
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
_TERMINAL_STATUSES = {
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
}
_NON_RESUBMIT_STATUSES = {
    *_TERMINAL_STATUSES,
    VideoGenerationStatus.SUBMIT_UNKNOWN.value,
}


def _video_generation_facade_globals() -> dict[str, Any]:
    return globals()


bind_video_generation_facade(_video_generation_facade_globals)

cron_jobs = [
    cron(reconcile_video_tasks, second={15, 45}, run_at_startup=False),
]

__all__ = [
    "cron_jobs",
    "reconcile_video_tasks",
    "run_video_generation",
    "run_video_poll",
]
