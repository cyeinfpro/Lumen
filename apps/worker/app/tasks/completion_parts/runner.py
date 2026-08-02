"""Completion task execution phases.

This module contains the mutable orchestration state and the ARQ task phases.
It receives all external dependencies through the explicit completion runtime.
"""

from __future__ import annotations

from .contracts import (
    CompletionCommand,
    CompletionOutcome,
    CompletionPhase,
    CompletionResult,
    CompletionServices,
)
from .execution import CompletionExecution, CompletionRequest
from .bindings import CompletionBindings
from .failure_settlement import (
    handle_completion_failure,
    handle_completion_run_failure,
    settle_cancelled_completion,
)
from .request_context import load_request_context
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import (
    EV_COMP_DELTA,
    EV_COMP_PROGRESS,
    EV_COMP_RESTARTED,
    EV_COMP_STARTED,
    EV_COMP_THINKING_DELTA,
    CompletionStage,
    CompletionStatus,
    task_channel,
)
from lumen_core.chat_tools import ToolStatus, normalize_tool_idle_timeout_seconds
from lumen_core.models import Completion
from lumen_core.upstream_billing import has_stable_provider_idempotency_key

from ...reconciliation.task_domains import (
    COMPLETION_EXECUTION_EPOCH_KEY as _EXECUTION_EPOCH_KEY,
    COMPLETION_RESULT_UNKNOWN_MESSAGE,
    CompletionDispatchResultUnknown,
    bind_completion_execution_fence,
    completion_execution_epoch as _completion_execution_epoch,
    ensure_completion_execution_current as _ensure_completion_execution_current,
    raise_completion_dispatch_failure as _raise_completion_dispatch_failure,
    record_completion_upstream_marker,
    stage_completion_preflight_failure as _stage_preflight_failure,
)
from ...task_cancellation import bind_task_cancellation
from ..generation_parts.lease import bind_task_lease_execution_epoch


_STABLE_PROVIDER_IDEMPOTENCY_KEY = "_stable_provider_idempotency"


@dataclass(slots=True)
class _CompletionRoundReceipt:
    """Dispatch/response evidence for one upstream stream invocation."""

    dispatch_started_recorded: bool = False
    response_received: bool = False


def _new_execution(
    command: CompletionCommand,
    ports: CompletionBindings,
    services: CompletionServices,
    *,
    execution_epoch: int | None = None,
) -> CompletionExecution:
    lease_suffix = (
        f":execution:{max(0, int(execution_epoch))}"
        if execution_epoch is not None
        else ""
    )
    state = CompletionExecution(
        ports=ports,
        services=services,
        request=CompletionRequest(
            redis=command.redis,
            task_id=command.task_id,
            lease_token=(
                f"{command.worker_id}:{ports.persistence.new_uuid7()}{lease_suffix}"
            ),
            task_start=asyncio.get_event_loop().time(),
            channel=task_channel(command.task_id),
        ),
    )
    if execution_epoch is not None:
        state.preparation.queue_metadata_payload[_EXECUTION_EPOCH_KEY] = max(
            0, int(execution_epoch)
        )
    return state


def _event_payload(state: CompletionExecution, **extra: Any) -> dict[str, Any]:
    return {
        "completion_id": state.request.task_id,
        "message_id": state.preparation.message_id,
        "attempt": state.preparation.attempt,
        "attempt_epoch": state.preparation.attempt_epoch,
        "execution_epoch": _completion_execution_epoch(state),
        **extra,
    }


