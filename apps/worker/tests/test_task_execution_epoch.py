from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from arq.jobs import deserialize_job, serialize_job
from sqlalchemy.dialects import postgresql

from app import upstream_image_requests
from app.provider_runtime.errors import UpstreamError
from app.reconciliation import task_domains
from app.tasks.completion_parts import failure_settlement
from app.tasks.completion_parts import runner as completion_runner
from app.tasks.completion_parts import tool_images
from app.tasks.completion_parts.execution import CompletionRequest
from app.tasks.generation_parts import lease as generation_lease
from app.tasks.generation_parts import retry_state
from app.tasks.generation_parts import runner_claim_phase
from app.tasks.generation_parts import runner_dispatch_phase
from lumen_core.models import Completion
from lumen_core.upstream_billing import (
    LocalBillingAction,
    decide_dispatch_evidence_billing,
    mark_upstream_dispatch_proven_no_cost,
    mark_upstream_dispatch_proven_undelivered,
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
)


def test_generation_old_worker_cas_rejects_new_execution_epoch() -> None:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "generations",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("execution_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            table.insert().values(
                id="gen-1",
                attempt=1,
                execution_epoch=5,
                status="running",
                cancel_requested_at=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        stale = connection.execute(
            retry_state.generation_attempt_update(
                retry_state.generation_execution_task_id("gen-1", 4),
                1,
                statuses=("running",),
            ).values(status="failed")
        )
        current = connection.execute(
            retry_state.generation_attempt_update(
                retry_state.generation_execution_task_id("gen-1", 5),
                1,
                statuses=("running",),
            ).values(status="failed")
        )

    assert stale.rowcount == 0
    assert current.rowcount == 1


def test_generation_lease_identity_includes_execution_epoch_and_attempt() -> None:
    first = generation_lease.generation_lease_token(
        "worker:token",
        execution_epoch=4,
        attempt=1,
    )
    manual_retry = generation_lease.generation_lease_token(
        "worker:token",
        execution_epoch=5,
        attempt=1,
    )

    assert first != manual_retry
    assert "execution:4:attempt:1" in first
    assert "execution:5:attempt:1" in manual_retry


def test_generation_execution_task_id_survives_arq_serialization() -> None:
    task_id = retry_state.generation_execution_task_id("gen-1", 7)

    restored_job = deserialize_job(
        serialize_job(
            "run_generation",
            (task_id,),
            {},
            2,
            1_000,
        )
    )
    restored_task_id = restored_job.args[0]

    assert restored_task_id == "gen-1"
    assert retry_state.current_generation_execution_epoch(restored_task_id) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_request", "blocked"),
    [
        ({}, True),
        ({"upstream_dispatch_delivery": "proven_undelivered"}, False),
        (
            {
                "provider_idempotency_key": "provider-key",
                "provider_idempotency_stable": True,
            },
            False,
        ),
    ],
)
async def test_generation_claim_blocks_only_nonreplayable_dispatch_receipts(
    monkeypatch: pytest.MonkeyPatch,
    extra_request: dict[str, Any],
    blocked: bool,
) -> None:
    request = {
        "upstream_dispatch_started_at": "2026-08-02T08:00:00+00:00",
        "upstream_dispatch_execution_epoch": 7,
        **extra_request,
    }
    state = SimpleNamespace(
        task_id="gen-1",
        generation=SimpleNamespace(
            execution_epoch=7,
            attempt=2,
            upstream_request=request,
        ),
    )
    failures: list[dict[str, Any]] = []

    async def fail_queued(
        _state: Any,
        _session: Any,
        **kwargs: Any,
    ) -> None:
        failures.append(kwargs)

    monkeypatch.setattr(
        runner_claim_phase,
        "fail_queued_generation",
        fail_queued,
    )

    result = await runner_claim_phase.fail_nonreplayable_dispatch(
        state,
        object(),
        object(),
    )

    assert result is blocked
    assert bool(failures) is blocked
    if blocked:
        assert failures[0]["code"] == "result_unknown"
        assert failures[0]["next_attempt"] is None


def test_generation_provider_identity_rotates_only_between_execution_epochs() -> None:
    first = retry_state.generation_execution_trace_id("trace-1", 4)
    automatic_retry = retry_state.generation_execution_trace_id(first, 4)
    manual_retry = retry_state.generation_execution_trace_id(first, 5)
    body = {"model": "gpt-image-test", "prompt": "render"}
    first_key = upstream_image_requests._image_idempotency_key(
        trace_id=first,
        endpoint="images/generations",
        body=body,
    )
    automatic_retry_key = upstream_image_requests._image_idempotency_key(
        trace_id=automatic_retry,
        endpoint="images/generations",
        body=body,
    )
    manual_retry_key = upstream_image_requests._image_idempotency_key(
        trace_id=manual_retry,
        endpoint="images/generations",
        body=body,
    )

    assert first == "trace-1:execution:4"
    assert automatic_retry == first
    assert manual_retry == "trace-1:execution:5"
    assert automatic_retry_key == first_key
    assert manual_retry_key != first_key


