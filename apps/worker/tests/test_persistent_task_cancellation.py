from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import inspect
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.reconciliation.contracts import ReconcileContext
from app.reconciliation.task_domains import (
    COMPLETION_RECONCILER,
    GENERATION_RECONCILER,
)
from app.task_cancellation import (
    DurableCancellationProbeUnavailable,
    bind_task_cancellation,
)
from app.tasks.completion_parts import failure_settlement, outcomes
from app.tasks.completion_parts import runner as completion_runner
from app.tasks.completion_parts import stream as completion_stream
from app.tasks.generation_parts import lease as generation_lease
from app.tasks.generation_parts import lifecycle as generation_lifecycle
from app.tasks.generation_parts import runner_dispatch_phase
from app.tasks.generation_parts.retry_state import generation_attempt_update
from app.tasks.generation_parts.runner_claim_phase import generation_cannot_start
from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    MessageStatus,
)
from lumen_core.models import Completion, Generation


class _RowResult:
    def __init__(self, row: Any) -> None:
        self.row = row

    def one_or_none(self) -> Any:
        return self.row


class _CancellationSession:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def __aenter__(self) -> _CancellationSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> _RowResult:
        return _RowResult(self.row)


class _CancellationSessionFactory:
    def __init__(self, row: Any) -> None:
        self.row = row

    def __call__(self) -> _CancellationSession:
        return _CancellationSession(self.row)


class _Redis:
    def __init__(self, value: Any = None, *, unavailable: bool = False) -> None:
        self.value = value
        self.unavailable = unavailable
        self.get_calls = 0

    async def get(self, _key: str) -> Any:
        self.get_calls += 1
        if self.unavailable:
            raise ConnectionError("redis unavailable")
        return self.value


def _durable_row(
    *,
    cancel_requested_at: datetime | None = None,
    task_status: str = GenerationStatus.RUNNING.value,
    message_status: str = MessageStatus.STREAMING.value,
    message_deleted_at: datetime | None = None,
    conversation_deleted_at: datetime | None = None,
    user_deleted_at: datetime | None = None,
) -> tuple[Any, ...]:
    return (
        cancel_requested_at,
        task_status,
        message_status,
        message_deleted_at,
        conversation_deleted_at,
        user_deleted_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redis", "row"),
    [
        (
            _Redis(unavailable=True),
            _durable_row(cancel_requested_at=datetime.now(timezone.utc)),
        ),
        (
            _Redis(value=None),
            _durable_row(cancel_requested_at=datetime.now(timezone.utc)),
        ),
        (
            _Redis(value=None),
            _durable_row(message_status=MessageStatus.CANCELED.value),
        ),
    ],
)
async def test_durable_probe_survives_redis_loss_or_expiry(
    redis: _Redis,
    row: tuple[Any, ...],
) -> None:
    with bind_task_cancellation(
        kind="generation",
        task_id="gen-1",
        model=Generation,
        session_factory=_CancellationSessionFactory(row),
        logger=logging.getLogger("test.durable-cancel"),
        poll_interval_s=60,
    ):
        assert (
            await generation_lease.is_cancelled(
                redis,
                "gen-1",
                force_db=True,
            )
            is True
        )


@pytest.mark.asyncio
async def test_stale_redis_notification_cannot_recancel_retried_task() -> None:
    redis = _Redis(value="1")
    with bind_task_cancellation(
        kind="generation",
        task_id="gen-1",
        model=Generation,
        session_factory=_CancellationSessionFactory(_durable_row()),
        logger=logging.getLogger("test.durable-cancel"),
        poll_interval_s=60,
    ):
        assert (
            await generation_lease.is_cancelled(
                redis,
                "gen-1",
                force_db=True,
            )
            is False
        )


