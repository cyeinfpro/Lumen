from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import httpx
import pytest

from app.tasks.video_generation import (
    _MAX_POLL_COUNT,
    _MAX_POLL_DURATION_S,
    _MAX_PROVIDER_POLL_DURATION_S,
    _POLL_INTERVAL_S,
    _is_retryable_video_exception,
    _submit_outcome_unknown,
    _submit_retry_delay_s,
    _video_exception_code,
    _video_exception_message,
)
from app.tasks import video_generation
from app.video_upstream import VideoSubmitRequest, VideoUpstreamError, _submit_headers


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
            VideoUpstreamError(
                "bad prompt", error_code="invalid_input", status_code=400
            )
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


@pytest.mark.asyncio
async def test_video_submit_lease_renews_only_for_current_owner() -> None:
    class Redis:
        async def eval(self, *_args: object) -> int:
            return 1

    assert (
        await video_generation._renew_lease(  # noqa: SLF001
            Redis(),
            "video-1",
            "owner-1",
        )
        is True
    )


@pytest.mark.asyncio
async def test_video_submit_lease_renew_transport_failure_is_indeterminate() -> None:
    class Redis:
        async def eval(self, *_args: object) -> int:
            raise RuntimeError("redis unavailable")

    assert (
        await video_generation._renew_lease(  # noqa: SLF001
            Redis(),
            "video-1",
            "owner-1",
        )
        is None
    )


@pytest.mark.asyncio
async def test_video_submit_lease_renewer_tolerates_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = iter([None, None, True, None, None, None])

    async def renew(*_args: object) -> bool | None:
        return next(outcomes)

    monkeypatch.setattr(video_generation, "_renew_lease", renew)
    monkeypatch.setattr(video_generation, "_LEASE_RENEW_S", 0.001)
    stop = asyncio.Event()
    lost = asyncio.Event()

    await video_generation._lease_renewer(  # noqa: SLF001
        object(),
        "video-1",
        "owner-1",
        stop=stop,
        lost=lost,
    )

    assert lost.is_set()


def test_submit_outcome_unknown_excludes_explicit_capacity_rejections() -> None:
    assert _submit_outcome_unknown(httpx.ReadTimeout("timeout")) is True
    assert (
        _submit_outcome_unknown(
            VideoUpstreamError(
                "gateway timeout",
                error_code="provider_error",
                status_code=504,
            )
        )
        is True
    )
    assert (
        _submit_outcome_unknown(
            VideoUpstreamError("busy", error_code="capacity", status_code=429)
        )
        is False
    )


def test_video_submit_uses_persisted_provider_idempotency_key() -> None:
    request = VideoSubmitRequest(
        task_id="video-1",
        user_id="user-1",
        action="t2v",
        model="seedance",
        upstream_model="seedance-upstream",
        prompt="hello",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="video:video-1",
    )

    headers = _submit_headers(request)

    assert headers["Idempotency-Key"] == "video:video-1"
    assert headers["X-Request-ID"] == "video:video-1"
    assert headers["X-Lumen-Task-ID"] == "video-1"


def test_video_submit_caches_receipt_before_post_submit_lease_check() -> None:
    source = inspect.getsource(video_generation._run_video_generation_with_lease)

    submit_idx = source.index("result = await adapter.submit")
    cache_idx = source.index("await _store_submit_result", submit_idx)
    lease_check_idx = source.index(
        '"video submit lease lost after upstream call"',
        submit_idx,
    )

    assert submit_idx < cache_idx < lease_check_idx


def test_video_submit_retry_queues_durable_regression_event_before_commit() -> None:
    source = inspect.getsource(video_generation._schedule_submit_retry)

    queue_idx = source.index("_queue_video_event")
    commit_idx = source.index("await session.commit()")

    assert queue_idx < commit_idx
    assert "retry_transition=True" in source
    assert "await _publish(" not in source