def test_completion_existing_updates_are_epoch_fenced() -> None:
    async def noop_async(*_args: object, **_kwargs: object) -> None:
        return None

    @dataclass(frozen=True)
    class Persistence:
        Completion: Any
        SessionLocal: Any
        affected_rows: Any
        select: Any
        update: Any
        _flush_completion_text: Any

    @dataclass(frozen=True)
    class Context:
        _record_completion_context_metadata: Any

    @dataclass(frozen=True)
    class Upstream:
        UpstreamError: Any
        _merge_completion_upstream_metadata: Any
        _record_completion_upstream_metadata: Any

    @dataclass(frozen=True)
    class Events:
        _completion_event_payload: Any
        logger: Any

    @dataclass(frozen=True)
    class Ports:
        persistence: Any
        context: Any
        upstream: Any
        events: Any
        retry: Any

    ports = Ports(
        persistence=Persistence(
            Completion=Completion,
            SessionLocal=lambda: None,
            affected_rows=lambda result: result.rowcount,
            select=sa.select,
            update=sa.update,
            _flush_completion_text=noop_async,
        ),
        context=Context(_record_completion_context_metadata=noop_async),
        upstream=Upstream(
            UpstreamError=UpstreamError,
            _merge_completion_upstream_metadata=lambda request, **_kwargs: request,
            _record_completion_upstream_metadata=noop_async,
        ),
        events=Events(
            _completion_event_payload=lambda *args, **extra: {
                "args": args,
                **extra,
            },
            logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        ),
        retry=SimpleNamespace(
            _CompletionEpochSuperseded=_CompletionEpochSuperseded,
            _RUNNING_COMPLETION_STATUSES=("streaming",),
        ),
    )
    state = SimpleNamespace(ports=ports)

    task_domains.bind_completion_execution_fence(state, 9)

    statement = (
        state.ports.persistence.update(Completion)
        .where(Completion.id == "comp-1", Completion.attempt == 1)
        .values(status="failed")
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    payload = state.ports.events._completion_event_payload("comp-1")

    assert "completions.execution_epoch = 9" in sql
    assert payload["execution_epoch"] == 9


@pytest.mark.asyncio
async def test_completion_lease_owner_is_rebound_to_execution_epoch() -> None:
    calls: list[tuple[object, ...]] = []

    class Redis:
        async def eval(self, *args: object) -> int:
            calls.append(args)
            return 1

    async def renewer(*_args: object) -> None:
        await asyncio.Event().wait()

    async def old_renewer() -> None:
        await asyncio.Event().wait()

    old_task = asyncio.create_task(old_renewer())
    state = SimpleNamespace(
        request=CompletionRequest(
            redis=Redis(),
            task_id="comp-1",
            lease_token="worker:token",
            task_start=0.0,
            channel="task:comp-1",
        ),
        settlement=SimpleNamespace(
            renewer=old_task,
            lease_lost=asyncio.Event(),
        ),
        ports=SimpleNamespace(
            retry=SimpleNamespace(
                _lease_renewer=renewer,
                _LeaseLost=RuntimeError,
            )
        ),
    )

    await generation_lease.bind_task_lease_execution_epoch(state, 7)

    assert state.request.lease_token == "worker:token:execution:7"
    assert calls[0][2:] == (
        "task:comp-1:lease",
        "worker:token",
        "worker:token:execution:7",
    )
    assert old_task.done()
    assert state.settlement.renewer is not None
    state.settlement.renewer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await state.settlement.renewer


@pytest.mark.asyncio
async def test_generation_dispatch_without_response_becomes_result_unknown() -> None:
    state = SimpleNamespace(
        generation=SimpleNamespace(execution_epoch=3),
        gen_upstream_request_snapshot={
            "upstream_dispatch_started_at": "2026-08-03T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
        },
        attempt=1,
        image_iter=object(),
    )

    with pytest.raises(UpstreamError) as exc_info:
        await runner_dispatch_phase._raise_dispatch_failure(  # noqa: SLF001
            state,
            httpx.ReadTimeout("no response"),
        )

    assert exc_info.value.error_code == "image_job_result_unknown"
    assert exc_info.value.payload["execution_epoch"] == 3
    assert exc_info.value.payload["attempt"] == 1


@pytest.mark.asyncio
async def test_generation_explicit_error_response_requires_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool, bool]] = []
    state = SimpleNamespace(
        generation=SimpleNamespace(execution_epoch=3),
        gen_upstream_request_snapshot={
            "upstream_dispatch_started_at": "2026-08-03T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
        },
        attempt=1,
        image_iter=object(),
    )

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        proven_no_cost: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered, proven_no_cost))

    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_generation_upstream_marker",
        record_marker,
    )

    await runner_dispatch_phase._raise_dispatch_failure(  # noqa: SLF001
        state,
        UpstreamError(
            "provider rejected request",
            status_code=503,
            error_code="provider_error",
        ),
    )

    assert markers == [(True, False, False)]