@pytest.mark.asyncio
async def test_completion_probe_reads_durable_intent_after_redis_expiry() -> None:
    class Counter:
        def inc(self) -> None:
            return None

    with bind_task_cancellation(
        kind="completion",
        task_id="comp-1",
        model=Completion,
        session_factory=_CancellationSessionFactory(
            _durable_row(
                cancel_requested_at=datetime.now(timezone.utc),
                task_status=CompletionStatus.STREAMING.value,
            )
        ),
        logger=logging.getLogger("test.durable-cancel"),
        poll_interval_s=60,
    ):
        assert (
            await completion_stream._is_cancelled(
                _Redis(value=None),
                "comp-1",
                hooks=completion_stream.CancellationCheckHooks(
                    cancel_check_errors_total=Counter(),
                    logger=logging.getLogger("test.durable-cancel"),
                ),
                force_db=True,
            )
            is True
        )


class _UnavailableCancellationSession:
    async def __aenter__(self) -> _UnavailableCancellationSession:
        raise ConnectionError("database unavailable")

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _UnavailableCancellationSessionFactory:
    def __call__(self) -> _UnavailableCancellationSession:
        return _UnavailableCancellationSession()


@pytest.mark.asyncio
async def test_durable_probe_failure_is_unknown_not_user_cancel() -> None:
    with bind_task_cancellation(
        kind="generation",
        task_id="gen-unknown",
        model=Generation,
        session_factory=_UnavailableCancellationSessionFactory(),
        logger=logging.getLogger("test.durable-cancel"),
        poll_interval_s=60,
    ):
        with pytest.raises(DurableCancellationProbeUnavailable):
            await generation_lease.is_cancelled(
                _Redis(value=None),
                "gen-unknown",
                force_db=True,
            )


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _ScalarRows:
        return self

    def __iter__(self):
        return iter(self.rows)


class _ReconcileSession:
    def __init__(self, rows: list[Any], message: Any) -> None:
        self.rows = rows
        self.message = message

    async def execute(self, _statement: Any) -> _ScalarRows:
        return _ScalarRows(self.rows)

    async def get(self, _model: Any, _object_id: str) -> Any:
        return self.message


class _Billing:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name: str):
        async def call(_session: Any, _task: Any, *, reason: str, **_kwargs: Any):
            self.calls.append((name, reason))

        return call


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciler", "active_status", "terminal_status"),
    [
        (
            GENERATION_RECONCILER,
            GenerationStatus.RUNNING.value,
            GenerationStatus.CANCELED.value,
        ),
        (
            COMPLETION_RECONCILER,
            CompletionStatus.STREAMING.value,
            CompletionStatus.CANCELED.value,
        ),
    ],
)
async def test_reconciler_terminalizes_cancel_intent_after_lease_expiry(
    reconciler: Any,
    active_status: str,
    terminal_status: str,
) -> None:
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id="task-1",
        user_id="user-1",
        message_id="msg-1",
        status=active_status,
        progress_stage=active_status,
        attempt=1,
        cancel_requested_at=now,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=now - timedelta(minutes=3),
        upstream_request={},
    )
    message = SimpleNamespace(status=MessageStatus.STREAMING.value)
    session = _ReconcileSession([task], message)
    billing = _Billing()
    staged: list[dict[str, Any]] = []

    def stage_event(
        _session: Any,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        staged.append({"kind": kind, "payload": payload})
        return (kind, "pending", payload)

    context = ReconcileContext(
        redis=_Redis(value=None),
        session=session,
        now=now,
        billing=billing,
        logger=logging.getLogger("test.reconcile-cancel"),
        lease_unknowns=None,
        stage_event=stage_event,
    )

    first = await reconciler.reconcile(context)
    second = await reconciler.reconcile(context)

    assert first.touched == 1
    assert second.touched == 0
    assert task.status == terminal_status
    assert task.error_code == "cancelled"
    assert message.status == MessageStatus.FAILED.value
    assert billing.calls == [(reconciler.spec.release_method, "cancelled")]
    assert [event["kind"] for event in staged] == ["sse"]
    assert staged[0]["payload"]["event_name"] == reconciler.spec.failed_event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconciler", "active_status"),
    [
        (GENERATION_RECONCILER, GenerationStatus.RUNNING.value),
        (COMPLETION_RECONCILER, CompletionStatus.STREAMING.value),
    ],
)
async def test_reconciler_defers_cancel_while_worker_lease_is_active(
    reconciler: Any,
    active_status: str,
) -> None:
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id="task-active",
        user_id="user-1",
        message_id="msg-1",
        status=active_status,
        progress_stage=active_status,
        attempt=1,
        cancel_requested_at=now,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=now,
        upstream_request={},
    )
    billing = _Billing()
    context = ReconcileContext(
        redis=_Redis(value="worker:lease"),
        session=_ReconcileSession(
            [task],
            SimpleNamespace(status=MessageStatus.STREAMING.value),
        ),
        now=now,
        billing=billing,
        logger=logging.getLogger("test.reconcile-cancel-active"),
        lease_unknowns=None,
        stage_event=lambda _session, **kwargs: ("sse", "pending", kwargs),
    )

    result = await reconciler.reconcile(context)

    assert result.touched == 0
    assert result.pending_outbox == []
    assert task.status == active_status
    assert task.error_code is None
    assert billing.calls == []