async def claim_completion(state: CompletionExecution) -> bool:
    """Acquire the lease and transition the completion row to streaming."""
    await state.ports.retry._acquire_lease(
        state.request.redis,
        state.request.task_id,
        state.request.lease_token,
    )
    state.settlement.lease_acquired = True
    state.settlement.renewer = asyncio.create_task(
        state.ports.retry._lease_renewer(
            state.request.redis,
            state.request.task_id,
            state.request.lease_token,
            state.settlement.lease_lost,
        )
    )

    raw_expected_execution_epoch = state.preparation.queue_metadata_payload.get(
        _EXECUTION_EPOCH_KEY
    )
    if raw_expected_execution_epoch is None:
        async with state.ports.persistence.SessionLocal() as session:
            current_execution_epoch = (
                await session.execute(
                    state.ports.persistence.select(
                        state.ports.persistence.Completion.execution_epoch
                    ).where(
                        state.ports.persistence.Completion.id
                        == state.request.task_id
                    )
                )
            ).scalar_one_or_none()
        expected_execution_epoch = max(0, int(current_execution_epoch or 0))
        state.preparation.queue_metadata_payload[_EXECUTION_EPOCH_KEY] = (
            expected_execution_epoch
        )
    else:
        expected_execution_epoch = max(0, int(raw_expected_execution_epoch))
    await bind_task_lease_execution_epoch(state, expected_execution_epoch)
    bind_completion_execution_fence(state, expected_execution_epoch)

    async with state.ports.persistence.SessionLocal() as session:
        await state.ports.persistence._acquire_completion_xact_lock(
            session, state.request.task_id
        )
        completion: Completion | None = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(state.ports.persistence.Completion.id == state.request.task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            state.ports.events.logger.warning(
                "completion not found task_id=%s", state.request.task_id
            )
            state.settlement.task_outcome = "not_found"
            return False
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost("lease lost before completion claim")
        current_execution_epoch = max(
            0,
            int(getattr(completion, "execution_epoch", 0) or 0),
        )
        if current_execution_epoch != expected_execution_epoch:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion execution superseded task={state.request.task_id} "
                f"expected={expected_execution_epoch} current={current_execution_epoch}"
            )
        if state.ports.persistence.is_completion_terminal(completion.status):
            state.ports.events.logger.info(
                "completion terminal task_id=%s status=%s",
                state.request.task_id,
                completion.status,
            )
            state.settlement.task_outcome = "terminal"
            return False

        state.preparation.was_restarted = (completion.attempt or 0) > 0 and bool(
            completion.text
        )
        state.preparation.user_id = completion.user_id
        state.preparation.message_id = completion.message_id
        state.preparation.system_prompt = completion.system_prompt
        state.preparation.user_api_credential_id = getattr(
            completion,
            "user_api_credential_id",
            None,
        )
        user = await session.get(
            state.ports.persistence.User, state.preparation.user_id
        )
        state.preparation.account_mode = getattr(user, "account_mode", "wallet")
        state.preparation.chat_model = (
            completion.model or state.ports.context.DEFAULT_CHAT_MODEL
        )
        if getattr(completion, "cancel_requested_at", None) is not None:
            state.settlement.task_outcome = CompletionOutcome.CANCELLED.value
            return False
        (
            state.preparation.attempt,
            preflight_failure,
        ) = await state.ports.retry._completion_preflight_failure(
            session,
            completion,
        )
        state.preparation.attempt_epoch = state.preparation.attempt
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost("lease lost during completion preflight")
        if preflight_failure is not None:
            err_code, err_msg = preflight_failure
            await _stage_preflight_failure(
                state,
                session,
                completion,
                err_code=err_code,
                err_msg=err_msg,
            )
            state.settlement.task_outcome = "failed"
            return False

        completion.status = CompletionStatus.STREAMING.value
        completion.progress_stage = CompletionStage.STREAMING
        started_at = datetime.now(timezone.utc)
        completion.started_at = started_at
        completion.attempt = state.preparation.attempt
        upstream_request = dict(completion.upstream_request or {})
        state.preparation.queue_metadata_payload = (
            state.ports.retry.completion_queue_metadata(
                upstream_request=upstream_request,
                created_at=completion.created_at,
                started_at=started_at,
                finished_at=completion.finished_at,
                now=started_at,
            )
        )
        state.preparation.queue_metadata_payload[_EXECUTION_EPOCH_KEY] = (
            expected_execution_epoch
        )
        state.preparation.queue_metadata_payload[_STABLE_PROVIDER_IDEMPOTENCY_KEY] = (
            has_stable_provider_idempotency_key(completion)
        )
        completion.upstream_request = state.ports.retry.merge_queue_metadata(
            upstream_request,
            state.preparation.queue_metadata_payload,
        )
        if state.preparation.was_restarted:
            completion.text = ""
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost(
                "lease lost before completion claim commit"
            )
        await session.commit()

        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        state.preparation.conversation_id = (
            message.conversation_id if message is not None else None
        )

    return True


def _start_stream_span(state: CompletionExecution) -> None:
    try:
        span_cm = state.ports.events._tracer.start_as_current_span(
            "upstream.stream_completion"
        )
        span = span_cm.__enter__()
        state.settlement.stream_span_cm = span_cm
        span.set_attribute("lumen.task_id", state.request.task_id)
    except Exception:  # noqa: BLE001
        if state.settlement.stream_span_cm is not None:
            with suppress(BaseException):
                state.settlement.stream_span_cm.__exit__(None, None, None)
            state.settlement.stream_span_cm = None


