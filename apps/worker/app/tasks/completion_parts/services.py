"""Behavior services composed over the legacy completion bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from .contracts import (
    ClaimResult,
    CompletionExecutionView,
    CompletionOutcome,
    CompletionServices,
    JsonObject,
    RetryResolution,
)
from .execution import CompletionExecution
from .legacy_adapter import LegacyCompletionAdapter
from .outcomes import settle_success
from .runner import (
    claim_completion,
    consume_completion_stream,
    handle_completion_failure,
    handle_completion_tool_call,
    prepare_completion_request,
    publish_completion_started,
    record_completion_upstream_marker,
    settle_cancelled_completion,
)


def _execution(view: CompletionExecutionView) -> CompletionExecution:
    return cast(CompletionExecution, view)


@dataclass(frozen=True, slots=True)
class CompletionRepositoryService:
    adapter: LegacyCompletionAdapter

    async def claim(self, execution: CompletionExecutionView) -> ClaimResult:
        state = _execution(execution)
        claimed = await claim_completion(state)
        return ClaimResult(
            claimed=claimed,
            outcome=CompletionOutcome(state.settlement.task_outcome),
        )

    async def flush_stream(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        await self.adapter.persistence._flush_completion_text(
            state.request.task_id,
            state.streaming.accumulated_text,
            attempt_epoch=state.preparation.attempt_epoch,
        )

    async def record_upstream_marker(
        self,
        execution: CompletionExecutionView,
        *,
        response_received: bool,
    ) -> None:
        await record_completion_upstream_marker(
            _execution(execution),
            response_received=response_received,
        )

    async def queue_retry(self, execution: CompletionExecutionView) -> bool:
        state = _execution(execution)
        return state.settlement.task_outcome == CompletionOutcome.RETRY.value

    async def cleanup(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        await self.adapter.persistence._cleanup_completion_runtime(
            redis=state.request.redis,
            task_id=state.request.task_id,
            lease_token=state.request.lease_token,
            lease_acquired=state.settlement.lease_acquired,
            renewer=state.settlement.renewer,
            cancel_stop_requested=state.settlement.cancel_stop_requested,
            cancel_watcher=state.settlement.cancel_watcher,
            stream_span_cm=state.settlement.stream_span_cm,
            task_start=state.request.task_start,
            task_outcome=state.settlement.task_outcome,
        )


@dataclass(frozen=True, slots=True)
class CompletionContextService:
    adapter: LegacyCompletionAdapter

    async def prepare(self, execution: CompletionExecutionView) -> None:
        await prepare_completion_request(_execution(execution))


@dataclass(frozen=True, slots=True)
class CompletionToolService:
    adapter: LegacyCompletionAdapter

    @property
    def tool_image_service(self):
        return self.adapter.tools.tool_image_service

    def initialize(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        state.usage.tool_tracker = self.adapter.tools._CompletionToolTracker()
        state.usage.usage_totals = self.adapter.tools._CompletionUsageAccumulator()

    async def consume_event(
        self,
        execution: CompletionExecutionView,
        event: JsonObject,
    ) -> bool:
        return await handle_completion_tool_call(
            _execution(execution),
            cast(dict[str, Any], event),
            allow_tool_limit=True,
        )

    async def finalize(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        if state.usage.tool_tracker is None:
            self.initialize(state)


@dataclass(frozen=True, slots=True)
class CompletionUpstreamService:
    adapter: LegacyCompletionAdapter

    @property
    def stream_completion(self):
        return self.adapter.upstream.stream_completion

    async def consume(self, execution: CompletionExecutionView) -> None:
        await consume_completion_stream(_execution(execution))


@dataclass(frozen=True, slots=True)
class CompletionBillingServiceAdapter:
    adapter: LegacyCompletionAdapter

    async def settle_success(self, execution: CompletionExecutionView) -> None:
        await settle_success(_execution(execution))

    async def settle_cancelled(self, execution: CompletionExecutionView) -> None:
        await settle_cancelled_completion(_execution(execution))

    async def settle_failure(
        self,
        execution: CompletionExecutionView,
        failure: BaseException,
    ) -> RetryResolution:
        state = _execution(execution)
        await handle_completion_failure(state, failure)
        return RetryResolution(
            retriable=state.settlement.task_outcome == CompletionOutcome.RETRY.value,
            reason=type(failure).__name__,
            error_code=str(
                getattr(failure, "error_code", None)
                or getattr(failure, "code", None)
                or type(failure).__name__
            ),
            error_message=str(failure)[:2000],
            delay_seconds=None,
        )


@dataclass(frozen=True, slots=True)
class CompletionEventService:
    adapter: LegacyCompletionAdapter

    async def publish_started(self, execution: CompletionExecutionView) -> None:
        await publish_completion_started(_execution(execution))

    def record_outcome(
        self,
        execution: CompletionExecutionView,
        outcome: CompletionOutcome,
    ) -> None:
        _execution(execution).settlement.task_outcome = outcome.value


@dataclass(frozen=True, slots=True)
class CompletionLeaseRetryService:
    adapter: LegacyCompletionAdapter

    async def start(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        if not state.settlement.lease_acquired:
            await self.adapter.retry._acquire_lease(
                state.request.redis,
                state.request.task_id,
                state.request.lease_token,
            )

    def is_lease_lost(self, failure: BaseException) -> bool:
        return isinstance(failure, self.adapter.retry._LeaseLost)

    def is_cancelled(self, failure: BaseException) -> bool:
        return isinstance(failure, self.adapter.retry._TaskCancelled)

    def is_superseded(self, failure: BaseException) -> bool:
        return isinstance(failure, self.adapter.retry._CompletionEpochSuperseded)

    async def enqueue_retry(
        self,
        execution: CompletionExecutionView,
        resolution: RetryResolution,
    ) -> bool:
        state = _execution(execution)
        return resolution.retriable and state.settlement.task_outcome == "retry"


def build_completion_services(
    adapter: LegacyCompletionAdapter,
) -> CompletionServices:
    return CompletionServices(
        repository=CompletionRepositoryService(adapter),
        context_builder=CompletionContextService(adapter),
        tool_executor=CompletionToolService(adapter),
        upstream_client=CompletionUpstreamService(adapter),
        billing=CompletionBillingServiceAdapter(adapter),
        events=CompletionEventService(adapter),
        lease_retry=CompletionLeaseRetryService(adapter),
    )