@pytest.mark.asyncio
async def test_reconciler_defers_cancel_when_lease_state_is_unknown() -> None:
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id="task-unknown",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.RUNNING.value,
        progress_stage=GenerationStatus.RUNNING.value,
        attempt=1,
        cancel_requested_at=now,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=now,
        upstream_request={},
    )
    billing = _Billing()
    context = ReconcileContext(
        redis=_Redis(unavailable=True),
        session=_ReconcileSession(
            [task],
            SimpleNamespace(status=MessageStatus.STREAMING.value),
        ),
        now=now,
        billing=billing,
        logger=logging.getLogger("test.reconcile-cancel-unknown"),
        lease_unknowns=None,
        stage_event=lambda _session, **kwargs: ("sse", "pending", kwargs),
    )

    result = await GENERATION_RECONCILER.reconcile(context)

    assert result.touched == 0
    assert task.status == GenerationStatus.RUNNING.value
    assert billing.calls == []


@pytest.mark.asyncio
async def test_reconciler_settles_ambiguous_dispatch_on_cancel() -> None:
    now = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id="gen-dispatched",
        user_id="user-1",
        message_id="msg-1",
        status=GenerationStatus.RUNNING.value,
        progress_stage="rendering",
        attempt=1,
        cancel_requested_at=now,
        error_code=None,
        error_message=None,
        finished_at=None,
        updated_at=now - timedelta(minutes=3),
        upstream_request={"upstream_dispatch_started_at": "2026-07-30T08:00:00+00:00"},
    )
    session = _ReconcileSession(
        [task],
        SimpleNamespace(status=MessageStatus.STREAMING.value),
    )
    billing = _Billing()
    context = ReconcileContext(
        redis=_Redis(value=None),
        session=session,
        now=now,
        billing=billing,
        logger=logging.getLogger("test.reconcile-cancel"),
        lease_unknowns=None,
        stage_event=lambda _session, **kwargs: ("sse", "pending", kwargs),
    )

    result = await GENERATION_RECONCILER.reconcile(context)

    assert result.touched == 1
    assert billing.calls == [("settle_generation_unknown_upstream", "cancelled")]


def test_generation_state_transitions_reject_cancel_intent_by_default() -> None:
    guarded = str(
        generation_attempt_update(
            "gen-1",
            2,
            statuses=(GenerationStatus.RUNNING.value,),
        ).compile(dialect=postgresql.dialect())
    )
    cancellation = str(
        generation_attempt_update(
            "gen-1",
            2,
            statuses=(GenerationStatus.RUNNING.value,),
            allow_cancel_requested=True,
        ).compile(dialect=postgresql.dialect())
    )

    assert "generations.cancel_requested_at IS NULL" in guarded
    assert "generations.cancel_requested_at IS NULL" not in cancellation
    generation_cancel_source = inspect.getsource(
        generation_lifecycle.finalize_running_generation_cancel
    )
    completion_cancel_source = inspect.getsource(
        failure_settlement._cancel_completion_row
    )
    assert "Generation.cancel_requested_at.is_not(None)" in generation_cancel_source
    assert "Completion.cancel_requested_at.is_not(None)" in completion_cancel_source