@pytest.mark.asyncio
async def test_generation_direct_result_unknown_does_not_overwrite_response_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool, bool]] = []
    response_at = "2026-08-03T00:00:01+00:00"
    state = SimpleNamespace(
        generation=SimpleNamespace(execution_epoch=3),
        gen_upstream_request_snapshot={
            "upstream_dispatch_started_at": "2026-08-03T00:00:00+00:00",
            "upstream_dispatch_attempt": 1,
            "upstream_dispatch_execution_epoch": 3,
            "upstream_response_received_at": response_at,
            "upstream_response_attempt": 1,
            "upstream_response_execution_epoch": 3,
        },
        attempt=1,
        image_iter=object(),
    )

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        proven_no_cost: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered, proven_no_cost))

    monkeypatch.setattr(
        runner_dispatch_phase,
        "record_generation_upstream_marker",
        record_marker,
    )

    await runner_dispatch_phase._raise_dispatch_failure(  # noqa: SLF001
        state,
        UpstreamError(
            "successful response contained invalid JSON",
            status_code=200,
            error_code="direct_image_result_unknown",
            payload={
                "upstream_result_unknown": True,
                "response_received": True,
            },
        ),
    )

    assert markers == []
    assert state.gen_upstream_request_snapshot["upstream_response_received_at"] == (
        response_at
    )
    assert "upstream_dispatch_delivery" not in state.gen_upstream_request_snapshot


class _CompletionEpochSuperseded(RuntimeError):
    pass


class _TaskCancelled(RuntimeError):
    pass


class _MarkerResult:
    def scalar_one_or_none(self) -> None:
        return None


class _MarkerSession:
    def __init__(self, statements: list[Any]) -> None:
        self.statements = statements

    async def __aenter__(self) -> _MarkerSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, statement: Any) -> _MarkerResult:
        self.statements.append(statement)
        return _MarkerResult()


@pytest.mark.asyncio
async def test_completion_marker_cas_includes_execution_epoch() -> None:
    statements: list[Any] = []
    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1"),
        preparation=SimpleNamespace(
            attempt=1,
            queue_metadata_payload={"execution_epoch": 9},
        ),
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                SessionLocal=lambda: _MarkerSession(statements),
                select=sa.select,
                Completion=Completion,
            ),
            retry=SimpleNamespace(
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
            ),
        ),
    )

    with pytest.raises(_CompletionEpochSuperseded):
        await completion_runner.record_completion_upstream_marker(
            state,
            response_received=False,
        )

    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "completions.attempt = 1" in sql
    assert "completions.execution_epoch = 9" in sql


@pytest.mark.asyncio
async def test_completion_response_marker_is_not_blocked_by_cancel_intent() -> None:
    statements: list[Any] = []
    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1"),
        preparation=SimpleNamespace(
            attempt=1,
            queue_metadata_payload={"execution_epoch": 9},
        ),
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                SessionLocal=lambda: _MarkerSession(statements),
                select=sa.select,
                Completion=Completion,
            ),
            retry=SimpleNamespace(
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
            ),
        ),
    )

    with pytest.raises(_CompletionEpochSuperseded):
        await completion_runner.record_completion_upstream_marker(
            state,
            response_received=True,
        )

    sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "completions.cancel_requested_at IS NULL" not in sql


@pytest.mark.asyncio
async def test_completion_dispatch_without_response_becomes_result_unknown() -> None:
    state = SimpleNamespace(
        usage=SimpleNamespace(response_receipt_recorded=False),
        preparation=SimpleNamespace(queue_metadata_payload={}),
        ports=SimpleNamespace(
            retry=SimpleNamespace(
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            )
        ),
    )

    with pytest.raises(task_domains.CompletionDispatchResultUnknown):
        await task_domains.raise_completion_dispatch_failure(
            state,
            httpx.ReadTimeout("no response"),
        )


