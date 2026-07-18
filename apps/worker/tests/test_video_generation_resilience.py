from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lumen_core.models import OutboxEvent

from app import video_artifacts
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
from app.video_upstream import (
    PollResult,
    VideoSubmitRequest,
    VideoUpstreamError,
    _submit_headers,
)


@pytest.mark.parametrize("streams", [None, {}, "invalid"])
def test_probe_video_rejects_payloads_without_video_stream(
    monkeypatch: pytest.MonkeyPatch,
    streams: object,
) -> None:
    monkeypatch.setattr(
        video_artifacts.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": streams, "format": None}).encode(),
            stderr=b"",
        ),
    )

    with pytest.raises(
        video_artifacts.InvalidVideoArtifactError,
        match="no video stream",
    ):
        video_artifacts.probe_video("ffprobe", Path("ignored.mp4"))


def test_probe_video_rejects_ffprobe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        video_artifacts.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b"invalid data",
        ),
    )

    with pytest.raises(
        video_artifacts.InvalidVideoArtifactError,
        match="ffprobe rejected",
    ):
        video_artifacts.probe_video("ffprobe", Path("ignored.mp4"))


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


def test_invalid_video_artifact_poll_preserves_upstream_billing_evidence() -> None:
    poll = video_generation._invalid_video_artifact_poll(  # noqa: SLF001
        PollResult(
            status="succeeded",
            usage_total_tokens=42,
            upstream_billable=True,
            raw={"provider_state": "succeeded"},
        ),
        video_artifacts.InvalidVideoArtifactError(
            "no video stream",
            diagnostics={"probe_error": "no video stream"},
        ),
    )

    assert poll.status == "failed"
    assert poll.failure_class == "invalid_video_artifact"
    assert poll.usage_total_tokens == 42
    assert poll.upstream_billable is True
    assert poll.raw["reason"] == "invalid_video_artifact_after_upstream_success"
    assert poll.raw["phase"] == "artifact_validation"
    assert poll.raw["provider_status"] == "succeeded"


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


def test_cached_submit_receipt_rejects_provider_identity_mismatch() -> None:
    generation = SimpleNamespace(
        provider_name="provider-a",
        provider_kind="volcano",
        provider_task_id=None,
        upstream_request={
            "provider_snapshot": {
                "provider_name": "provider-a",
                "provider_kind": "volcano",
                "base_url": "https://provider-a.example",
            }
        },
    )
    cached = SimpleNamespace(
        provider_name="provider-b",
        provider_kind="volcano",
        provider_task_id="upstream-1",
        raw={"id": "upstream-1"},
    )

    with pytest.raises(VideoUpstreamError) as excinfo:
        video_generation._restore_cached_provider_identity(  # noqa: SLF001
            generation,
            cached,
        )

    assert excinfo.value.error_code == "provider_snapshot_unavailable"
    assert generation.provider_name == "provider-a"


@pytest.mark.asyncio
async def test_submitted_task_rejects_provider_endpoint_snapshot_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SimpleNamespace(
        name="provider-a",
        kind="volcano",
        base_url="https://replacement.example",
    )
    generation = SimpleNamespace(
        provider_name="provider-a",
        provider_kind="volcano",
        provider_task_id="upstream-1",
        upstream_request={
            "provider_snapshot": {
                "provider_name": "provider-a",
                "provider_kind": "volcano",
                "base_url": "https://original.example",
            }
        },
        model="seedance",
        action="t2v",
    )

    async def provider_config() -> list[SimpleNamespace]:
        return [provider]

    monkeypatch.setattr(video_generation, "_provider_config", provider_config)

    with pytest.raises(VideoUpstreamError) as excinfo:
        await video_generation._provider_for_generation(generation)  # noqa: SLF001

    assert excinfo.value.error_code == "provider_snapshot_unavailable"
    assert "endpoint changed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_submitted_task_rejects_provider_credential_snapshot_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_provider = SimpleNamespace(
        name="provider-a",
        kind="volcano",
        base_url="https://provider.example",
        api_key="account-a-key",
        proxy_name=None,
    )
    replacement_provider = SimpleNamespace(
        name="provider-a",
        kind="volcano",
        base_url="https://provider.example",
        api_key="account-b-key",
        proxy_name=None,
    )
    generation = SimpleNamespace(
        provider_name="provider-a",
        provider_kind="volcano",
        provider_task_id="upstream-1",
        upstream_request={},
        model="seedance",
        action="t2v",
    )
    video_generation._persist_provider_snapshot(  # noqa: SLF001
        generation,
        original_provider,
        upstream_model="seedance-upstream",
    )

    async def provider_config() -> list[SimpleNamespace]:
        return [replacement_provider]

    monkeypatch.setattr(video_generation, "_provider_config", provider_config)

    with pytest.raises(VideoUpstreamError) as excinfo:
        await video_generation._provider_for_generation(generation)  # noqa: SLF001

    snapshot = generation.upstream_request["provider_snapshot"]
    assert "api_key" not in snapshot
    assert snapshot["binding_fingerprint"]
    assert excinfo.value.error_code == "provider_snapshot_unavailable"
    assert "credentials or route changed" in str(excinfo.value)


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