def test_workers_gate_claim_success_retry_and_failure_on_cancel_intent() -> None:
    assert generation_cannot_start(
        SimpleNamespace(
            id="gen-1",
            status=GenerationStatus.QUEUED.value,
            cancel_requested_at=datetime.now(timezone.utc),
        )
    )

    claim_source = inspect.getsource(completion_runner.claim_completion)
    assert claim_source.index("cancel_requested_at") < claim_source.index(
        "completion.status = CompletionStatus.STREAMING.value"
    )

    success_source = inspect.getsource(outcomes._persist_success)
    retry_source = inspect.getsource(failure_settlement._mark_retry_queued)
    failure_source = inspect.getsource(failure_settlement._settle_terminal_failure)
    for source in (success_source, retry_source, failure_source):
        assert "Completion.cancel_requested_at.is_(None)" in source


class _GenerationMarkerResult:
    def __init__(self, row: Any) -> None:
        self.row = row

    def scalar_one_or_none(self) -> Any:
        return self.row

    def one_or_none(self) -> Any:
        return self.row


class _CancellationInterleavingMarkerSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    async def __aenter__(self) -> _CancellationInterleavingMarkerSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, statement: Any) -> _GenerationMarkerResult:
        self.statements.append(statement)
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        # The cancellation API commits after the pre-dispatch probe. A guarded
        # marker query must no longer own the row.
        row = (
            None
            if "generations.cancel_requested_at IS NULL" in sql
            else SimpleNamespace(upstream_request={})
        )
        return _GenerationMarkerResult(row)

    async def commit(self) -> None:
        self.commits += 1


class _TaskOnlyMarkerSession:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> _TaskOnlyMarkerSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> _GenerationMarkerResult:
        return _GenerationMarkerResult(SimpleNamespace(upstream_request={}))

    async def commit(self) -> None:
        self.commits += 1


class _DeletedAccountFenceSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0

    async def __aenter__(self) -> _DeletedAccountFenceSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, statement: Any) -> _GenerationMarkerResult:
        self.statements.append(statement)
        return _GenerationMarkerResult(
            SimpleNamespace(deleted_at=datetime.now(timezone.utc))
        )

    async def commit(self) -> None:
        self.commits += 1


class _DeletedAccountStore:
    def __init__(self) -> None:
        self.fence = _DeletedAccountFenceSession()
        self.calls = 0

    def session(self) -> Any:
        self.calls += 1
        if self.calls == 1:
            return self.fence
        raise AssertionError(f"unexpected generation session {self.calls}")


def _dispatch_marker_state(
    session: _CancellationInterleavingMarkerSession,
) -> SimpleNamespace:
    return SimpleNamespace(
        services=SimpleNamespace(
            store=SimpleNamespace(session=lambda: session),
            events=object(),
            provider=object(),
        ),
        generation=SimpleNamespace(execution_epoch=9),
        task_id="gen-1",
        user_id="user-1",
        attempt=2,
        gen_upstream_request_snapshot={},
    )


