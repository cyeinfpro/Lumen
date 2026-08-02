from __future__ import annotations

# ruff: noqa: E402

import asyncio
import inspect
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from lumen_core.models import OutboxEvent
from lumen_core.upstream_billing import (
    LocalBillingAction,
    UpstreamCostKnowledge,
    decide_upstream_billing,
)
from sqlalchemy.dialects import postgresql

from app import video_artifacts
from app.artifact_commit import ArtifactAdoption, commit_with_adoption_probe
from app.storage import LocalStorage
from app.tasks.video_generation_parts.default_runtime import (
    _MAX_POLL_COUNT,
    _MAX_POLL_DURATION_S,
    _MAX_PROVIDER_POLL_DURATION_S,
    _POLL_INTERVAL_S,
    _is_retryable_video_exception,
    _submit_failure_billable_hint,
    _submit_outcome_unknown,
    _submit_retry_delay_s,
    _video_exception_code,
    _video_exception_message,
)
from app.tasks.video_generation_parts.errors import (
    submit_delivery_proven_absent as _submit_delivery_proven_absent,
)
from app.tasks.video_generation_parts import default_runtime as video_generation
from app.tasks.video_generation_parts import submission as video_submission
from app.tasks.video_generation_parts import persistence as video_persistence
from app.tasks.video_generation_parts.contracts import StoredVideo
from app.video_provider_slots import (
    VIDEO_PROVIDER_SLOT_STALE_AFTER_S,
    VIDEO_PROVIDER_SLOT_TTL_S,
)
from .task_parts_runtime_testing import synchronize_module_ports


@pytest.fixture(autouse=True)
def _sync_video_ports(
    monkeypatch: pytest.MonkeyPatch,
):
    with synchronize_module_ports(
        monkeypatch,
        video_generation,
        video_generation.DEFAULT_VIDEO_GENERATION_RUNTIME.ports,
    ):
        yield


from app.video_upstream_service import (
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


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.PoolTimeout("no free connection"),
        httpx.ConnectError("dns failure"),
        httpx.ProxyError("proxy refused CONNECT"),
        httpx.UnsupportedProtocol("gopher://"),
        httpx.LocalProtocolError("bad request line"),
    ],
)
def test_undelivered_submit_failures_are_not_result_unknown(exc: Exception) -> None:
    """E-8: 连接/代理/连接池阶段失败 = 请求没送达，不是「结果不可知」。

    归进 unknown 会把任务钉死在 SUBMIT_UNKNOWN：既不重试，对账时还按上限
    结算——对一笔上游根本没收到的请求收钱。
    """
    assert _submit_delivery_proven_absent(exc) is True
    assert _submit_outcome_unknown(exc) is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("response never arrived"),
        httpx.WriteTimeout("body half-written"),
        httpx.ReadError("connection reset while reading"),
        httpx.WriteError("connection reset while writing"),
        httpx.RemoteProtocolError("server disconnected"),
        asyncio.TimeoutError(),
    ],
)
def test_delivered_or_ambiguous_submit_failures_stay_unknown(exc: Exception) -> None:
    """请求已经（至少部分）发出去了 → 上游可能已扣费 → 必须走结算。"""
    assert _submit_delivery_proven_absent(exc) is False
    assert _submit_outcome_unknown(exc) is True


def test_undelivered_submit_failure_resolves_to_release() -> None:
    """E-8 计费闭环：可证明未送达 → 决策表判 RELEASE，不能按上限扣用户的钱。"""
    hint = _submit_failure_billable_hint(httpx.ConnectError("dns failure"))

    assert hint is False
    decision = decide_upstream_billing(
        upstream_billable=hint,
        actual_cost_known=False,
        # fail_before_submit 在 hint 为 False 时传的就是这个 reason。
        receipt_reasons=("submit_failed_before_upstream_cost",),
    )
    assert decision.knowledge is UpstreamCostKnowledge.PROVEN_ABSENT
    assert decision.action is LocalBillingAction.RELEASE


def test_delivered_submit_failure_resolves_to_settle() -> None:
    """纯转嫁另一侧：结果不可知时 hint 保持 None → 结算而不是释放。"""
    hint = _submit_failure_billable_hint(httpx.ReadTimeout("no response"))

    assert hint is None
    decision = decide_upstream_billing(
        upstream_billable=hint,
        actual_cost_known=False,
        receipt_reasons=("submit_failed_ambiguous_upstream_cost",),
    )
    assert decision.knowledge is UpstreamCostKnowledge.UNKNOWN
    assert decision.action is LocalBillingAction.SETTLE_DEFAULT


def test_undelivered_submit_failure_stays_retryable() -> None:
    """没送达就没有第二笔上游成本，重试是安全且必要的。"""
    assert _is_retryable_video_exception(httpx.ConnectError("dns failure")) is True


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
            if self.commits <= 2:
                assert self.staged == []
                return
            self.staged_at_terminal_commit = list(self.staged)
            raise RuntimeError("terminal commit failed")

        async def refresh(
            self,
            _generation: object,
            *,
            with_for_update: bool,
        ) -> None:
            assert with_for_update is True

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
    assert session.commits == 3
    assert session.rollbacks == 1
    assert session.staged == []
    assert len(session.staged_at_terminal_commit) == 1
    event = session.staged_at_terminal_commit[0]
    assert isinstance(event, OutboxEvent)
    assert event.payload["event_name"] == "video.failed"
    assert event.payload["data"]["status"] == "failed"
    assert event.payload["data"]["error_code"] == "invalid_video_artifact"