def test_video_poll_renews_lease_and_threads_loss_fence() -> None:
    source = inspect.getsource(video_generation.run_video_poll)

    assert "_lease_renewer(" in source
    assert "lease_lost = asyncio.Event()" in source
    assert "adapter.poll(provider_task_id)" in source
    assert '"video poll lease lost during provider poll"' in source
    assert "lease_lost=lease_lost" in source
    assert "renewer.cancel()" in source


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
    assert "await _continue_running_poll(" in source
    assert "lease_lost=lease_lost" in source
    assert "extended_polling_continues" in helper
    assert "extended_poll_delay_s" in helper
    assert "_EXTENDED_POLL_INTERVAL_S" in helper
    assert "video task exceeded maximum provider tracking window" in source
    assert "poll_timeout" in source
    assert "max_poll_duration_s" in source
    assert "_MAX_PROVIDER_POLL_DURATION_S" in source
    assert _MAX_PROVIDER_POLL_DURATION_S > _MAX_POLL_DURATION_S
    assert "poll_elapsed_s" in source


@pytest.mark.asyncio
async def test_poll_result_fences_db_mutation_after_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lost = asyncio.Event()
    lost.set()
    monkeypatch.setattr(
        video_generation,
        "SessionLocal",
        lambda: pytest.fail("database should not be opened after lease loss"),
    )

    with pytest.raises(video_generation._VideoLeaseLost):  # noqa: SLF001
        await video_generation._apply_poll_result(  # noqa: SLF001
            object(),
            "video-1",
            PollResult(status="running"),
            lease_lost=lost,
        )


@pytest.mark.asyncio
async def test_repeated_deterministic_poll_exception_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    generation = SimpleNamespace(
        id="video-1",
        status="running",
        diagnostics={},
        submitted_at=now,
        deadline_at=now + timedelta(minutes=10),
        progress_stage="rendering",
        progress_pct=20,
        poll_count=0,
        next_poll_at=None,
        error_code=None,
        error_message=None,
        provider_name="provider-a",
    )
    terminal_polls: list[PollResult] = []
    enqueued: list[tuple[str, int]] = []

    class Result:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return generation

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result()

        async def commit(self) -> None:
            return None

    async def publish(*_args: object, **_kwargs: object) -> None:
        return None

    async def enqueue(_redis: object, task_id: str, *, defer_s: int = 0) -> None:
        enqueued.append((task_id, defer_s))

    async def finish_terminal(
        _session: object,
        _redis: object,
        _generation: object,
        poll: PollResult,
        *,
        fallback_error_message: str | None,
        lease_lost: asyncio.Event | None = None,
    ) -> None:
        del fallback_error_message, lease_lost
        terminal_polls.append(poll)

    monkeypatch.setattr(video_generation, "SessionLocal", Session)
    monkeypatch.setattr(video_generation, "_publish", publish)
    monkeypatch.setattr(video_generation, "_enqueue_poll", enqueue)
    monkeypatch.setattr(
        video_generation,
        "_finish_terminal_failure",
        finish_terminal,
    )

    for _attempt in range(video_generation._MAX_UNEXPECTED_POLL_ATTEMPTS):  # noqa: SLF001
        await video_generation._handle_unexpected_poll_exception(  # noqa: SLF001
            object(),
            generation.id,
            ValueError("invalid deterministic payload"),
        )

    assert len(enqueued) == video_generation._MAX_UNEXPECTED_POLL_ATTEMPTS - 1  # noqa: SLF001
    assert len(terminal_polls) == 1
    assert terminal_polls[0].status == "failed"
    assert terminal_polls[0].failure_class == "poll_internal_error"
    assert terminal_polls[0].upstream_billable is None
    assert terminal_polls[0].raw["upstream_cost_ambiguous"] is True