@pytest.mark.asyncio
async def test_cancel_commit_between_probe_and_marker_blocks_generation_dispatch() -> (
    None
):
    session = _CancellationInterleavingMarkerSession()
    state = _dispatch_marker_state(session)

    with pytest.raises(runner_dispatch_phase.StaleGenerationAttempt):
        await runner_dispatch_phase.record_generation_upstream_marker(
            state,
            response_received=False,
            fence_active_user=True,
        )

    assert session.commits == 0
    user_lock_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    marker_sql = str(
        session.statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "FROM users" in user_lock_sql
    assert "FOR UPDATE" in user_lock_sql
    assert "generations.cancel_requested_at IS NULL" in marker_sql


@pytest.mark.asyncio
async def test_proven_undelivered_marker_can_settle_cancelled_generation() -> None:
    session = _CancellationInterleavingMarkerSession()
    state = _dispatch_marker_state(session)

    await runner_dispatch_phase.record_generation_upstream_marker(
        state,
        response_received=False,
        proven_undelivered=True,
    )

    assert session.commits == 1
    assert (
        state.gen_upstream_request_snapshot["upstream_dispatch_delivery"]
        == "proven_undelivered"
    )
    marker_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "generations.cancel_requested_at IS NULL" not in marker_sql


@pytest.mark.asyncio
async def test_response_marker_can_settle_cancelled_generation() -> None:
    session = _CancellationInterleavingMarkerSession()
    state = _dispatch_marker_state(session)

    await runner_dispatch_phase.record_generation_upstream_marker(
        state,
        response_received=True,
    )

    assert session.commits == 1
    assert "upstream_response_received_at" in state.gen_upstream_request_snapshot
    marker_sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "generations.cancel_requested_at IS NULL" not in marker_sql


@pytest.mark.asyncio
async def test_deleted_account_blocks_atomic_dispatch_marker_and_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _DeletedAccountStore()
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=store,
            events=object(),
            provider=object(),
        ),
        generation=SimpleNamespace(execution_epoch=9),
        task_id="gen-1",
        user_id="user-1",
        attempt=2,
        gen_upstream_request_snapshot={},
        image_iter=None,
        task_deadline=asyncio.get_running_loop().time() + 1,
    )
    upstream_calls = 0

    async def pre_dispatch_probe_passes(_state: Any) -> None:
        return None

    def provider_must_not_start(_state: Any) -> Any:
        nonlocal upstream_calls
        upstream_calls += 1
        raise AssertionError("deleted account reached upstream provider")

    monkeypatch.setattr(
        runner_dispatch_phase,
        "raise_if_pre_upstream_interrupted",
        pre_dispatch_probe_passes,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "build_image_iterator",
        provider_must_not_start,
    )

    with pytest.raises(
        runner_dispatch_phase.TaskCancelled,
        match="account deleted before upstream dispatch",
    ):
        await runner_dispatch_phase.dispatch_upstream_request(state)

    assert store.calls == 1
    assert store.fence.commits == 0
    assert upstream_calls == 0
    assert len(store.fence.statements) == 1
    user_lock_sql = str(
        store.fence.statements[0].compile(dialect=postgresql.dialect())
    ).upper()
    assert "FROM USERS" in user_lock_sql
    assert "FOR UPDATE" in user_lock_sql


class _PreDispatchCancellationStore:
    def __init__(self) -> None:
        self.fence = _ActiveDispatchFenceSession()
        self.calls = 0

    def session(self) -> Any:
        self.calls += 1
        if self.calls == 1:
            return self.fence
        raise AssertionError(f"unexpected generation session {self.calls}")


@pytest.mark.asyncio
async def test_cancel_after_active_user_fence_precedes_dispatch_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _PreDispatchCancellationStore()
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=store,
            events=object(),
            provider=object(),
        ),
        generation=SimpleNamespace(execution_epoch=9),
        task_id="gen-1",
        user_id="user-1",
        attempt=2,
        gen_upstream_request_snapshot={},
        image_iter=None,
        task_deadline=asyncio.get_running_loop().time() + 1,
    )
    probes = 0
    upstream_calls = 0

    async def cancel_after_marker(_state: Any) -> None:
        nonlocal probes
        probes += 1
        if probes == 2:
            raise runner_dispatch_phase.TaskCancelled(
                "cancelled before provider iterator"
            )

    def provider_must_not_start(_state: Any) -> Any:
        nonlocal upstream_calls
        upstream_calls += 1
        raise AssertionError("cancelled task reached upstream provider")

    monkeypatch.setattr(
        runner_dispatch_phase,
        "raise_if_pre_upstream_interrupted",
        cancel_after_marker,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "build_image_iterator",
        provider_must_not_start,
    )

    with pytest.raises(
        runner_dispatch_phase.TaskCancelled,
        match="cancelled before provider iterator",
    ):
        await runner_dispatch_phase.dispatch_upstream_request(state)

    assert probes == 2
    assert upstream_calls == 0
    assert store.calls == 1
    assert store.fence.commits == 0
    assert store.fence.exited is True
    assert state.gen_upstream_request_snapshot == {}


