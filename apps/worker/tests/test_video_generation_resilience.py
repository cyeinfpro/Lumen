from __future__ import annotations

import inspect

import httpx

from app.tasks.video_generation import (
    _MAX_POLL_COUNT,
    _MAX_POLL_DURATION_S,
    _MAX_PROVIDER_POLL_DURATION_S,
    _POLL_INTERVAL_S,
    _is_retryable_video_exception,
    _submit_retry_delay_s,
    _video_exception_code,
    _video_exception_message,
)
from app.tasks import video_generation
from app.video_upstream import VideoUpstreamError


def test_blank_submit_timeout_gets_actionable_error_message() -> None:
    exc = httpx.ReadTimeout("")

    assert _video_exception_code(exc, default="provider_unavailable") == (
        "upstream_timeout"
    )
    assert _video_exception_message(exc, phase="submit") == (
        "video upstream submit failed: upstream_timeout (ReadTimeout)"
    )
    assert _is_retryable_video_exception(exc) is True


def test_retryable_video_upstream_errors_are_transient_only() -> None:
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError("busy", error_code="capacity", status_code=429)
        )
        is True
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError(
                "gateway failed", error_code="provider_error", status_code=502
            )
        )
        is True
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError("bad prompt", error_code="invalid_input", status_code=400)
        )
        is False
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError(
                "not visible yet",
                error_code="upstream_not_ready",
                status_code=404,
            )
        )
        is True
    )
    assert (
        _is_retryable_video_exception(
            VideoUpstreamError("bad response", error_code="bad_response")
        )
        is False
    )


def test_submit_retry_delays_are_bounded() -> None:
    assert [_submit_retry_delay_s(attempt) for attempt in range(1, 6)] == [
        8,
        24,
        60,
        60,
        60,
    ]


def test_video_poll_deadline_continues_polling_submitted_tasks() -> None:
    source = inspect.getsource(video_generation.run_video_poll)

    assert "deadline_expired_polling_continues" in source
    assert 'raw={"deadline_expired": True}' not in source
    assert "generation.cancel_requested_at is not None or deadline_expired" not in source
    assert "if generation.cancel_requested_at is not None:" in source
    assert "if deadline_expired:" in source


def test_video_poll_retry_is_bounded_by_poll_window_not_local_deadline() -> None:
    source = inspect.getsource(video_generation._schedule_poll_retry)
    window_source = inspect.getsource(video_generation._poll_window_exhausted)

    assert _MAX_POLL_DURATION_S == 30 * 60
    assert _MAX_POLL_COUNT == _MAX_POLL_DURATION_S // _POLL_INTERVAL_S
    assert "generation.deadline_at <= now and" not in source
    assert "_poll_window_exhausted(generation, now)" in source
    assert "_provider_tracking_window_exhausted(generation, now)" in source
    assert "_EXTENDED_POLL_INTERVAL_S" in source
    assert "generation.poll_count >= _MAX_POLL_COUNT" in window_source
    assert "_MAX_POLL_DURATION_S" in window_source
    assert "deadline_expired_poll_retry_continues" in source
    assert "extended_polling_continues" in source


def test_video_poll_extends_running_provider_tasks_after_local_window() -> None:
    source = inspect.getsource(video_generation._apply_poll_result)
    helper = inspect.getsource(video_generation._continue_running_poll)

    assert "_poll_window_exhausted(generation, now)" in helper
    assert "_provider_tracking_window_exhausted(generation, now)" in source
    assert "_continue_running_poll(session, redis, generation, poll, now=now)" in source
    assert "extended_polling_continues" in helper
    assert "extended_poll_delay_s" in helper
    assert "_EXTENDED_POLL_INTERVAL_S" in helper
    assert "video task exceeded maximum provider tracking window" in source
    assert "poll_timeout" in source
    assert "max_poll_duration_s" in source
    assert "_MAX_PROVIDER_POLL_DURATION_S" in source
    assert _MAX_PROVIDER_POLL_DURATION_S > _MAX_POLL_DURATION_S
    assert "poll_elapsed_s" in source


def test_video_provider_slot_ttl_covers_tracking_window() -> None:
    assert (
        video_generation._VIDEO_PROVIDER_SLOT_STALE_AFTER_S  # noqa: SLF001
        > _MAX_PROVIDER_POLL_DURATION_S
    )
    assert (
        video_generation._VIDEO_PROVIDER_SLOT_TTL_S  # noqa: SLF001
        > video_generation._VIDEO_PROVIDER_SLOT_STALE_AFTER_S  # noqa: SLF001
    )


def test_video_pre_submit_terminal_paths_flush_balance_cache() -> None:
    run_source = inspect.getsource(video_generation.run_video_generation)
    fail_source = inspect.getsource(video_generation._fail_before_submit)

    expired_idx = run_source.index("await _mark_pre_submit_expired")
    expired_commit_idx = run_source.index("await session.commit()", expired_idx)
    expired_flush_idx = run_source.index(
        "await worker_flush_balance_cache(session)",
        expired_commit_idx,
    )
    assert expired_idx < expired_commit_idx < expired_flush_idx

    canceled_idx = run_source.index("await _mark_pre_submit_canceled")
    canceled_commit_idx = run_source.index("await session.commit()", canceled_idx)
    canceled_flush_idx = run_source.index(
        "await worker_flush_balance_cache(session)",
        canceled_commit_idx,
    )
    assert canceled_idx < canceled_commit_idx < canceled_flush_idx

    fail_commit_idx = fail_source.index("await session.commit()")
    fail_flush_idx = fail_source.index(
        "await worker_flush_balance_cache(session)",
        fail_commit_idx,
    )
    assert fail_commit_idx < fail_flush_idx


def test_video_cancel_ack_not_found_finishes_as_canceled() -> None:
    source = inspect.getsource(video_generation.run_video_poll)
    helper = inspect.getsource(video_generation._finish_cancelled_after_provider_poll_error)

    assert "_finish_cancelled_after_provider_poll_error" in source
    assert source.index("_finish_cancelled_after_provider_poll_error") < source.index(
        "_schedule_poll_retry"
    )
    assert 'status="cancelled"' in helper
    assert 'failure_class="canceled"' in helper
    assert "upstream_billable=None" in helper
    assert "upstream_cost_ambiguous" in helper
    assert "cancel_sent_at" in helper


def test_retryable_poll_error_exhaustion_expires_without_billable_signal() -> None:
    source = inspect.getsource(video_generation.run_video_poll)

    assert "retryable_poll_error = _is_retryable_video_exception(exc)" in source
    assert 'status="expired" if retryable_poll_error else "failed"' in source
    assert "upstream_billable=None" in source


def test_reconcile_expires_overdue_tasks_without_provider_task_id() -> None:
    source = inspect.getsource(video_generation.reconcile_video_tasks)

    assert "_mark_pre_submit_expired" in source
    assert "reconcile_deadline_expired_before_submit" in source