async def _resolve_runtime_override(state: CompletionExecution) -> None:
    if not state.preparation.user_api_credential_id:
        return
    async with state.ports.persistence.SessionLocal() as session:
        state.preparation.runtime_override = (
            await state.ports.billing.resolve_user_credential_runtime(
                session,
                state.preparation.user_api_credential_id,
            )
        )
    if "chat" not in (
        getattr(state.preparation.runtime_override, "purposes", ()) or ()
    ):
        raise state.ports.upstream.UpstreamError(
            "user API key supplier does not allow chat purpose",
            status_code=403,
            error_code="byok_purpose_mismatch",
            payload={"credential_id": state.preparation.user_api_credential_id},
        )


async def prepare_completion_request(state: CompletionExecution) -> None:
    await _resolve_runtime_override(state)
    await load_request_context(state)
    state.preparation.reasoning_effort = (
        state.ports.upstream._normalize_reasoning_effort_for_upstream(
            state.preparation.reasoning_effort
        )
    )
    state.streaming.body = {
        "model": state.preparation.chat_model,
        "input": state.streaming.input_list,
        "instructions": state.streaming.instructions,
        "stream": True,
        "store": True,
    }
    state.ports.tools._configure_chat_tools(
        state.streaming.body, state.streaming.chat_tools
    )
    if state.preparation.reasoning_effort:
        state.streaming.body["reasoning"] = {
            "effort": state.preparation.reasoning_effort,
            "summary": "auto",
        }
    if state.preparation.fast_mode:
        state.streaming.body["service_tier"] = "priority"
    state.streaming.max_tool_invocations = max(
        1,
        await state.ports.context.runtime_settings.resolve_int(
            "chat.max_tool_invocations",
            state.ports.retry._MAX_TOOL_INVOCATIONS_DEFAULT,
        ),
    )
    state.streaming.cancel_poll_interval_s = max(
        0.05,
        (
            await state.ports.context.runtime_settings.resolve_int(
                "chat.cancel_poll_interval_ms",
                int(state.ports.retry._CANCEL_POLL_INTERVAL_S * 1000),
            )
        )
        / 1000,
    )
    state.streaming.tool_idle_timeout_s = normalize_tool_idle_timeout_seconds(
        await state.ports.context.runtime_settings.resolve_int(
            "chat.tool_status_idle_timeout_s",
            int(state.ports.retry._TOOL_IDLE_TIMEOUT_S_DEFAULT),
        ),
        default=state.ports.retry._TOOL_IDLE_TIMEOUT_S_DEFAULT,
    )


async def _publish_thinking(
    state: CompletionExecution,
    text: str,
) -> None:
    if not text:
        return
    if state.streaming.accumulated_thinking.endswith(text):
        return
    state.streaming.accumulated_thinking += text
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_THINKING_DELTA,
        _event_payload(state, thinking_delta=text),
    )


async def _store_image_event(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    mark_partial: bool,
) -> None:
    image_b64 = state.ports.tools._extract_response_image_b64(event)
    if not image_b64:
        return
    dedupe_key = state.ports.tools._tool_image_dedupe_key(event, image_b64)
    if dedupe_key in state.streaming.stored_image_call_ids:
        return
    if mark_partial:
        state.streaming.has_partial = True
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before tool image store")
    (
        image_payload,
        image_budget_micro,
    ) = await state.ports.tools.tool_image_service.store_and_publish_tool_image(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        execution_epoch=_completion_execution_epoch(state),
        b64_image=image_b64,
        revised_prompt=state.ports.tools._extract_response_revised_prompt(event),
        reserved_tool_image_micro=state.streaming.reserved_tool_image_budget_micro,
    )
    if image_payload is None:
        return
    state.streaming.tool_images.append(image_payload)
    state.streaming.stored_image_call_ids.add(dedupe_key)
    state.streaming.reserved_tool_image_budget_micro += image_budget_micro


async def handle_completion_tool_call(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    allow_tool_limit: bool,
) -> bool:
    tool_call = state.usage.tool_tracker.update(event)
    if tool_call is None:
        return False
    await state.ports.tools._publish_completion_tool_progress(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_call=tool_call,
        tool_calls=state.usage.tool_tracker.content(),
    )
    if not allow_tool_limit or (
        state.usage.tool_tracker.invocation_count
        <= state.streaming.max_tool_invocations
    ):
        return False
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.finalize_active(
            ToolStatus.FAILED.value,
            error="tool invocation limit exceeded",
        ),
    )
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_PROGRESS,
        _event_payload(
            state,
            stage="tool_loop_truncated",
            max_tool_invocations=state.streaming.max_tool_invocations,
        ),
    )
    state.streaming.tool_loop_truncated = True
    return True