@pytest.mark.asyncio
async def test_terminal_failure_does_not_rebill_terminal_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    billing_calls = 0
    generation = SimpleNamespace(
        id="video-1",
        status="failed",
        provider_name="provider-a",
    )

    async def resolve_billing(*_args: object, **_kwargs: object) -> None:
        nonlocal billing_calls
        billing_calls += 1

    monkeypatch.setattr(
        video_generation,
        "resolve_video_billing",
        resolve_billing,
    )

    await video_generation._finish_terminal_failure(  # noqa: SLF001
        object(),
        object(),
        generation,
        PollResult(status="failed"),
        fallback_error_message="ignored",
    )

    assert generation.status == "failed"
    assert billing_calls == 0


@pytest.mark.asyncio
async def test_invalid_artifact_terminal_event_is_staged_and_rolled_back_with_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        provider_name="provider-1",
        status="running",
        cancel_requested_at=None,
        progress_stage="rendering",
        progress_pct=90,
        upstream_response=None,
        diagnostics={},
        error_code=None,
        error_message=None,
        billed_tokens=None,
        billed_cost_micro=None,
        finished_at=None,
    )

    class Result:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return generation

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0
            self.staged: list[object] = []
            self.staged_at_terminal_commit: list[object] = []

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(
            self,
            exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            if exc_type is not None:
                await self.rollback()

        async def execute(self, _statement: object) -> Result:
            return Result()

        def add(self, value: object) -> None:
            self.staged.append(value)

        async def commit(self) -> None:
            self.commits += 1
            if self.commits == 1:
                assert self.staged == []
                return
            self.staged_at_terminal_commit = list(self.staged)
            raise RuntimeError("terminal commit failed")

        async def rollback(self) -> None:
            self.rollbacks += 1
            self.staged.clear()

    class Adapter:
        async def download_result(
            self,
            _url: str,
            *,
            ensure_active: object,
        ) -> bytes:
            ensure_active()
            return b"not-a-video"

    async def reject_artifact(*_args: object, **_kwargs: object) -> object:
        raise video_artifacts.InvalidVideoArtifactError(
            "no video stream",
            diagnostics={"probe_error": "no video stream"},
        )

    billing_calls: list[tuple[object, PollResult, str]] = []

    async def billing(
        session_arg: object,
        _generation: object,
        *,
        poll_result: PollResult,
        reason: str,
    ) -> SimpleNamespace:
        billing_calls.append((session_arg, poll_result, reason))
        return SimpleNamespace(
            decision="failure_usage_settle",
            actual_tokens=poll_result.usage_total_tokens,
            actual_micro=321,
        )

    session = Session()
    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(video_generation, "_store_video_asset", reject_artifact)
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(video_generation, "_publish", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", _noop_async)

    with pytest.raises(RuntimeError, match="terminal commit failed"):
        await video_generation._apply_poll_result(  # noqa: SLF001
            object(),
            generation.id,
            PollResult(
                status="succeeded",
                video_url="https://cdn.example/invalid.mp4",
                usage_total_tokens=42,
                upstream_billable=True,
                raw={"provider_state": "succeeded"},
            ),
            adapter=Adapter(),  # type: ignore[arg-type]
        )

    assert len(billing_calls) == 1
    billing_session, billing_poll, billing_reason = billing_calls[0]
    assert billing_session is session
    assert billing_reason == "invalid_video_artifact_after_upstream_success"
    assert billing_poll.usage_total_tokens == 42
    assert billing_poll.upstream_billable is True
    assert billing_poll.raw["reason"] == "invalid_video_artifact_after_upstream_success"
    assert session.commits == 2
    assert session.rollbacks == 1
    assert session.staged == []
    assert len(session.staged_at_terminal_commit) == 1
    event = session.staged_at_terminal_commit[0]
    assert isinstance(event, OutboxEvent)
    assert event.payload["event_name"] == "video.failed"
    assert event.payload["data"]["status"] == "failed"
    assert event.payload["data"]["error_code"] == "invalid_video_artifact"


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
    source = inspect.getsource(video_generation._handle_video_upstream_poll_error)
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
    source = inspect.getsource(video_generation._handle_video_upstream_poll_error)

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


def _finalization_generation() -> SimpleNamespace:
    return SimpleNamespace(
        id="video-1",
        user_id="user-1",
        provider_name="provider-1",
        status="running",
        cancel_requested_at=None,
        progress_stage="rendering",
        progress_pct=90,
        upstream_response=None,
        diagnostics={},
        billed_tokens=None,
        billed_cost_micro=None,
        finished_at=None,
    )


class _FinalizationSession:
    def __init__(
        self,
        *,
        on_refresh: object | None = None,
        fail_final_commit: bool = False,
    ) -> None:
        self.on_refresh = on_refresh
        self.fail_final_commit = fail_final_commit
        self.commits = 0
        self.added: list[object] = []
        self.flushes = 0

    async def commit(self) -> None:
        self.commits += 1
        if self.fail_final_commit and self.commits == 2:
            raise RuntimeError("final commit failed")

    async def refresh(self, generation: object, *, with_for_update: bool) -> None:
        assert with_for_update is True
        if callable(self.on_refresh):
            self.on_refresh(generation)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


async def _noop_async(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["download", "storage"])
async def test_finalization_cancel_during_download_or_storage_never_settles_success(
    monkeypatch: pytest.MonkeyPatch,
    cancel_phase: str,
) -> None:
    generation = _finalization_generation()
    cancel_requested_at: datetime | None = None
    deleted_keys: list[str] = []
    billing_calls: list[tuple[str, PollResult]] = []
    events: list[str] = []

    def request_cancel() -> None:
        nonlocal cancel_requested_at
        cancel_requested_at = datetime.now(timezone.utc)

    def refresh(row: object) -> None:
        row.cancel_requested_at = cancel_requested_at

    class Adapter:
        async def download_result(
            self,
            _url: str,
            *,
            ensure_active: object,
        ) -> bytes:
            ensure_active()
            if cancel_phase == "download":
                request_cancel()
            ensure_active()
            return b"downloaded"

    async def store(
        _generation: object,
        _downloaded: object,
        *,
        lease_lost: asyncio.Event | None,
        artifact_attempt_id: str,
    ) -> object:
        assert lease_lost is not None
        if cancel_phase == "storage":
            request_cancel()
        key = f"u/user-1/v/video-1/final/{artifact_attempt_id}/output.mp4"
        return video_generation._StoredVideo(  # noqa: SLF001
            video=SimpleNamespace(id="stored-video"),
            diagnostics={"output_mime": "video/mp4"},
            created_storage_keys=(key,),
        )

    async def billing(
        _session: object,
        _generation: object,
        *,
        poll_result: PollResult,
        reason: str,
    ) -> SimpleNamespace:
        billing_calls.append((reason, poll_result))
        return SimpleNamespace(
            decision="failure_usage_settle",
            actual_tokens=poll_result.usage_total_tokens,
            actual_micro=321,
        )

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        deleted_keys.extend(keys)

    monkeypatch.setattr(video_generation, "new_uuid7", lambda: "attempt-current")
    monkeypatch.setattr(video_generation, "_store_video_asset", store)
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)
    monkeypatch.setattr(video_generation, "_publish", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", _noop_async)
    monkeypatch.setattr(video_generation, "worker_flush_balance_cache", _noop_async)
    monkeypatch.setattr(
        video_generation,
        "_queue_video_event",
        lambda _session, _generation, event, **_kwargs: events.append(event),
    )

    session = _FinalizationSession(on_refresh=refresh)
    poll = PollResult(
        status="succeeded",
        video_url="https://cdn.example/output.mp4",
        usage_total_tokens=42,
        upstream_billable=True,
        raw={"provider_state": "succeeded"},
    )

    await video_generation._finish_success(  # noqa: SLF001
        session,
        object(),
        generation,
        poll,
        adapter=Adapter(),
        lease_lost=asyncio.Event(),
    )

    assert generation.status == "canceled"
    assert session.added == []
    assert len(billing_calls) == 1
    reason, billing_poll = billing_calls[0]
    assert reason == "cancelled"
    assert billing_poll.status == "cancelled"
    assert billing_poll.usage_total_tokens == 42
    assert billing_poll.upstream_billable is True
    assert billing_poll.raw["reason"] == "cancel_requested_during_finalization"
    assert deleted_keys == ["u/user-1/v/video-1/final/attempt-current/output.mp4"]
    assert events == ["video.canceled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created", "expected_deleted"),
    [
        (True, ["u/user-1/v/video-1/final/attempt-current/output.mp4"]),
        (False, []),
    ],
)
async def test_video_store_lease_loss_deletes_only_new_attempt_artifact(
    monkeypatch: pytest.MonkeyPatch,
    created: bool,
    expected_deleted: list[str],
) -> None:
    lease_lost = asyncio.Event()
    deleted_keys: list[str] = []

    def postprocess(_data: bytes) -> tuple[dict[str, object], dict[str, object]]:
        return (
            {
                "video_bytes": b"video",
                "poster_bytes": None,
                "mime": "video/mp4",
                "extension": ".mp4",
                "faststart": True,
            },
            {"output_mime": "video/mp4"},
        )

    async def put(
        _key: str,
        _data: bytes,
        *,
        track_created: bool,
    ) -> bool:
        assert track_created is True
        lease_lost.set()
        return created

    def delete(key: str) -> bool:
        deleted_keys.append(key)
        return True

    monkeypatch.setattr(video_generation, "_postprocess_video_bytes", postprocess)
    monkeypatch.setattr(video_generation, "_put_video_storage_bytes", put)
    monkeypatch.setattr(video_generation.storage, "delete", delete)

    with pytest.raises(video_generation._VideoLeaseLost):  # noqa: SLF001
        await video_generation._store_video_asset(  # noqa: SLF001
            _finalization_generation(),
            b"upstream-video",
            lease_lost=lease_lost,
            artifact_attempt_id="attempt-current",
        )

    assert deleted_keys == expected_deleted


@pytest.mark.asyncio
async def test_finalization_terminal_race_rolls_back_only_current_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _finalization_generation()
    deleted_keys: list[str] = []
    current_key = "u/user-1/v/video-1/final/attempt-current/output.mp4"
    other_attempt_key = "u/user-1/v/video-1/final/attempt-other/output.mp4"

    class Adapter:
        async def download_result(
            self,
            _url: str,
            *,
            ensure_active: object,
        ) -> bytes:
            ensure_active()
            return b"downloaded"

    async def store(*_args: object, **_kwargs: object) -> object:
        return video_generation._StoredVideo(  # noqa: SLF001
            video=SimpleNamespace(id="stored-video"),
            diagnostics={},
            created_storage_keys=(current_key,),
        )

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        deleted_keys.extend(keys)

    def win_terminal_race(row: object) -> None:
        row.status = "failed"

    async def unexpected_billing(*_args: object, **_kwargs: object) -> object:
        pytest.fail("terminal loser must not bill")

    monkeypatch.setattr(video_generation, "new_uuid7", lambda: "attempt-current")
    monkeypatch.setattr(video_generation, "_store_video_asset", store)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)
    monkeypatch.setattr(video_generation, "resolve_video_billing", unexpected_billing)
    monkeypatch.setattr(video_generation, "_publish", _noop_async)

    session = _FinalizationSession(on_refresh=win_terminal_race)
    await video_generation._finish_success(  # noqa: SLF001
        session,
        object(),
        generation,
        PollResult(
            status="succeeded",
            video_url="https://cdn.example/output.mp4",
        ),
        adapter=Adapter(),
        lease_lost=asyncio.Event(),
    )

    assert generation.status == "failed"
    assert session.commits == 1
    assert deleted_keys == [current_key]
    assert other_attempt_key not in deleted_keys