@pytest.mark.asyncio
async def test_completion_local_proxy_failure_precedes_dispatch_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool]] = []

    async def stream_completion(
        *_args: object,
        **_kwargs: object,
    ) -> Any:
        yield {
            "type": "provider_used",
            "provider": "test-provider",
        }
        raise RuntimeError("local SSH proxy initialization failed")

    async def iter_stream_with_abort(stream: Any, **_kwargs: object) -> Any:
        async for event in stream:
            yield event

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered))

    monkeypatch.setattr(
        completion_runner,
        "record_completion_upstream_marker",
        record_marker,
    )

    async def ensure_current(_state: object) -> None:
        return None

    monkeypatch.setattr(
        completion_runner,
        "_ensure_completion_execution_current",
        ensure_current,
    )
    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1"),
        preparation=SimpleNamespace(
            attempt=1,
            queue_metadata_payload={},
            runtime_override=None,
            fast_mode=False,
        ),
        settlement=SimpleNamespace(
            cancel_requested=asyncio.Event(),
            lease_lost=asyncio.Event(),
        ),
        streaming=SimpleNamespace(tool_idle_timeout_s=30.0),
        usage=SimpleNamespace(
            dispatch_started_recorded=False,
            response_receipt_recorded=False,
            active_round_dispatch_started=False,
            active_round_response_received=False,
            active_round_dispatch_proven_undelivered=False,
            upstream_provider_event=None,
            tool_tracker=object(),
        ),
        ports=SimpleNamespace(
            upstream=SimpleNamespace(
                stream_completion=stream_completion,
                _completion_upstream_provider_event=lambda _event: None,
            ),
            retry=SimpleNamespace(
                _iter_completion_stream_with_abort=iter_stream_with_abort,
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            ),
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="local SSH proxy initialization failed",
    ):
        await completion_runner._consume_round(  # noqa: SLF001
            state,
            {},
            phase="primary",
            allow_tool_limit=True,
            track_tool_calls=True,
            append_completed_text=False,
            finalize_tools=False,
        )

    assert markers == []
    assert state.usage.active_round_dispatch_started is False


@pytest.mark.asyncio
async def test_completion_provider_used_then_cancel_before_dispatch_releases_without_input_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[bool] = []

    async def stream_completion(
        *_args: object,
        **_kwargs: object,
    ) -> Any:
        yield {
            "type": "provider_used",
            "provider": "test-provider",
        }
        raise _TaskCancelled("cancelled before upstream dispatch")

    async def iter_stream_with_abort(stream: Any, **_kwargs: object) -> Any:
        async for event in stream:
            yield event

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        **_kwargs: object,
    ) -> None:
        markers.append(response_received)

    async def record_provider_metadata(**_kwargs: object) -> None:
        return None

    async def ensure_current(_state: object) -> None:
        return None

    monkeypatch.setattr(
        completion_runner,
        "record_completion_upstream_marker",
        record_marker,
    )
    monkeypatch.setattr(
        completion_runner,
        "_ensure_completion_execution_current",
        ensure_current,
    )

    completion_row = SimpleNamespace(
        execution_epoch=7,
        upstream_request={},
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _model: Any, _task_id: str) -> Any:
            return completion_row

    usage_totals = tool_images._CompletionUsageAccumulator()  # noqa: SLF001
    usage_totals.start_round(input_fallback_tokens=37)
    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1"),
        preparation=SimpleNamespace(
            attempt=1,
            attempt_epoch=1,
            queue_metadata_payload={"execution_epoch": 7},
            runtime_override=None,
            fast_mode=False,
        ),
        settlement=SimpleNamespace(
            cancel_requested=asyncio.Event(),
            lease_lost=asyncio.Event(),
        ),
        streaming=SimpleNamespace(
            tool_idle_timeout_s=30.0,
            has_partial=False,
            tool_images=[],
        ),
        usage=SimpleNamespace(
            request_sent=False,
            usage_totals=usage_totals,
            dispatch_started_recorded=False,
            response_receipt_recorded=False,
            active_round_dispatch_started=False,
            active_round_response_received=False,
            active_round_dispatch_proven_undelivered=False,
            upstream_provider_event=None,
            tool_tracker=object(),
        ),
        ports=SimpleNamespace(
            upstream=SimpleNamespace(
                stream_completion=stream_completion,
                _completion_upstream_provider_event=lambda event: {
                    "provider": str(event["provider"])
                },
                _record_completion_upstream_metadata=record_provider_metadata,
            ),
            retry=SimpleNamespace(
                _iter_completion_stream_with_abort=iter_stream_with_abort,
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            ),
            persistence=SimpleNamespace(
                SessionLocal=Session,
                Completion=Completion,
            ),
        ),
    )

    with pytest.raises(_TaskCancelled):
        await completion_runner._consume_round(  # noqa: SLF001
            state,
            {},
            phase="primary",
            allow_tool_limit=True,
            track_tool_calls=True,
            append_completed_text=False,
            finalize_tools=False,
        )

    usage_totals.finish_round()

    assert markers == []
    assert state.usage.request_sent is False
    assert state.usage.active_round_dispatch_started is False
    assert usage_totals.tokens_in == 0
    assert (
        await task_domains.completion_cancel_requires_unknown_settlement(state)
        is False
    )
    assert (
        decide_dispatch_evidence_billing(
            completion_row,
            actual_cost_known=False,
            execution_epoch=7,
        ).action
        is LocalBillingAction.RELEASE
    )