async def _handle_delta(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    phase: str,
) -> None:
    delta = event.get("delta") or ""
    if not delta:
        return
    state.streaming.has_partial = True
    state.streaming.accumulated_text += delta
    state.usage.delta_counter += 1
    if state.usage.delta_counter % state.ports.retry._CANCEL_CHECK_EVERY_DELTAS == 0:
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost(f"lease lost during {phase} stream")
        if await state.ports.retry._is_cancelled(
            state.request.redis, state.request.task_id
        ):
            raise state.ports.retry._TaskCancelled(f"cancelled during {phase} stream")
    total_len = len(state.streaming.accumulated_text)
    if (
        total_len - state.streaming.flushed_len
        >= state.ports.retry._PG_FLUSH_EVERY_CHARS
    ):
        state.streaming.flushed_len = total_len
        await state.ports.persistence._flush_completion_text(
            state.request.task_id,
            state.streaming.accumulated_text,
            attempt_epoch=state.preparation.attempt_epoch,
        )
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_DELTA,
        _event_payload(state, text_delta=delta),
    )


async def _handle_completed(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    append_completed_text: bool,
    finalize_tools: bool,
) -> None:
    state.streaming.has_partial = True
    raw_response = event.get("response")
    response = raw_response if isinstance(raw_response, dict) else {}
    state.usage.completed_response = response
    raw_usage = response.get("usage")
    state.usage.usage_totals.record_usage(
        state.ports.billing.parse_usage(
            state.preparation.chat_model,
            raw_usage if isinstance(raw_usage, dict) else None,
        ),
        raw_usage=raw_usage if isinstance(raw_usage, dict) else None,
    )
    completed_text = state.ports.upstream._extract_completed_output_text(response)
    if append_completed_text:
        if completed_text and not state.streaming.accumulated_text.endswith(
            completed_text
        ):
            state.streaming.accumulated_text = (
                f"{state.streaming.accumulated_text}\n\n{completed_text}"
                if state.streaming.accumulated_text
                else completed_text
            )
    elif not state.streaming.accumulated_text:
        state.streaming.accumulated_text = completed_text
    if not state.streaming.accumulated_thinking:
        await _publish_thinking(
            state,
            state.ports.upstream._extract_reasoning_text_from_response(response),
        )
    for image_event in state.ports.tools._extract_image_events_from_response(response):
        await _store_image_event(state, image_event, mark_partial=False)
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.update_from_response(response),
    )
    if finalize_tools:
        await state.ports.tools._publish_completion_tool_updates(
            redis=state.request.redis,
            user_id=state.preparation.user_id,
            channel=state.request.channel,
            task_id=state.request.task_id,
            message_id=state.preparation.message_id,
            attempt=state.preparation.attempt,
            attempt_epoch=state.preparation.attempt_epoch,
            tool_tracker=state.usage.tool_tracker,
            updates=state.usage.tool_tracker.finalize_active(
                ToolStatus.SUCCEEDED.value
            ),
        )


async def _handle_terminal_event(
    state: CompletionExecution,
    event: dict[str, Any],
) -> None:
    event_type = event.get("type", "")
    raw_response = event.get("response")
    response = raw_response if isinstance(raw_response, dict) else {}
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.update_from_response(response),
    )
    terminal_status = (
        ToolStatus.CANCELLED.value
        if event_type in {"response.cancelled", "response.canceled"}
        else ToolStatus.FAILED.value
    )
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.finalize_active(
            terminal_status,
            error=state.ports.tools._summarize_tool_error(
                response.get("error")
                or response.get("incomplete_details")
                or event.get("error")
            ),
        ),
    )
    state.ports.upstream._raise_for_terminal_response_event(
        event_type,
        response,
        event.get("error"),
    )