def test_video_provider_slot_ttl_covers_tracking_window() -> None:
    assert VIDEO_PROVIDER_SLOT_STALE_AFTER_S > _MAX_PROVIDER_POLL_DURATION_S
    assert VIDEO_PROVIDER_SLOT_TTL_S > VIDEO_PROVIDER_SLOT_STALE_AFTER_S


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
    assert "generation.billed_cost_micro = resolution.actual_micro" in canceled_source
    assert "generation.billed_cost_micro = resolution.actual_micro" in expired_source

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
async def test_idempotent_ambiguous_submit_retry_preserves_delivery_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fail_before_submit(
        _redis: object,
        _task_id: str,
        _exc: Exception,
        **kwargs: object,
    ) -> None:
        captured.update(kwargs)

    async def fail_mark_unknown(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("idempotent ambiguous submits should use retry handling")

    monkeypatch.setattr(
        video_generation,
        "_fail_before_submit",
        fail_before_submit,
    )
    monkeypatch.setattr(
        video_generation,
        "_mark_submit_unknown",
        fail_mark_unknown,
    )

    await video_generation._handle_video_submit_exception(  # noqa: SLF001
        object(),
        "video-1",
        httpx.ReadTimeout("timeout"),
        provider_name="provider-1",
        submission_epoch=2,
        upstream_invoked=True,
        provider_supports_idempotency=True,
    )

    assert captured["upstream_invoked"] is True
    assert captured["provider_supports_idempotency"] is True
    assert captured["submission_epoch"] == 2


def test_submit_delivery_evidence_never_downgrades_unknown_to_absent() -> None:
    generation = SimpleNamespace(
        provider_task_id=None,
        provider_idempotency_key="video:video-1",
        diagnostics={},
        attempt=1,
        submission_epoch=1,
        submit_started_at=datetime.now(timezone.utc),
    )

    video_submission._record_submit_delivery(  # noqa: SLF001
        generation,
        state="unknown",
        reason="ambiguous_idempotent_timeout",
        provider_supports_idempotency=True,
    )
    video_submission._record_submit_delivery(  # noqa: SLF001
        generation,
        state="proven_absent",
        reason="later_connect_error",
        provider_supports_idempotency=True,
    )

    assert generation.diagnostics["submit_delivery_state"] == "unknown"
    assert [
        item["state"] for item in generation.diagnostics["submit_delivery_history"]
    ] == ["unknown", "proven_absent"]


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
async def test_ambiguous_submit_unknown_releases_provider_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[tuple[str, str]] = []

    async def mark_unknown(*_args: object, **_kwargs: object) -> bool:
        return True

    async def release_slot(
        _redis: object,
        provider_name: str,
        task_id: str,
    ) -> None:
        released.append((provider_name, task_id))

    monkeypatch.setattr(video_generation, "_mark_submit_unknown", mark_unknown)
    monkeypatch.setattr(video_generation, "_release_provider_slot", release_slot)

    await video_generation._handle_video_submit_exception(  # noqa: SLF001
        object(),
        "video-1",
        httpx.ReadTimeout("response never arrived"),
        provider_name="provider-1",
        submission_epoch=2,
        upstream_invoked=True,
        provider_supports_idempotency=False,
    )

    # The slot must not be held through the whole SUBMIT_UNKNOWN finalize
    # window; exclusive providers would otherwise be blocked for an hour.
    assert released == [("provider-1", "video-1")]


@pytest.mark.asyncio
async def test_submit_exception_with_lost_lease_fence_skips_db_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def mark_unknown(*_args: object, **_kwargs: object) -> bool:
        calls.append("unknown")
        return True

    async def fail_before_submit(*_args: object, **_kwargs: object) -> None:
        calls.append("fail")

    monkeypatch.setattr(video_generation, "_mark_submit_unknown", mark_unknown)
    monkeypatch.setattr(video_generation, "_fail_before_submit", fail_before_submit)

    lease_lost = asyncio.Event()
    lease_lost.set()

    # Outcome-unknown failure while the lease is already lost: the stale
    # worker must not write SUBMIT_UNKNOWN/FAILED to the row.
    await video_generation._handle_video_submit_exception(  # noqa: SLF001
        object(),
        "video-1",
        httpx.ReadTimeout("response never arrived"),
        provider_name="provider-1",
        submission_epoch=2,
        upstream_invoked=True,
        provider_supports_idempotency=False,
        lease_lost=lease_lost,
    )
    # Retryable failure with the lease lost: still fenced from terminal writes.
    await video_generation._handle_video_submit_exception(  # noqa: SLF001
        object(),
        "video-1",
        httpx.ConnectError("dns failure"),
        provider_name="provider-1",
        submission_epoch=2,
        upstream_invoked=True,
        provider_supports_idempotency=False,
        lease_lost=lease_lost,
    )

    assert calls == []


@pytest.mark.asyncio
async def test_cancel_commit_after_submitting_fences_adapter_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    generation = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="queued",
        provider_name=None,
        provider_kind=None,
        provider_task_id=None,
        provider_idempotency_key=None,
        upstream_request={},
        upstream_response=None,
        model="seedance",
        action="t2v",
        prompt="hello",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        generate_audio=True,
        seed=None,
        watermark=False,
        attempt=0,
        submission_epoch=0,
        submit_started_at=None,
        started_at=None,
        submitted_at=None,
        next_poll_at=None,
        deadline_at=now + timedelta(minutes=10),
        cancel_requested_at=None,
        progress_stage="queued",
        progress_pct=0,
        error_code=None,
        error_message=None,
        finished_at=None,
        diagnostics={},
        billed_tokens=None,
        billed_cost_micro=None,
    )
    adapter_calls = 0
    receipt_calls = 0
    released_slots: list[tuple[str, str]] = []

    class Result:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return generation

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.statements: list[object] = []

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result()

        async def commit(self) -> None:
            self.commits += 1
            if self.commits == 1:
                # The cancellation API commits after SUBMITTING but before this
                # worker re-locks the epoch for dispatch.
                generation.cancel_requested_at = now

    session = Session()
    provider = SimpleNamespace(
        name="provider-1",
        kind="volcano",
        supports_idempotency=True,
        upstream_model_for=lambda _model, _action: "seedance-upstream",
    )

    class Adapter:
        async def submit(self, _request: VideoSubmitRequest) -> None:
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("cancellation fence must block upstream submit")

    async def prepared(*_args: object, **_kwargs: object) -> object:
        return video_submission._SubmitPreparation(  # noqa: SLF001
            generation=generation,
            cached_submit=None,
        )

    async def reserve(*_args: object, **_kwargs: object) -> bool:
        return True

    async def provider_for_generation(*_args: object, **_kwargs: object) -> object:
        return provider

    async def input_image(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
        return b"image", "image/png"

    async def reference_media(*_args: object, **_kwargs: object) -> list[object]:
        return []

    async def billing(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            decision="pre_submit_cancel_release",
            actual_tokens=None,
            actual_micro=0,
        )

    async def release_slot(
        _redis: object,
        provider_name: str,
        task_id: str,
    ) -> None:
        released_slots.append((provider_name, task_id))

    async def receipt_must_not_run(*_args: object, **_kwargs: object) -> bool:
        nonlocal receipt_calls
        receipt_calls += 1
        raise AssertionError("cancellation fence must not persist a receipt")

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(video_submission, "_prepare_submit_row", prepared)
    monkeypatch.setattr(
        video_generation,
        "_provider_for_generation",
        provider_for_generation,
    )
    monkeypatch.setattr(video_generation, "_reserve_video_submit_slot", reserve)
    monkeypatch.setattr(video_generation, "_input_image_bytes", input_image)
    monkeypatch.setattr(video_generation, "_reference_media_bytes", reference_media)
    monkeypatch.setattr(video_generation, "_persist_provider_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(video_generation, "adapter_for_provider", lambda _provider: Adapter())
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(video_generation, "_queue_video_event", lambda *_a, **_k: None)
    monkeypatch.setattr(video_generation, "worker_flush_balance_cache", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", release_slot)
    monkeypatch.setattr(video_generation, "_persist_video_submit_receipt", receipt_must_not_run)

    await video_generation._run_video_generation_with_lease(  # noqa: SLF001
        {"redis": object()},
        generation.id,
        token="owner-1",
        lease_lost=asyncio.Event(),
    )

    assert adapter_calls == 0
    assert receipt_calls == 0
    assert generation.status == "canceled"
    assert generation.provider_task_id is None
    assert "submit_receipt" not in generation.diagnostics
    assert generation.diagnostics["submit_delivery_state"] == "proven_absent"
    assert released_slots == [("provider-1", generation.id)]
    assert len(session.statements) == 2
    user_fence_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    fence_sql = str(
        session.statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "users.id = 'user-1'" in user_fence_sql
    assert "video_generations.id = 'video-1'" in fence_sql
    assert "video_generations.submission_epoch = 1" in fence_sql


@pytest.mark.asyncio
async def test_submit_receipt_persists_cancel_requested_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_requested_at = datetime.now(timezone.utc)
    generation = SimpleNamespace(
        id="video-1",
        submission_epoch=2,
        cancel_requested_at=cancel_requested_at,
        status="submitting",
        provider_task_id=None,
        provider_idempotency_key="video:video-1",
        upstream_response=None,
        progress_stage="submitting",
        progress_pct=5,
        submitted_at=None,
        next_poll_at=None,
        attempt=1,
        submit_started_at=cancel_requested_at,
        diagnostics={},
    )
    published: list[str] = []

    class Result:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return generation

    class Session:
        commits = 0

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result()

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)

    async def publish(
        _redis: object,
        _generation: object,
        event: str,
    ) -> None:
        published.append(event)

    monkeypatch.setattr(video_generation, "_publish_after_commit", publish)

    persisted = await video_generation._persist_video_submit_receipt(  # noqa: SLF001
        object(),
        generation.id,
        SimpleNamespace(provider_task_id="provider-task-1", raw={"id": "provider-task-1"}),
        submission_epoch=generation.submission_epoch,
        lease_lost=asyncio.Event(),
    )

    assert persisted is True
    assert session.commits == 1
    assert generation.cancel_requested_at is cancel_requested_at
    assert generation.status == "submitted"
    assert generation.provider_task_id == "provider-task-1"
    assert generation.diagnostics["submit_receipt"]["submission_epoch"] == 2
    assert generation.diagnostics["submit_delivery_state"] == "confirmed"
    assert published == ["video.submitted"]


@pytest.mark.asyncio
async def test_submit_then_cancel_persists_receipt_and_enqueues_poll_without_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    generation = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="queued",
        provider_name=None,
        provider_kind=None,
        provider_task_id=None,
        provider_idempotency_key=None,
        upstream_request={},
        upstream_response=None,
        model="seedance",
        action="t2v",
        prompt="hello",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        generate_audio=True,
        seed=None,
        watermark=False,
        attempt=0,
        submission_epoch=0,
        submit_started_at=None,
        started_at=None,
        submitted_at=None,
        next_poll_at=None,
        deadline_at=now + timedelta(minutes=10),
        cancel_requested_at=None,
        progress_stage="queued",
        progress_pct=0,
        error_code=None,
        error_message=None,
        finished_at=None,
        diagnostics={},
        billed_tokens=None,
        billed_cost_micro=None,
    )
    active_user = SimpleNamespace(deleted_at=None)
    adapter_calls = 0
    cache_calls = 0
    polls: list[tuple[str, int | None]] = []

    class Result:
        def __init__(self, row: object) -> None:
            self.row = row

        def scalar_one_or_none(self) -> object:
            return self.row

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.rows = iter((active_user, generation, generation))

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result(next(self.rows))

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    provider = SimpleNamespace(
        name="provider-1",
        kind="volcano",
        supports_idempotency=True,
        upstream_model_for=lambda _model, _action: "seedance-upstream",
    )

    class Adapter:
        async def submit(self, _request: VideoSubmitRequest) -> SimpleNamespace:
            nonlocal adapter_calls
            adapter_calls += 1
            generation.cancel_requested_at = now
            return SimpleNamespace(
                provider_task_id="provider-task-1",
                raw={"id": "provider-task-1"},
            )

    async def prepared(*_args: object, **_kwargs: object) -> object:
        return video_submission._SubmitPreparation(  # noqa: SLF001
            generation=generation,
            cached_submit=None,
        )

    async def reserve(*_args: object, **_kwargs: object) -> bool:
        return True

    async def provider_for_generation(*_args: object, **_kwargs: object) -> object:
        return provider

    async def input_image(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
        return b"image", "image/png"

    async def reference_media(*_args: object, **_kwargs: object) -> list[object]:
        return []

    async def store_submit_result(*_args: object, **_kwargs: object) -> None:
        nonlocal cache_calls
        cache_calls += 1

    async def enqueue_poll(
        _redis: object,
        task_id: str,
        *,
        defer_s: int | None = None,
    ) -> None:
        polls.append((task_id, defer_s))

    async def unexpected_recovery(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("confirmed receipt must route to poll, not submit recovery")

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(video_submission, "_prepare_submit_row", prepared)
    monkeypatch.setattr(video_generation, "_provider_for_generation", provider_for_generation)
    monkeypatch.setattr(video_generation, "_reserve_video_submit_slot", reserve)
    monkeypatch.setattr(video_generation, "_input_image_bytes", input_image)
    monkeypatch.setattr(video_generation, "_reference_media_bytes", reference_media)
    monkeypatch.setattr(video_generation, "_persist_provider_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(video_generation, "adapter_for_provider", lambda _provider: Adapter())
    monkeypatch.setattr(video_generation, "_store_submit_result", store_submit_result)
    monkeypatch.setattr(video_generation, "_enqueue_poll", enqueue_poll)
    monkeypatch.setattr(
        video_generation,
        "_enqueue_cached_submit_recovery",
        unexpected_recovery,
    )
    monkeypatch.setattr(video_generation, "_publish_after_commit", _noop_async)
    monkeypatch.setattr(video_generation, "_release_lease", _noop_async)

    await video_generation._run_video_generation_with_lease(  # noqa: SLF001
        {"redis": object()},
        generation.id,
        token="owner-1",
        lease_lost=asyncio.Event(),
    )

    assert adapter_calls == 1
    assert cache_calls == 1
    assert generation.cancel_requested_at is now
    assert generation.status == "submitted"
    assert generation.provider_task_id == "provider-task-1"
    assert generation.diagnostics["submit_delivery_state"] == "confirmed"
    assert polls == [(generation.id, None)]
    assert session.commits == 2


@pytest.mark.asyncio
async def test_deleted_user_fence_cancels_before_provider_submit_and_settles_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    generation = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        status="queued",
        provider_name=None,
        provider_kind=None,
        provider_task_id=None,
        provider_idempotency_key=None,
        upstream_request={},
        upstream_response=None,
        model="seedance",
        action="t2v",
        prompt="hello",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        generate_audio=True,
        seed=None,
        watermark=False,
        attempt=0,
        submission_epoch=0,
        submit_started_at=None,
        started_at=None,
        submitted_at=None,
        next_poll_at=None,
        deadline_at=now + timedelta(minutes=10),
        cancel_requested_at=None,
        progress_stage="queued",
        progress_pct=0,
        error_code=None,
        error_message=None,
        finished_at=None,
        diagnostics={},
        billed_tokens=None,
        billed_cost_micro=None,
    )
    deleted_user = SimpleNamespace(deleted_at=now)
    adapter_calls = 0
    billing_reasons: list[str] = []
    events: list[str] = []
    released_slots: list[tuple[str, str]] = []

    class Result:
        def __init__(self, row: object) -> None:
            self.row = row

        def scalar_one_or_none(self) -> object:
            return self.row

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.rows = iter((deleted_user, generation))
            self.statements: list[object] = []

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result(next(self.rows))

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    provider = SimpleNamespace(
        name="provider-1",
        kind="volcano",
        supports_idempotency=True,
        upstream_model_for=lambda _model, _action: "seedance-upstream",
    )

    class Adapter:
        async def submit(self, _request: VideoSubmitRequest) -> None:
            nonlocal adapter_calls
            adapter_calls += 1
            raise AssertionError("deleted-user fence must block upstream submit")

    async def prepared(*_args: object, **_kwargs: object) -> object:
        return video_submission._SubmitPreparation(  # noqa: SLF001
            generation=generation,
            cached_submit=None,
        )

    async def reserve(*_args: object, **_kwargs: object) -> bool:
        return True

    async def provider_for_generation(*_args: object, **_kwargs: object) -> object:
        return provider

    async def input_image(*_args: object, **_kwargs: object) -> tuple[bytes, str]:
        return b"image", "image/png"

    async def reference_media(*_args: object, **_kwargs: object) -> list[object]:
        return []

    async def billing(
        _session: object,
        _generation: object,
        *,
        reason: str,
        **_kwargs: object,
    ) -> SimpleNamespace:
        billing_reasons.append(reason)
        return SimpleNamespace(
            decision="pre_submit_cancel_release",
            actual_tokens=None,
            actual_micro=0,
        )

    async def release_slot(
        _redis: object,
        provider_name: str,
        task_id: str,
    ) -> None:
        released_slots.append((provider_name, task_id))

    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)
    monkeypatch.setattr(video_submission, "_prepare_submit_row", prepared)
    monkeypatch.setattr(video_generation, "_provider_for_generation", provider_for_generation)
    monkeypatch.setattr(video_generation, "_reserve_video_submit_slot", reserve)
    monkeypatch.setattr(video_generation, "_input_image_bytes", input_image)
    monkeypatch.setattr(video_generation, "_reference_media_bytes", reference_media)
    monkeypatch.setattr(video_generation, "_persist_provider_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(video_generation, "adapter_for_provider", lambda _provider: Adapter())
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(
        video_generation,
        "_queue_video_event",
        lambda _session, _generation, event, **_kwargs: events.append(event),
    )
    monkeypatch.setattr(video_generation, "worker_flush_balance_cache", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", release_slot)

    await video_generation._run_video_generation_with_lease(  # noqa: SLF001
        {"redis": object()},
        generation.id,
        token="owner-1",
        lease_lost=asyncio.Event(),
    )

    assert adapter_calls == 0
    assert generation.cancel_requested_at is not None
    assert generation.status == "canceled"
    assert generation.error_code == "canceled"
    assert generation.billed_cost_micro == 0
    assert generation.diagnostics["pre_submit_cancellation_reason"] == (
        "inactive_user_fence_before_upstream"
    )
    assert generation.diagnostics["submit_delivery_state"] == "proven_absent"
    assert billing_reasons == ["pre_submit_cancel"]
    assert events == ["video.canceled"]
    assert released_slots == [("provider-1", generation.id)]
    assert session.commits == 2
    assert len(session.statements) == 2
    user_fence_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    task_fence_sql = str(
        session.statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "users.id = 'user-1'" in user_fence_sql
    assert "video_generations.id = 'video-1'" in task_fence_sql
    assert "video_generations.submission_epoch = 1" in task_fence_sql


@pytest.mark.asyncio
async def test_submit_receipt_rejects_terminal_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = SimpleNamespace(
        id="video-1",
        submission_epoch=2,
        cancel_requested_at=None,
        status="failed",
        provider_task_id=None,
        diagnostics={},
    )

    class Result:
        def scalar_one_or_none(self) -> SimpleNamespace:
            return generation

    class Session:
        commits = 0

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result()

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    monkeypatch.setattr(video_generation, "SessionLocal", lambda: session)

    persisted = await video_generation._persist_video_submit_receipt(  # noqa: SLF001
        object(),
        generation.id,
        SimpleNamespace(provider_task_id="provider-task-1", raw={"id": "provider-task-1"}),
        submission_epoch=generation.submission_epoch,
        lease_lost=asyncio.Event(),
    )

    assert persisted is False
    assert session.commits == 0
    assert generation.status == "failed"
    assert generation.provider_task_id is None
    assert "submit_receipt" not in generation.diagnostics


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
        provider_task_id="provider-task-1",
        submission_epoch=3,
        attempt=2,
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


def test_video_finalization_key_is_stable_and_execution_scoped() -> None:
    generation = _finalization_generation()
    generation.provider_task_id = "provider-task-1"
    generation.submission_epoch = 3
    generation.attempt = 2

    first = video_persistence.video_artifact_attempt_id(generation)
    second = video_persistence.video_artifact_attempt_id(generation)
    assert first == second
    assert len(first) == 32

    generation.attempt = 3
    assert video_persistence.video_artifact_attempt_id(generation) != first


@pytest.mark.asyncio
async def test_stale_not_adopted_worker_cannot_delete_takeover_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    durable_generation = _finalization_generation()
    row_lock = asyncio.Lock()
    worker_a_stored = asyncio.Event()
    worker_b_reused = asyncio.Event()
    worker_a_cleanup_finished = asyncio.Event()
    worker_a_lease_lost = asyncio.Event()
    durable_video: SimpleNamespace | None = None
    owner_tokens = iter(("owner-a", "owner-b"))

    def local_generation() -> SimpleNamespace:
        values = dict(vars(durable_generation))
        values["diagnostics"] = dict(durable_generation.diagnostics)
        return SimpleNamespace(**values)

    class ClaimSession:
        def __init__(self, generation: SimpleNamespace) -> None:
            self.generation = generation
            self.locked = False

        async def refresh(
            self,
            generation: object,
            *,
            with_for_update: bool,
        ) -> None:
            assert with_for_update is True
            await row_lock.acquire()
            self.locked = True
            for name, value in vars(durable_generation).items():
                setattr(
                    generation,
                    name,
                    dict(value) if name == "diagnostics" else value,
                )

        async def commit(self) -> None:
            for name, value in vars(self.generation).items():
                setattr(
                    durable_generation,
                    name,
                    dict(value) if name == "diagnostics" else value,
                )
            if self.locked:
                self.locked = False
                row_lock.release()

    class DurableSession:
        async def __aenter__(self) -> DurableSession:
            await row_lock.acquire()
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            row_lock.release()

        async def get(
            self,
            _model: object,
            generation_id: str,
            *,
            with_for_update: bool,
        ) -> SimpleNamespace:
            assert generation_id == durable_generation.id
            assert with_for_update is True
            return durable_generation

        async def commit(self) -> None:
            return None

    class RejectedCommitSession:
        async def commit(self) -> None:
            raise RuntimeError("worker A final commit rejected")

        async def rollback(self) -> None:
            return None

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        for key in keys:
            storage.delete(key)

    monkeypatch.setattr(
        video_generation,
        "new_uuid7",
        lambda: next(owner_tokens),
    )
    monkeypatch.setattr(video_generation, "SessionLocal", DurableSession)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)

    async def worker_a() -> tuple[object, bool]:
        generation = local_generation()
        fence = await video_persistence._claim_video_artifact_fence(  # noqa: SLF001
            ClaimSession(generation),
            generation,
            lease_lost=worker_a_lease_lost,
        )
        assert fence is not None
        video_key, _poster_key = video_persistence.video_artifact_keys(
            generation,
            ".mp4",
            artifact_attempt_id=fence.artifact_attempt_id,
        )
        put_result = storage.put_bytes_result(video_key, b"video")
        assert put_result.created is True
        worker_a_stored.set()
        await worker_b_reused.wait()

        async def probe_not_adopted() -> ArtifactAdoption:
            return ArtifactAdoption.NOT_ADOPTED

        commit_result = await commit_with_adoption_probe(
            RejectedCommitSession(),
            probe=probe_not_adopted,
            logger=logging.getLogger(__name__),
            label="worker A video artifact",
        )
        assert commit_result.outcome is ArtifactAdoption.NOT_ADOPTED
        cleanup_result = await video_persistence._cleanup_video_artifacts_if_owned(  # noqa: SLF001
            (video_key,),
            generation_id=generation.id,
            fence=fence,
            lease_lost=worker_a_lease_lost,
        )
        worker_a_cleanup_finished.set()
        return fence, cleanup_result

    async def worker_b() -> tuple[object, bool, str]:
        nonlocal durable_video
        await worker_a_stored.wait()
        generation = local_generation()
        fence = await video_persistence._claim_video_artifact_fence(  # noqa: SLF001
            ClaimSession(generation),
            generation,
            lease_lost=asyncio.Event(),
        )
        assert fence is not None
        video_key, _poster_key = video_persistence.video_artifact_keys(
            generation,
            ".mp4",
            artifact_attempt_id=fence.artifact_attempt_id,
        )
        put_result = storage.put_bytes_result(video_key, b"video")
        worker_b_reused.set()
        await worker_a_cleanup_finished.wait()
        async with DurableSession() as session:
            row = await session.get(
                object(),
                generation.id,
                with_for_update=True,
            )
            diagnostics = dict(row.diagnostics)
            diagnostics[video_persistence._VIDEO_ARTIFACT_FENCE_KEY] = (  # noqa: SLF001
                fence.payload(state=video_persistence._VIDEO_ARTIFACT_ADOPTED)  # noqa: SLF001
            )
            row.diagnostics = diagnostics
            row.status = "succeeded"
            durable_video = SimpleNamespace(storage_key=video_key)
            await session.commit()
        return fence, put_result.created, video_key

    (
        (fence_a, cleanup_result),
        (fence_b, worker_b_created, video_key),
    ) = await asyncio.gather(worker_a(), worker_b())

    assert fence_a.artifact_attempt_id == fence_b.artifact_attempt_id
    assert fence_a.owner_token != fence_b.owner_token
    assert worker_b_created is False
    assert worker_a_lease_lost.is_set() is False
    assert cleanup_result is False
    assert durable_generation.status == "succeeded"
    assert durable_video is not None
    assert durable_video.storage_key == video_key
    assert storage.get_bytes(video_key) == b"video"


@pytest.mark.asyncio
async def test_video_artifact_cleanup_fails_closed_without_durable_owner_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    generation = _finalization_generation()
    artifact_attempt_id = video_persistence.video_artifact_attempt_id(generation)
    video_key, _poster_key = video_persistence.video_artifact_keys(
        generation,
        ".mp4",
        artifact_attempt_id=artifact_attempt_id,
    )
    storage.put_bytes_result(video_key, b"video")
    fence = video_persistence._VideoArtifactFence(  # noqa: SLF001
        owner_token="owner-a",
        execution_epoch=generation.submission_epoch,
        attempt_epoch=generation.attempt,
        artifact_attempt_id=artifact_attempt_id,
    )

    class BrokenSession:
        async def __aenter__(self) -> BrokenSession:
            raise RuntimeError("database unavailable")

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            return None

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        for key in keys:
            storage.delete(key)

    monkeypatch.setattr(video_generation, "SessionLocal", BrokenSession)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)

    cleaned = await video_persistence._cleanup_video_artifacts_if_owned(  # noqa: SLF001
        (video_key,),
        generation_id=generation.id,
        fence=fence,
        lease_lost=asyncio.Event(),
    )

    assert cleaned is False
    assert storage.get_bytes(video_key) == b"video"


class _FinalizationSession:
    def __init__(
        self,
        *,
        on_refresh: object | None = None,
        fail_commit_number: int | None = None,
    ) -> None:
        self.on_refresh = on_refresh
        self.fail_commit_number = fail_commit_number
        self.commits = 0
        self.refreshes = 0
        self.added: list[object] = []
        self.flushes = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1
        if self.commits == self.fail_commit_number:
            raise RuntimeError("final commit failed")

    async def refresh(self, generation: object, *, with_for_update: bool) -> None:
        assert with_for_update is True
        self.refreshes += 1
        if callable(self.on_refresh):
            self.on_refresh(generation)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


async def _noop_async(*_args: object, **_kwargs: object) -> None:
    return None


async def _record_cleanup(
    deleted_keys: list[str],
    keys: tuple[str, ...] | list[str],
    **_kwargs: object,
) -> bool:
    deleted_keys.extend(keys)
    return True


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
        return StoredVideo(
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

    monkeypatch.setattr(
        video_persistence,
        "video_artifact_attempt_id",
        lambda _generation: "attempt-current",
    )
    monkeypatch.setattr(video_generation, "_store_video_asset", store)
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)
    monkeypatch.setattr(
        video_persistence,
        "_cleanup_video_artifacts_if_owned",
        lambda keys, **kwargs: _record_cleanup(deleted_keys, keys, **kwargs),
    )
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
    expected_deleted = (
        []
        if cancel_phase == "download"
        else ["u/user-1/v/video-1/final/attempt-current/output.mp4"]
    )
    assert deleted_keys == expected_deleted
    assert events == ["video.canceled"]


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [True, False])
async def test_video_store_lease_loss_retains_deterministic_attempt_artifact(
    monkeypatch: pytest.MonkeyPatch,
    created: bool,
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

    assert deleted_keys == []


@pytest.mark.asyncio
async def test_downloaded_video_store_lease_loss_retains_deterministic_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "downloaded.mp4"
    source.write_bytes(b"video")
    processed = video_artifacts.ProcessedVideoFile(
        path=source,
        mime="video/mp4",
        extension=".mp4",
        size_bytes=5,
        sha256="a" * 64,
        poster_bytes=None,
        faststart=True,
        metadata={},
        temporary=False,
    )
    downloaded = video_artifacts.DownloadedVideo(
        path=source,
        mime="video/mp4",
        extension=".mp4",
        size_bytes=5,
        temporary=False,
    )
    lease_lost = asyncio.Event()
    deleted_keys: list[str] = []

    async def copy_video(*_args: object, **_kwargs: object) -> bool:
        lease_lost.set()
        return True

    async def store_poster(*_args: object, **_kwargs: object) -> None:
        return None

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        deleted_keys.extend(keys)

    monkeypatch.setattr(
        video_generation,
        "_postprocess_video_file",
        lambda _downloaded: processed,
    )
    monkeypatch.setattr(
        video_persistence,
        "_copy_processed_video",
        copy_video,
    )
    monkeypatch.setattr(
        video_persistence,
        "_store_processed_poster",
        store_poster,
    )
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)

    with pytest.raises(video_generation._VideoLeaseLost):  # noqa: SLF001
        await video_generation._store_downloaded_video_asset(  # noqa: SLF001
            _finalization_generation(),
            downloaded,
            lease_lost=lease_lost,
            artifact_attempt_id="attempt-current",
        )

    assert deleted_keys == []


@pytest.mark.asyncio
async def test_finalization_terminal_race_retains_deterministic_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _finalization_generation()
    deleted_keys: list[str] = []
    current_key = "u/user-1/v/video-1/final/attempt-current/output.mp4"
    other_attempt_key = "u/user-1/v/video-1/final/attempt-other/output.mp4"
    refresh_calls = 0

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
        return StoredVideo(
            video=SimpleNamespace(id="stored-video"),
            diagnostics={},
            created_storage_keys=(current_key,),
        )

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        deleted_keys.extend(keys)

    def win_terminal_race(row: object) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            row.status = "failed"

    async def unexpected_billing(*_args: object, **_kwargs: object) -> object:
        pytest.fail("terminal loser must not bill")

    monkeypatch.setattr(
        video_persistence,
        "video_artifact_attempt_id",
        lambda _generation: "attempt-current",
    )
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
    assert session.commits == 2
    assert deleted_keys == []
    assert other_attempt_key not in deleted_keys


@pytest.mark.asyncio
async def test_finalization_lease_loss_after_store_defers_cleanup_to_sweeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = _finalization_generation()
    lease_lost = asyncio.Event()
    deleted_keys: list[str] = []
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
        lease_lost.set()
        return StoredVideo(
            video=SimpleNamespace(id="stored-video"),
            diagnostics={},
            created_storage_keys=(current_key,),
        )

    async def delete(keys: tuple[str, ...] | list[str]) -> None:
        deleted_keys.extend(keys)

    monkeypatch.setattr(
        video_persistence,
        "video_artifact_attempt_id",
        lambda _generation: "attempt-current",
    )
    monkeypatch.setattr(video_generation, "_store_video_asset", store)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)
    monkeypatch.setattr(video_generation, "_publish", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", _noop_async)

    session = _FinalizationSession()
    with pytest.raises(video_generation._VideoLeaseLost):  # noqa: SLF001
        await video_generation._finish_success(  # noqa: SLF001
            session,
            object(),
            generation,
            PollResult(
                status="succeeded",
                video_url="https://cdn.example/output.mp4",
            ),
            adapter=Adapter(),
            lease_lost=lease_lost,
        )

    assert session.rollbacks == 1
    assert deleted_keys == []


async def _run_finalization_commit_outcome(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome: ArtifactAdoption,
) -> tuple[Any, list[str], list[str], _FinalizationSession, BaseException | None]:
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
        return StoredVideo(
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

    async def probe(**_kwargs: object) -> ArtifactAdoption:
        return outcome

    monkeypatch.setattr(
        video_persistence,
        "video_artifact_attempt_id",
        lambda _generation: "attempt-current",
    )
    monkeypatch.setattr(video_generation, "_store_video_asset", store)
    monkeypatch.setattr(video_generation, "_video_for_generation", no_existing_video)
    monkeypatch.setattr(video_persistence, "_probe_video_success_adoption", probe)
    monkeypatch.setattr(video_generation, "resolve_video_billing", billing)
    monkeypatch.setattr(video_generation, "_delete_video_storage_keys", delete)
    monkeypatch.setattr(
        video_persistence,
        "_cleanup_video_artifacts_if_owned",
        lambda keys, **kwargs: _record_cleanup(deleted_keys, keys, **kwargs),
    )
    monkeypatch.setattr(video_generation, "_publish", _noop_async)
    monkeypatch.setattr(video_generation, "_release_provider_slot", _noop_async)
    monkeypatch.setattr(video_generation, "_queue_video_event", lambda *_a, **_k: None)

    session = _FinalizationSession(fail_commit_number=3)
    error: BaseException | None = None
    try:
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
    except BaseException as exc:  # noqa: BLE001
        error = exc
    return generation, deleted_keys, billing_reasons, session, error


@pytest.mark.asyncio
async def test_finalization_confirmed_non_adoption_cleans_created_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _generation,
        deleted_keys,
        billing_reasons,
        session,
        error,
    ) = await _run_finalization_commit_outcome(
        monkeypatch,
        outcome=ArtifactAdoption.NOT_ADOPTED,
    )

    current_key = "u/user-1/v/video-1/final/attempt-current/output.mp4"
    assert isinstance(error, RuntimeError)
    assert str(error) == "final commit failed"
    assert session.added
    assert billing_reasons == ["succeeded"]
    assert deleted_keys == [current_key]


@pytest.mark.asyncio
async def test_finalization_lost_commit_ack_keeps_adopted_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        generation,
        deleted_keys,
        billing_reasons,
        session,
        error,
    ) = await _run_finalization_commit_outcome(
        monkeypatch,
        outcome=ArtifactAdoption.ADOPTED,
    )

    assert generation.status == "succeeded"
    assert session.added
    assert session.flushes == 1
    assert billing_reasons == ["succeeded"]
    assert deleted_keys == []
    assert error is None


@pytest.mark.asyncio
async def test_finalization_unknown_commit_keeps_artifact_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        generation,
        deleted_keys,
        billing_reasons,
        session,
        error,
    ) = await _run_finalization_commit_outcome(
        monkeypatch,
        outcome=ArtifactAdoption.UNKNOWN,
    )

    assert generation.status == "succeeded"
    assert session.added
    assert billing_reasons == ["succeeded"]
    assert deleted_keys == []
    assert error is None
