"""Internal adapter for the existing completion implementation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from .contracts import (
    ClaimResult,
    CompletionExecutionView,
    CompletionOutcome,
    CompletionServices,
    JsonObject,
    RetryResolution,
)
from .image_storage_runtime import CompletionToolImageService

if TYPE_CHECKING:
    from .execution import CompletionExecution


def _execution(view: CompletionExecutionView) -> "CompletionExecution":
    return cast("CompletionExecution", view)


class CompletionStream(Protocol):
    def __call__(
        self,
        body: dict[str, Any],
        *,
        runtime_override: Any | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class CompletionContextAdapter:
    DEFAULT_CHAT_MODEL: Any
    _inject_user_memory_context: Any
    _instructions_with_summary_guardrail: Any
    _pack_recent_history: Any
    _record_completion_context_metadata: Any
    runtime_settings: Any

    async def prepare(self, execution: CompletionExecutionView) -> None:
        from .runner import _prepare_request

        await _prepare_request(_execution(execution))


@dataclass(frozen=True, slots=True)
class CompletionToolAdapter:
    _CompletionToolTracker: Any
    _CompletionUsageAccumulator: Any
    _chat_tools_from_content: Any
    _completion_tool_images: Any
    _configure_chat_tools: Any
    _estimate_completion_request_input_tokens: Any
    _estimate_completion_tool_output_tokens: Any
    _extract_image_events_from_response: Any
    _extract_response_image_b64: Any
    _extract_response_revised_prompt: Any
    _publish_completion_tool_progress: Any
    _publish_completion_tool_updates: Any
    tool_image_service: CompletionToolImageService
    _summarize_tool_error: Any
    _tool_image_dedupe_key: Any
    _tool_limited_completion_body: Any

    def initialize(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        state.usage.tool_tracker = self._CompletionToolTracker()
        state.usage.usage_totals = self._CompletionUsageAccumulator()

    async def consume_event(
        self,
        execution: CompletionExecutionView,
        event: JsonObject,
    ) -> bool:
        from .runner import _handle_tool_call

        return await _handle_tool_call(
            _execution(execution),
            cast(dict[str, Any], event),
            allow_tool_limit=True,
        )

    async def finalize(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        if state.usage.tool_tracker is None:
            self.initialize(state)


@dataclass(frozen=True, slots=True)
class CompletionRepositoryAdapter:
    Completion: Any
    Message: Any
    SessionLocal: Any
    User: Any
    _acquire_completion_xact_lock: Any
    _cleanup_completion_runtime: Any
    _flush_completion_text: Any
    affected_rows: Any
    is_completion_terminal: Any
    new_uuid7: Any
    select: Any
    update: Any

    async def claim(self, execution: CompletionExecutionView) -> ClaimResult:
        from .runner import _claim_completion

        state = _execution(execution)
        claimed = await _claim_completion(state)
        return ClaimResult(
            claimed=claimed,
            outcome=CompletionOutcome(state.settlement.task_outcome),
        )

    async def flush_stream(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        await self._flush_completion_text(
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
        from .runner import _record_completion_upstream_marker

        await _record_completion_upstream_marker(
            _execution(execution),
            response_received=response_received,
        )

    async def queue_retry(self, execution: CompletionExecutionView) -> bool:
        state = _execution(execution)
        return state.settlement.task_outcome == CompletionOutcome.RETRY.value

    async def cleanup(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        await self._cleanup_completion_runtime(
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
class CompletionUpstreamAdapter:
    UpstreamError: Any
    _apply_url_citations: Any
    _completion_upstream_provider_event: Any
    _extract_completed_output_text: Any
    _extract_reasoning_delta: Any
    _extract_reasoning_text_from_response: Any
    _extract_url_citations: Any
    _finalize_completion_text: Any
    _merge_completion_upstream_metadata: Any
    _normalize_reasoning_effort_for_upstream: Any
    _raise_for_terminal_response_event: Any
    _record_completion_upstream_metadata: Any
    stream_completion: CompletionStream

    async def consume(self, execution: CompletionExecutionView) -> None:
        from .runner import _consume_stream

        await _consume_stream(_execution(execution))


@dataclass(frozen=True, slots=True)
class CompletionBillingAdapter:
    _fallback_completion_tool_image_tokens: Any
    _settle_cancelled_completion_billing: Any
    _settle_failed_completion_billing: Any
    byok_error_message: Any
    byok_error_to_generation_code: Any
    classify_user_credential_error: Any
    parse_usage: Any
    record_user_credential_runtime_error: Any
    resolve_user_credential_runtime: Any
    worker_billing: Any

    async def settle_success(self, execution: CompletionExecutionView) -> None:
        from .outcomes import settle_success

        await settle_success(_execution(execution))

    async def settle_cancelled(self, execution: CompletionExecutionView) -> None:
        from .runner import _settle_cancelled

        await _settle_cancelled(_execution(execution))

    async def settle_failure(
        self,
        execution: CompletionExecutionView,
        failure: BaseException,
    ) -> RetryResolution:
        from .runner import _failure_details, _handle_failure

        state = _execution(execution)
        decision, error_code, error_message = _failure_details(state, failure)
        await _handle_failure(state, failure)
        return RetryResolution(
            retriable=decision.retriable,
            reason=decision.reason,
            error_code=error_code,
            error_message=error_message,
            delay_seconds=None,
        )


@dataclass(frozen=True, slots=True)
class CompletionEventAdapter:
    _COMPLETION_EVENT_HOOKS: Any
    _completion_event_payload: Any
    _deliver_completion_event: Any
    _stage_completion_event: Any
    _tracer: Any
    logger: Any
    memory_extraction: Any
    publish_event: Any
    upstream_calls_total: Any

    async def publish_started(self, execution: CompletionExecutionView) -> None:
        from .runner import _publish_started

        await _publish_started(_execution(execution))

    def record_outcome(
        self,
        execution: CompletionExecutionView,
        outcome: CompletionOutcome,
    ) -> None:
        _execution(execution).settlement.task_outcome = outcome.value


@dataclass(frozen=True, slots=True)
class CompletionLeaseRetryAdapter:
    RETRY_BACKOFF_SECONDS: Any
    RetryDecision: Any
    _CANCEL_CHECK_EVERY_DELTAS: Any
    _CANCEL_POLL_INTERVAL_S: Any
    _CompletionEpochSuperseded: Any
    _LeaseLost: Any
    _MAX_ATTEMPTS: Any
    _MAX_TOOL_INVOCATIONS_DEFAULT: Any
    _PG_FLUSH_EVERY_CHARS: Any
    _RUNNING_COMPLETION_STATUSES: Any
    _TOOL_IDLE_TIMEOUT_S_DEFAULT: Any
    _TaskCancelled: Any
    _ToolIdleTimeout: Any
    _acquire_lease: Any
    _classify_exception: Any
    _completion_preflight_failure: Any
    _is_cancelled: Any
    _iter_completion_stream_with_abort: Any
    _lease_renewer: Any
    _raise_if_completion_cancelled: Any
    _watch_completion_cancel: Any
    completion_queue_metadata: Any
    merge_queue_metadata: Any

    async def start(self, execution: CompletionExecutionView) -> None:
        state = _execution(execution)
        if not state.settlement.lease_acquired:
            await self._acquire_lease(
                state.request.redis,
                state.request.task_id,
                state.request.lease_token,
            )

    def is_lease_lost(self, failure: BaseException) -> bool:
        return isinstance(failure, self._LeaseLost)

    def is_cancelled(self, failure: BaseException) -> bool:
        return isinstance(failure, self._TaskCancelled)

    def is_superseded(self, failure: BaseException) -> bool:
        return isinstance(failure, self._CompletionEpochSuperseded)

    async def enqueue_retry(
        self,
        execution: CompletionExecutionView,
        resolution: RetryResolution,
    ) -> bool:
        state = _execution(execution)
        return resolution.retriable and state.settlement.task_outcome == "retry"


@dataclass(frozen=True, slots=True)
class LegacyCompletionAdapter:
    context: CompletionContextAdapter
    tools: CompletionToolAdapter
    persistence: CompletionRepositoryAdapter
    upstream: CompletionUpstreamAdapter
    billing: CompletionBillingAdapter
    events: CompletionEventAdapter
    retry: CompletionLeaseRetryAdapter

    def services(self) -> CompletionServices:
        return CompletionServices(
            repository=self.persistence,
            context_builder=self.context,
            tool_executor=self.tools,
            upstream_client=self.upstream,
            billing=self.billing,
            events=self.events,
            lease_retry=self.retry,
        )