async def _consume_round(
    state: CompletionExecution,
    body: dict[str, Any],
    *,
    phase: str,
    allow_tool_limit: bool,
    track_tool_calls: bool,
    append_completed_text: bool,
    finalize_tools: bool,
) -> None:
    round_receipt = _CompletionRoundReceipt()
    state.usage.active_round_dispatch_started = False
    state.usage.active_round_response_received = False
    state.usage.active_round_dispatch_proven_undelivered = False
    if not state.usage.dispatch_started_recorded:
        await record_completion_upstream_marker(state, response_received=False)
        state.usage.dispatch_started_recorded = True
    round_receipt.dispatch_started_recorded = True
    state.usage.active_round_dispatch_started = True
    stream = state.ports.upstream.stream_completion(
        body,
        runtime_override=state.preparation.runtime_override,
    )
    try:
        async for event in state.ports.retry._iter_completion_stream_with_abort(
            stream,
            cancel_requested=state.settlement.cancel_requested,
            lease_lost=state.settlement.lease_lost,
            tool_tracker=state.usage.tool_tracker,
            tool_idle_timeout_s=state.streaming.tool_idle_timeout_s,
        ):
            await _ensure_completion_execution_current(state)
            if state.settlement.lease_lost.is_set():
                raise state.ports.retry._LeaseLost(f"lease lost during {phase} stream")
            event_type = event.get("type", "")
            if event_type == "provider_used":
                provider_event = (
                    state.ports.upstream._completion_upstream_provider_event(event)
                )
                if provider_event:
                    state.usage.upstream_provider_event = provider_event
                    await state.ports.upstream._record_completion_upstream_metadata(
                        task_id=state.request.task_id,
                        attempt_epoch=state.preparation.attempt_epoch,
                        provider_event=provider_event,
                        fast_mode=state.preparation.fast_mode,
                    )
                continue
            if (
                event_type.startswith("response.")
                and not round_receipt.response_received
            ):
                await record_completion_upstream_marker(state, response_received=True)
                round_receipt.response_received = True
                state.usage.response_receipt_recorded = True
                state.usage.active_round_response_received = True
            if track_tool_calls and await handle_completion_tool_call(
                state,
                event,
                allow_tool_limit=allow_tool_limit,
            ):
                return
            await _publish_thinking(
                state, state.ports.upstream._extract_reasoning_delta(event)
            )
            await _store_image_event(state, event, mark_partial=True)
            if event_type == "response.output_text.delta":
                await _handle_delta(state, event, phase=phase)
            elif event_type == "response.completed":
                await _handle_completed(
                    state,
                    event,
                    append_completed_text=append_completed_text,
                    finalize_tools=finalize_tools,
                )
            elif event_type in {
                "response.failed",
                "response.incomplete",
                "response.cancelled",
                "response.canceled",
            }:
                await _handle_terminal_event(state, event)
    except Exception as exc:
        await _raise_completion_round_failure(state, exc, round_receipt)
        raise


async def _raise_completion_round_failure(
    state: CompletionExecution,
    exc: Exception,
    round_receipt: _CompletionRoundReceipt,
) -> None:
    """Classify dispatch uncertainty from the failed round, not prior rounds."""

    state.usage.active_round_dispatch_started = round_receipt.dispatch_started_recorded
    state.usage.active_round_response_received = round_receipt.response_received
    execution_response_received = state.usage.response_receipt_recorded
    state.usage.response_receipt_recorded = round_receipt.response_received
    try:
        if _is_completion_round_control_failure(state, exc):
            return
        if (
            execution_response_received
            and state.preparation.queue_metadata_payload.get(
                _STABLE_PROVIDER_IDEMPOTENCY_KEY
            )
            is not True
        ):
            raise CompletionDispatchResultUnknown(
                COMPLETION_RESULT_UNKNOWN_MESSAGE
            ) from exc
        await _raise_completion_dispatch_failure(state, exc)
    finally:
        state.usage.response_receipt_recorded = (
            execution_response_received
            or round_receipt.response_received
            or state.usage.response_receipt_recorded
        )


def _is_completion_round_control_failure(
    state: CompletionExecution,
    exc: Exception,
) -> bool:
    retry = state.ports.retry
    return any(
        isinstance(exc, error_type)
        for error_type in (
            CompletionDispatchResultUnknown,
            getattr(retry, "_CompletionEpochSuperseded", None),
            getattr(retry, "_TaskCancelled", None),
            getattr(retry, "_LeaseLost", None),
        )
        if isinstance(error_type, type) and issubclass(error_type, BaseException)
    )