def test_video_poll_deadline_continues_polling_submitted_tasks() -> None:
    source = inspect.getsource(video_generation.run_video_poll)

    assert "deadline_expired_polling_continues" in source
    assert 'raw={"deadline_expired": True}' not in source
    assert (
        "generation.cancel_requested_at is not None or deadline_expired" not in source
    )
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
    state_source = inspect.getsource(video_generation._handle_existing_pre_submit_state)
    fail_source = inspect.getsource(video_generation._fail_before_submit)
    canceled_source = inspect.getsource(video_generation._mark_pre_submit_canceled)
    expired_source = inspect.getsource(video_generation._mark_pre_submit_expired)

    expired_idx = state_source.index("await _mark_pre_submit_expired")
    expired_commit_idx = state_source.index("await session.commit()", expired_idx)
    expired_flush_idx = state_source.index(
        "await worker_flush_balance_cache(session)",
        expired_commit_idx,
    )
    assert expired_idx < expired_commit_idx < expired_flush_idx

    canceled_idx = state_source.index("await _mark_pre_submit_canceled")
    canceled_commit_idx = state_source.index("await session.commit()", canceled_idx)
    canceled_flush_idx = state_source.index(
        "await worker_flush_balance_cache(session)",
        canceled_commit_idx,
    )
    assert canceled_idx < canceled_commit_idx < canceled_flush_idx
    assert "_publish(" not in canceled_source
    assert "_publish(" not in expired_source
    assert "_queue_video_event" in canceled_source
    assert "_queue_video_event" in expired_source

    fail_event_idx = fail_source.index("_queue_video_event")
    fail_commit_idx = fail_source.index("await session.commit()")
    fail_flush_idx = fail_source.index(
        "await worker_flush_balance_cache(session)",
        fail_commit_idx,
    )
    assert fail_event_idx < fail_commit_idx < fail_flush_idx


def test_video_cancel_ack_not_found_finishes_as_canceled() -> None:
    source = inspect.getsource(video_generation.run_video_poll)
    helper = inspect.getsource(
        video_generation._finish_cancelled_after_provider_poll_error
    )

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
    assert "await _lease_active(redis, row.id)" in source
    assert "_enqueue_cached_submit_recovery" in source
    assert "VideoGenerationStatus.SUBMIT_UNKNOWN.value" in source
    assert "_transition_submit_unknown" in source
    unknown_source = inspect.getsource(video_generation._reconcile_submit_unknown)
    assert "_finalize_submit_unknown" in unknown_source
    state_source = inspect.getsource(video_generation._handle_existing_pre_submit_state)
    assert "duplicate_worker_observed_stale_submitting" in state_source


@pytest.mark.asyncio
async def test_non_idempotent_ambiguous_submit_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def mark_unknown(*_args: object, **_kwargs: object) -> bool:
        calls.append("unknown")
        return True

    async def fail_before_submit(*_args: object, **_kwargs: object) -> None:
        calls.append("retry")

    monkeypatch.setattr(video_generation, "_mark_submit_unknown", mark_unknown)
    monkeypatch.setattr(video_generation, "_fail_before_submit", fail_before_submit)

    await video_generation._handle_video_submit_exception(  # noqa: SLF001
        object(),
        "video-1",
        httpx.ReadTimeout("timeout"),
        provider_name="provider-1",
        submission_epoch=2,
        upstream_invoked=True,
        provider_supports_idempotency=False,
    )

    assert calls == ["unknown"]


@pytest.mark.asyncio
async def test_lease_loss_before_upstream_restores_pre_submit_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restored: list[tuple[str, str | None, int | None]] = []

    async def restore(
        _redis: object,
        task_id: str,
        *,
        provider_name: str | None,
        submission_epoch: int | None,
    ) -> None:
        restored.append((task_id, provider_name, submission_epoch))

    monkeypatch.setattr(
        video_generation,
        "_restore_pre_submit_after_lease_loss",
        restore,
    )

    await video_generation._handle_video_submit_exception(  # noqa: SLF001
        object(),
        "video-1",
        video_generation._VideoLeaseLost("lost"),  # noqa: SLF001
        provider_name="provider-1",
        submission_epoch=2,
        upstream_invoked=False,
        provider_supports_idempotency=False,
    )

    assert restored == [("video-1", "provider-1", 2)]


@pytest.mark.asyncio
async def test_cached_submit_recovery_requeues_without_upstream_resubmit() -> None:
    class Redis:
        def __init__(self) -> None:
            self.enqueued: list[tuple[str, str, dict[str, object]]] = []

        async def get(self, _key: str) -> str:
            return json.dumps(
                {
                    "provider_task_id": "provider-task-1",
                    "raw": {"id": "provider-task-1"},
                }
            )

        async def enqueue_job(
            self,
            name: str,
            task_id: str,
            **kwargs: object,
        ) -> None:
            self.enqueued.append((name, task_id, kwargs))

    redis = Redis()

    recovered = await video_generation._enqueue_cached_submit_recovery(  # noqa: SLF001
        redis,
        "video-1",
        defer_s=0,
    )

    assert recovered is True
    assert redis.enqueued[0][0:2] == ("run_video_generation", "video-1")


@pytest.mark.asyncio
async def test_post_commit_publish_failure_does_not_change_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="canceled",
        progress_stage="finished",
        progress_pct=100,
        error_code="canceled",
        error_message="cancelled",
    )

    async def fail_publish(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(video_generation, "_publish", fail_publish)

    await video_generation._publish_after_commit(  # noqa: SLF001
        object(),
        generation,
        "video.canceled",
    )

    assert generation.status == "canceled"