@pytest.mark.asyncio
async def test_finalization_commit_failure_rolls_back_created_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _finalization_generation()
    deleted_keys: list[str] = []
    billing_reasons: list[str] = []
    current_key = "u/user-1/v/video-1/final/attempt-current/output.mp4"

    class Adapter:
        async def download_result(
            self,
            _url: str,
            *,
            ensure_active: object,
        ) -> bytes:
            ensure_active()
            return b"downloaded"

    async def store(*_args: object, **_kwargs: object) -> object:
        return video_generation._StoredVideo(  # noqa: SLF001
            video=SimpleNamespace(id="stored-video"),
            diagnostics={"output_mime": "video/mp4"},
            created_storage_keys=(current_key,),
        )

    async def no_existing_video(*_args: object, **_kwargs: object) -> None:
        return None

    async def billing(
        *_args: object,
        reason: str,
        **_kwargs: object,
    ) -> SimpleNamespace:
        billing_reasons.append(reason)
        return SimpleNamespace(
            decision="actual_usage_settle",
            actual_tokens=42,
            actual_micro=321,
        )

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        deleted_keys.extend(keys)

    monkeypatch.setattr(video_generation, "new_uuid7", lambda: "attempt-current")
    monkeypatch.setattr(video_generation, "_store_video_asset", store)
    monkeypatch.setattr(video_generation, "_video_for_generation", no_existing_video)
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)
    monkeypatch.setattr(video_generation, "_publish", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", _noop_async)
    monkeypatch.setattr(video_generation, "_queue_video_event", lambda *_a, **_k: None)

    session = _FinalizationSession(fail_final_commit=True)
    with pytest.raises(RuntimeError, match="final commit failed"):
        await video_generation._finish_success(  # noqa: SLF001
            session,
            object(),
            generation,
            PollResult(
                status="succeeded",
                video_url="https://cdn.example/output.mp4",
                usage_total_tokens=42,
            ),
            adapter=Adapter(),
            lease_lost=asyncio.Event(),
        )

    assert session.added
    assert session.flushes == 1
    assert billing_reasons == ["succeeded"]
    assert deleted_keys == [current_key]