class _ActiveDispatchFenceSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.commits = 0
        self.exited = False

    async def __aenter__(self) -> _ActiveDispatchFenceSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.exited = True

    async def execute(self, statement: Any) -> _GenerationMarkerResult:
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _GenerationMarkerResult(SimpleNamespace(deleted_at=None))
        return _GenerationMarkerResult(SimpleNamespace(upstream_request={}))

    async def commit(self) -> None:
        self.commits += 1


class _ProgressGuardSession:
    async def __aenter__(self) -> _ProgressGuardSession:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def execute(self, _statement: Any) -> _GenerationMarkerResult:
        return _GenerationMarkerResult((2, 9))


class _DispatchThenProgressStore:
    def __init__(self) -> None:
        self.preflight = _ActiveDispatchFenceSession()
        self.dispatch = _ActiveDispatchFenceSession()
        self.progress = _ProgressGuardSession()
        self.calls = 0

    def session(self) -> Any:
        self.calls += 1
        if self.calls == 1:
            return self.preflight
        if self.calls == 2:
            return self.dispatch
        if self.calls == 3:
            return self.progress
        raise AssertionError(f"unexpected generation session {self.calls}")


class _RecordingProgressPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def pop_provider_used_event(self) -> dict[str, str]:
        return {}


@pytest.mark.asyncio
async def test_generation_dispatch_releases_fence_before_progress_epoch_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _DispatchThenProgressStore()
    state = SimpleNamespace(
        services=SimpleNamespace(
            store=store,
            events=object(),
            provider=object(),
        ),
        generation=SimpleNamespace(execution_epoch=9),
        task_id="gen-1",
        user_id="user-1",
        attempt=2,
        gen_upstream_request_snapshot={},
        image_iter=None,
        lease_lost=asyncio.Event(),
        redis=object(),
        stage_timer=SimpleNamespace(set_ms=lambda *_args: None),
        task_deadline=asyncio.get_running_loop().time() + 1,
        action="generate",
        inpaint_size_override=None,
        resolved=SimpleNamespace(size="1024x1024"),
        reserved_provider_name=None,
    )
    inner_progress = _RecordingProgressPublisher()
    state.progress_publisher = runner_dispatch_phase._EpochGuardedProgressPublisher(
        state,
        inner_progress,
    )

    async def pre_dispatch_probe_passes(_state: Any) -> None:
        return None

    async def image_iter():
        assert store.preflight.commits == 0
        assert store.preflight.exited is True
        await state.progress_publisher({"type": "dispatch_ready"})
        assert store.dispatch.commits == 1
        assert store.dispatch.exited is True
        await state.progress_publisher(
            {"type": "provider_used", "provider": "test-provider"}
        )
        yield ("encoded-image", None)

    async def direct_anext(iterator: Any, *_args: Any, **_kwargs: Any) -> Any:
        return await iterator.__anext__()

    async def no_batch_extras(_state: Any) -> None:
        return None

    async def no_checkpoint(_state: Any) -> None:
        return None

    monkeypatch.setattr(
        runner_dispatch_phase,
        "raise_if_pre_upstream_interrupted",
        pre_dispatch_probe_passes,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "build_image_iterator",
        lambda _state: image_iter(),
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "anext_image_with_guards",
        direct_anext,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_winner_provider",
        lambda _state: None,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "consume_batch_extra_pairs",
        no_batch_extras,
    )
    monkeypatch.setattr(
        runner_dispatch_phase,
        "persist_generation_takeover_checkpoint",
        no_checkpoint,
    )

    await asyncio.wait_for(
        runner_dispatch_phase.dispatch_upstream_request(state),
        timeout=0.25,
    )

    assert store.calls == 3
    assert inner_progress.events == [
        {"type": "provider_used", "provider": "test-provider"}
    ]
    assert state.b64_result == "encoded-image"