@pytest.mark.asyncio
async def test_completion_connect_error_before_receipt_is_proven_undelivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool]] = []

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered))

    monkeypatch.setattr(
        task_domains,
        "record_completion_upstream_marker",
        record_marker,
    )
    usage_totals = tool_images._CompletionUsageAccumulator()  # noqa: SLF001
    usage_totals.start_round(input_fallback_tokens=41)
    usage_totals.mark_round_dispatched()
    state = SimpleNamespace(
        usage=SimpleNamespace(
            response_receipt_recorded=False,
            active_round_dispatch_proven_undelivered=False,
            usage_totals=usage_totals,
        ),
        preparation=SimpleNamespace(queue_metadata_payload={}),
        ports=SimpleNamespace(
            retry=SimpleNamespace(
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            )
        ),
    )

    await task_domains.raise_completion_dispatch_failure(
        state,
        httpx.ConnectError("connection failed before request delivery"),
    )
    usage_totals.finish_round()

    assert state.usage.response_receipt_recorded is False
    assert state.usage.active_round_dispatch_proven_undelivered is True
    assert usage_totals.tokens_in == 0
    assert markers == [(False, True)]


@pytest.mark.asyncio
async def test_completion_explicit_error_response_requires_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[tuple[bool, bool, bool]] = []

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
        proven_no_cost: bool = False,
    ) -> None:
        markers.append((response_received, proven_undelivered, proven_no_cost))

    monkeypatch.setattr(
        task_domains,
        "record_completion_upstream_marker",
        record_marker,
    )
    state = SimpleNamespace(
        usage=SimpleNamespace(
            response_receipt_recorded=False,
            active_round_dispatch_proven_undelivered=False,
        ),
        preparation=SimpleNamespace(queue_metadata_payload={}),
        ports=SimpleNamespace(
            retry=SimpleNamespace(
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            )
        ),
    )

    await task_domains.raise_completion_dispatch_failure(
        state,
        UpstreamError(
            "provider rejected request",
            status_code=503,
            error_code="provider_error",
        ),
    )

    assert state.usage.response_receipt_recorded is True
    assert state.usage.active_round_response_received is True
    assert state.usage.active_round_dispatch_proven_undelivered is False
    assert markers == [(True, False, False)]