async def consume_completion_stream(state: CompletionExecution) -> None:
    if await state.ports.retry._is_cancelled(
        state.request.redis, state.request.task_id
    ):
        raise state.ports.retry._TaskCancelled("cancelled before stream start")
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before stream start")
    cancel_requested = asyncio.Event()
    state.settlement.cancel_requested = cancel_requested
    state.settlement.cancel_stop_requested = asyncio.Event()
    state.settlement.cancel_watcher = asyncio.create_task(
        state.ports.retry._watch_completion_cancel(
            state.request.redis,
            state.request.task_id,
            cancel_requested=cancel_requested,
            stop_requested=state.settlement.cancel_stop_requested,
            poll_interval_s=state.streaming.cancel_poll_interval_s,
        )
    )
    state.usage.request_sent = True
    state.usage.round_text_start = len(state.streaming.accumulated_text)
    state.usage.round_thinking_start = len(state.streaming.accumulated_thinking)
    state.usage.usage_totals.start_round(
        input_fallback_tokens=state.ports.tools._estimate_completion_request_input_tokens(
            state.streaming.input_list,
            instructions=state.streaming.instructions,
        ),
        tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
            state.usage.tool_tracker.content()
        ),
    )
    await _consume_round(
        state,
        state.streaming.body,
        phase="primary",
        allow_tool_limit=True,
        track_tool_calls=True,
        append_completed_text=False,
        finalize_tools=False,
    )
    if state.streaming.tool_loop_truncated:
        state.usage.usage_totals.finish_round(
            output_text=state.streaming.accumulated_text[
                state.usage.round_text_start :
            ],
            reasoning_text=state.streaming.accumulated_thinking[
                state.usage.round_thinking_start :
            ],
            tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
                state.usage.tool_tracker.content()
            ),
        )
        fallback_body = state.ports.tools._tool_limited_completion_body(
            state.streaming.body
        )
        state.usage.round_text_start = len(state.streaming.accumulated_text)
        state.usage.round_thinking_start = len(state.streaming.accumulated_thinking)
        state.usage.usage_totals.start_round(
            input_fallback_tokens=state.ports.tools._estimate_completion_request_input_tokens(
                fallback_body["input"],
                instructions=fallback_body.get("instructions"),
            ),
            tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
                state.usage.tool_tracker.content()
            ),
        )
        await _consume_round(
            state,
            fallback_body,
            phase="fallback",
            allow_tool_limit=False,
            track_tool_calls=False,
            append_completed_text=True,
            finalize_tools=True,
        )
    state.usage.usage_totals.finish_round(
        output_text=state.streaming.accumulated_text[state.usage.round_text_start :],
        reasoning_text=state.streaming.accumulated_thinking[
            state.usage.round_thinking_start :
        ],
        tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
            state.usage.tool_tracker.content()
        ),
    )


async def publish_completion_started(state: CompletionExecution) -> None:
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before completion start event")
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_RESTARTED if state.preparation.was_restarted else EV_COMP_STARTED,
        _event_payload(
            state,
            **{
                key: value
                for key, value in state.preparation.queue_metadata_payload.items()
                if not key.startswith("_") and key != _EXECUTION_EPOCH_KEY
            },
        ),
    )
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost during completion start event")


async def run_completion(
    command: CompletionCommand,
    ports: CompletionBindings,
    services: CompletionServices,
) -> CompletionResult:
    """ARQ entrypoint; phases are split by context, stream, and terminal state."""
    state = _new_execution(command, ports, services)
    with bind_task_cancellation(
        kind="completion",
        task_id=command.task_id,
        model=ports.persistence.Completion,
        session_factory=ports.persistence.SessionLocal,
        logger=ports.events.logger,
    ):
        return await _run_completion_scoped(command, state, services)


async def _run_completion_scoped(
    command: CompletionCommand,
    state: CompletionExecution,
    services: CompletionServices,
) -> CompletionResult:
    try:
        claim = await services.repository.claim(state)
        if claim.claimed:
            services.tool_executor.initialize(state)
            _start_stream_span(state)
            await services.events.publish_started(state)
            await services.context_builder.prepare(state)
            await services.upstream_client.consume(state)
            await _ensure_completion_execution_current(state)
            await services.billing.settle_success(state)
    except BaseException as failure:
        await handle_completion_run_failure(command, state, services, failure)
    finally:
        await services.repository.cleanup(state)
    return CompletionResult(
        task_id=command.task_id,
        phase=(
            CompletionPhase.COMPLETE
            if state.settlement.task_outcome == CompletionOutcome.SUCCEEDED.value
            else CompletionPhase.SETTLEMENT
        ),
        outcome=CompletionOutcome(state.settlement.task_outcome),
        attempt=state.preparation.attempt,
    )


__all__ = [
    "CompletionExecution",
    "handle_completion_failure",
    "run_completion",
    "settle_cancelled_completion",
]