@pytest.mark.asyncio
async def test_completion_provider_used_then_pre_delta_interruption_settles_unknown_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[bool] = []
    settlements: list[str] = []

    class Redis:
        def __init__(self) -> None:
            self.retry_enqueues = 0

        async def enqueue_job(self, *_args: object, **_kwargs: object) -> None:
            self.retry_enqueues += 1

    async def stream_completion(*_args: object, **_kwargs: object) -> Any:
        yield {
            "type": "provider_used",
            "provider": "provider-a",
            "route": "responses",
        }
        await _kwargs["on_dispatch_ready"]()  # type: ignore[operator]
        raise httpx.ReadTimeout("SSE disconnected before first delta")

    async def iter_stream_with_abort(stream: Any, **_kwargs: object) -> Any:
        async for event in stream:
            yield event

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
    ) -> None:
        assert proven_undelivered is False
        markers.append(response_received)

    async def record_provider_metadata(**_kwargs: object) -> None:
        return None

    async def ensure_current(_state: object) -> None:
        return None

    async def settle_unknown(_state: object) -> None:
        settlements.append("dispatch-result-unknown")

    monkeypatch.setattr(
        completion_runner, "record_completion_upstream_marker", record_marker
    )
    monkeypatch.setattr(
        completion_runner, "_ensure_completion_execution_current", ensure_current
    )
    monkeypatch.setattr(
        failure_settlement,
        "settle_completion_result_unknown",
        settle_unknown,
    )

    redis = Redis()
    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1", redis=redis),
        preparation=SimpleNamespace(
            attempt=1,
            attempt_epoch=1,
            queue_metadata_payload={},
            runtime_override=None,
            fast_mode=False,
        ),
        settlement=SimpleNamespace(
            cancel_requested=asyncio.Event(),
            lease_lost=asyncio.Event(),
        ),
        streaming=SimpleNamespace(tool_idle_timeout_s=30.0),
        usage=SimpleNamespace(
            dispatch_started_recorded=False,
            response_receipt_recorded=False,
            upstream_provider_event=None,
            tool_tracker=object(),
        ),
        ports=SimpleNamespace(
            upstream=SimpleNamespace(
                stream_completion=stream_completion,
                _completion_upstream_provider_event=lambda event: {
                    "provider": str(event["provider"])
                },
                _record_completion_upstream_metadata=record_provider_metadata,
            ),
            retry=SimpleNamespace(
                _iter_completion_stream_with_abort=iter_stream_with_abort,
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            ),
        ),
    )

    with pytest.raises(task_domains.CompletionDispatchResultUnknown) as exc_info:
        await completion_runner._consume_round(  # noqa: SLF001
            state,
            {},
            phase="primary",
            allow_tool_limit=True,
            track_tool_calls=True,
            append_completed_text=False,
            finalize_tools=False,
        )

    await failure_settlement.handle_completion_failure(state, exc_info.value)

    assert markers == [False]
    assert state.usage.response_receipt_recorded is False
    assert settlements == ["dispatch-result-unknown"]
    assert redis.retry_enqueues == 0


@pytest.mark.asyncio
async def test_completion_tool_limit_fallback_cancel_after_primary_response_settles_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[bool] = []
    provider_events: list[dict[str, str]] = []
    settlements: list[str] = []
    releases: list[str] = []

    async def stream_completion(body: dict[str, str], **_kwargs: object) -> Any:
        if body["round"] == "primary":
            yield {"type": "response.created"}
            yield {"type": "tool.call"}
            return
        yield {
            "type": "provider_used",
            "provider": "provider-b",
            "route": "responses",
        }
        await _kwargs["on_dispatch_ready"]()  # type: ignore[operator]
        raise _TaskCancelled("cancelled during fallback")

    async def iter_stream_with_abort(stream: Any, **_kwargs: object) -> Any:
        async for event in stream:
            yield event

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
    ) -> None:
        assert proven_undelivered is False
        markers.append(response_received)

    async def record_provider_metadata(**kwargs: object) -> None:
        provider_events.append(kwargs["provider_event"])  # type: ignore[arg-type]

    async def ensure_current(_state: object) -> None:
        return None

    async def handle_tool_call(
        _state: object,
        event: dict[str, str],
        *,
        allow_tool_limit: bool,
    ) -> bool:
        return allow_tool_limit and event["type"] == "tool.call"

    async def settle_unknown(_state: object) -> None:
        settlements.append("unknown")

    async def settle_cancelled(_state: object) -> None:
        releases.append("release")

    async def publish_thinking(*_args: object, **_kwargs: object) -> None:
        return None

    async def store_image(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        completion_runner,
        "record_completion_upstream_marker",
        record_marker,
    )
    monkeypatch.setattr(
        completion_runner,
        "_ensure_completion_execution_current",
        ensure_current,
    )
    monkeypatch.setattr(
        failure_settlement,
        "ensure_completion_execution_current",
        ensure_current,
    )
    monkeypatch.setattr(
        completion_runner,
        "handle_completion_tool_call",
        handle_tool_call,
    )
    monkeypatch.setattr(completion_runner, "_publish_thinking", publish_thinking)
    monkeypatch.setattr(completion_runner, "_store_image_event", store_image)
    monkeypatch.setattr(
        failure_settlement,
        "settle_completion_cancel_unknown",
        settle_unknown,
    )

    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1", redis=object()),
        preparation=SimpleNamespace(
            attempt=1,
            attempt_epoch=1,
            queue_metadata_payload={},
            runtime_override=None,
            fast_mode=False,
        ),
        settlement=SimpleNamespace(
            cancel_requested=asyncio.Event(),
            lease_lost=asyncio.Event(),
        ),
        streaming=SimpleNamespace(tool_idle_timeout_s=30.0),
        usage=SimpleNamespace(
            dispatch_started_recorded=False,
            response_receipt_recorded=False,
            upstream_provider_event=None,
            tool_tracker=object(),
        ),
        ports=SimpleNamespace(
            upstream=SimpleNamespace(
                stream_completion=stream_completion,
                _completion_upstream_provider_event=lambda event: {
                    "provider": str(event["provider"])
                },
                _extract_reasoning_delta=lambda _event: "",
                _record_completion_upstream_metadata=record_provider_metadata,
            ),
            retry=SimpleNamespace(
                _iter_completion_stream_with_abort=iter_stream_with_abort,
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            ),
            events=SimpleNamespace(
                logger=SimpleNamespace(info=lambda *_args, **_kwargs: None)
            ),
        ),
    )

    await completion_runner._consume_round(  # noqa: SLF001
        state,
        {"round": "primary"},
        phase="primary",
        allow_tool_limit=True,
        track_tool_calls=True,
        append_completed_text=False,
        finalize_tools=False,
    )
    assert state.usage.response_receipt_recorded is True

    with pytest.raises(_TaskCancelled) as exc_info:
        await completion_runner._consume_round(  # noqa: SLF001
            state,
            {"round": "fallback"},
            phase="fallback",
            allow_tool_limit=False,
            track_tool_calls=False,
            append_completed_text=True,
            finalize_tools=True,
        )

    services = SimpleNamespace(
        lease_retry=SimpleNamespace(
            is_lease_lost=lambda _failure: False,
            is_superseded=lambda _failure: False,
            is_cancelled=lambda failure: isinstance(failure, _TaskCancelled),
        ),
        billing=SimpleNamespace(settle_cancelled=settle_cancelled),
        events=SimpleNamespace(record_outcome=lambda *_args, **_kwargs: None),
    )
    await failure_settlement.handle_completion_run_failure(
        SimpleNamespace(task_id="comp-1"),
        state,
        services,
        exc_info.value,
    )

    assert markers == [False, True]
    assert provider_events == [{"provider": "provider-b"}]
    assert state.usage.response_receipt_recorded is True
    assert state.usage.active_round_dispatch_started is True
    assert state.usage.active_round_response_received is False
    assert settlements == ["unknown"]
    assert releases == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_request", "usage_value", "expected"),
    [
        ({}, 0, False),
        (
            mark_upstream_dispatch_started(
                {},
                at="2026-08-03T00:00:00+00:00",
                attempt=1,
                execution_epoch=7,
            ),
            0,
            True,
        ),
        (
            mark_upstream_response_received(
                {},
                at="2026-08-03T00:00:01+00:00",
                attempt=1,
                execution_epoch=7,
            ),
            0,
            True,
        ),
        (
            mark_upstream_dispatch_proven_undelivered(
                {},
                at="2026-08-03T00:00:00+00:00",
                attempt=1,
                execution_epoch=7,
            ),
            0,
            False,
        ),
        (
            mark_upstream_dispatch_proven_no_cost(
                {},
                at="2026-08-03T00:00:00+00:00",
                attempt=1,
                execution_epoch=7,
            ),
            0,
            False,
        ),
        (
            mark_upstream_dispatch_started(
                {},
                at="2026-08-03T00:00:00+00:00",
                attempt=1,
                execution_epoch=7,
            ),
            3,
            False,
        ),
    ],
)
async def test_completion_cancel_uses_durable_dispatch_evidence(
    upstream_request: dict[str, object],
    usage_value: int,
    expected: bool,
) -> None:
    completion_row = SimpleNamespace(
        execution_epoch=7,
        upstream_request=upstream_request,
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _model: Any, _task_id: str) -> Any:
            return completion_row

    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1"),
        preparation=SimpleNamespace(
            queue_metadata_payload={"execution_epoch": 7},
        ),
        streaming=SimpleNamespace(has_partial=False, tool_images=[]),
        usage=SimpleNamespace(
            request_sent=False,
            usage_totals=SimpleNamespace(values=lambda: (usage_value,)),
        ),
        ports=SimpleNamespace(
            persistence=SimpleNamespace(
                SessionLocal=Session,
                Completion=Completion,
            )
        ),
    )

    assert (
        await task_domains.completion_cancel_requires_unknown_settlement(state)
    ) is expected


@pytest.mark.asyncio
async def test_completion_tool_limit_fallback_connect_error_after_primary_receipt_settles_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markers: list[bool] = []
    settlements: list[str] = []
    provider_events: list[dict[str, str]] = []

    class Redis:
        def __init__(self) -> None:
            self.retry_enqueues = 0

        async def enqueue_job(self, *_args: object, **_kwargs: object) -> None:
            self.retry_enqueues += 1

    async def stream_completion(body: dict[str, str], **_kwargs: object) -> Any:
        if body["round"] == "primary":
            yield {"type": "response.created"}
            yield {"type": "tool.call"}
            return
        yield {
            "type": "provider_used",
            "provider": "provider-b",
            "route": "responses",
        }
        raise httpx.ConnectError("fallback connection failed before request delivery")

    async def iter_stream_with_abort(stream: Any, **_kwargs: object) -> Any:
        async for event in stream:
            yield event

    async def record_marker(
        _state: object,
        *,
        response_received: bool,
        proven_undelivered: bool = False,
    ) -> None:
        assert proven_undelivered is False
        markers.append(response_received)

    async def record_provider_metadata(**kwargs: object) -> None:
        provider_events.append(kwargs["provider_event"])  # type: ignore[arg-type]

    async def ensure_current(_state: object) -> None:
        return None

    async def handle_tool_call(
        _state: object,
        event: dict[str, str],
        *,
        allow_tool_limit: bool,
    ) -> bool:
        if allow_tool_limit and event["type"] == "tool.call":
            _state.streaming.tool_loop_truncated = True  # type: ignore[attr-defined]
            return True
        return False

    async def publish_thinking(*_args: object, **_kwargs: object) -> None:
        return None

    async def store_image(*_args: object, **_kwargs: object) -> None:
        return None

    async def settle_unknown(_state: object) -> None:
        settlements.append("dispatch-result-unknown")

    monkeypatch.setattr(
        completion_runner, "record_completion_upstream_marker", record_marker
    )
    monkeypatch.setattr(
        completion_runner,
        "_ensure_completion_execution_current",
        ensure_current,
    )
    monkeypatch.setattr(
        completion_runner, "handle_completion_tool_call", handle_tool_call
    )
    monkeypatch.setattr(completion_runner, "_publish_thinking", publish_thinking)
    monkeypatch.setattr(completion_runner, "_store_image_event", store_image)
    monkeypatch.setattr(
        failure_settlement,
        "settle_completion_result_unknown",
        settle_unknown,
    )

    redis = Redis()
    state = SimpleNamespace(
        request=SimpleNamespace(task_id="comp-1", redis=redis),
        preparation=SimpleNamespace(
            attempt=1,
            attempt_epoch=1,
            queue_metadata_payload={},
            runtime_override=None,
            fast_mode=False,
        ),
        settlement=SimpleNamespace(
            cancel_requested=asyncio.Event(),
            lease_lost=asyncio.Event(),
        ),
        streaming=SimpleNamespace(
            tool_idle_timeout_s=30.0,
            tool_loop_truncated=False,
        ),
        usage=SimpleNamespace(
            dispatch_started_recorded=False,
            response_receipt_recorded=False,
            upstream_provider_event=None,
            tool_tracker=object(),
        ),
        ports=SimpleNamespace(
            upstream=SimpleNamespace(
                stream_completion=stream_completion,
                _completion_upstream_provider_event=lambda event: {
                    "provider": str(event["provider"])
                },
                _extract_reasoning_delta=lambda _event: "",
                _record_completion_upstream_metadata=record_provider_metadata,
            ),
            retry=SimpleNamespace(
                _iter_completion_stream_with_abort=iter_stream_with_abort,
                _CompletionEpochSuperseded=_CompletionEpochSuperseded,
                _TaskCancelled=_TaskCancelled,
            ),
        ),
    )

    await completion_runner._consume_round(  # noqa: SLF001
        state,
        {"round": "primary"},
        phase="primary",
        allow_tool_limit=True,
        track_tool_calls=True,
        append_completed_text=False,
        finalize_tools=False,
    )
    assert state.usage.response_receipt_recorded is True
    assert state.streaming.tool_loop_truncated is True

    with pytest.raises(task_domains.CompletionDispatchResultUnknown) as exc_info:
        await completion_runner._consume_round(  # noqa: SLF001
            state,
            {"round": "fallback"},
            phase="fallback",
            allow_tool_limit=False,
            track_tool_calls=False,
            append_completed_text=True,
            finalize_tools=True,
        )

    await failure_settlement.handle_completion_failure(state, exc_info.value)

    assert markers == [False, True]
    assert state.usage.response_receipt_recorded is True
    assert provider_events == [{"provider": "provider-b"}]
    assert state.usage.active_round_dispatch_proven_undelivered is False
    assert settlements == ["dispatch-result-unknown"]
    assert redis.retry_enqueues == 0
