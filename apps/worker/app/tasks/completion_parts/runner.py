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
from .legacy_adapter import LegacyCompletionAdapter
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import (
    DEFAULT_CHAT_INSTRUCTIONS,
    EV_COMP_DELTA,
    EV_COMP_FAILED,
    EV_COMP_PROGRESS,
    EV_COMP_RESTARTED,
    EV_COMP_STARTED,
    EV_COMP_THINKING_DELTA,
    CompletionStage,
    CompletionStatus,
    GenerationErrorCode as EC,
    MessageStatus,
    task_channel,
)
from lumen_core.chat_tools import ToolStatus, normalize_tool_idle_timeout_seconds
from lumen_core.models import Completion
from lumen_core.upstream_billing import (
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
)


def _new_execution(
    command: CompletionCommand,
    ports: LegacyCompletionAdapter,
    services: CompletionServices,
) -> CompletionExecution:
    return CompletionExecution(
        ports=ports,
        services=services,
        request=CompletionRequest(
            redis=command.redis,
            task_id=command.task_id,
            lease_token=f"{command.worker_id}:{ports.persistence.new_uuid7()}",
            task_start=asyncio.get_event_loop().time(),
            channel=task_channel(command.task_id),
        ),
    )


def _event_payload(state: CompletionExecution, **extra: Any) -> dict[str, Any]:
    return {
        "completion_id": state.request.task_id,
        "message_id": state.preparation.message_id,
        "attempt": state.preparation.attempt,
        "attempt_epoch": state.preparation.attempt_epoch,
        **extra,
    }


async def _stage_preflight_failure(
    state: CompletionExecution,
    session: Any,
    completion: Completion,
    *,
    err_code: str,
    err_msg: str,
) -> None:
    completion.status = CompletionStatus.FAILED.value
    completion.progress_stage = CompletionStage.FINALIZING
    completion.attempt = state.preparation.attempt
    completion.finished_at = datetime.now(timezone.utc)
    completion.error_code = err_code
    completion.error_message = err_msg
    message = await session.get(
        state.ports.persistence.Message, state.preparation.message_id
    )
    if message is not None and message.status != MessageStatus.CANCELED:
        message.status = MessageStatus.FAILED
    failed = await session.get(
        state.ports.persistence.Completion, state.request.task_id
    )
    if failed is not None:
        await state.ports.billing.worker_billing.release_completion(
            session,
            failed,
            reason=err_code,
        )
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before preflight failure commit")
    delivery = state.ports.events._stage_completion_event(
        session,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_FAILED,
        state.ports.events._completion_event_payload(
            state.request.task_id,
            state.preparation.message_id,
            state.preparation.attempt,
            state.preparation.attempt_epoch,
            code=err_code,
            message=err_msg,
            retriable=False,
        ),
    )
    await session.commit()
    await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)


async def _claim_completion(state: CompletionExecution) -> bool:
    """Acquire the lease and transition the completion row to streaming."""
    await state.ports.retry._acquire_lease(
        state.request.redis, state.request.task_id, state.request.lease_token
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
        if state.ports.persistence.is_completion_terminal(completion.status):
            state.ports.events.logger.info(
                "completion terminal task_id=%s status=%s",
                state.request.task_id,
                completion.status,
            )
            state.settlement.task_outcome = "terminal"
            return False
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost("lease lost before completion claim")

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


async def _load_request_context(state: CompletionExecution) -> None:
    state.streaming.instructions = (
        state.preparation.system_prompt or DEFAULT_CHAT_INSTRUCTIONS
    )
    async with state.ports.persistence.SessionLocal() as session:
        state.preparation.target_msg = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if state.preparation.conversation_id is not None:
            packed = await state.ports.context._pack_recent_history(
                session,
                conversation_id=state.preparation.conversation_id,
                up_to_message_id=state.preparation.message_id,
                system_prompt=state.preparation.system_prompt,
                redis=state.request.redis,
                chat_model=state.preparation.chat_model,
                account_mode=state.preparation.account_mode,
            )
            if state.settlement.lease_lost.is_set():
                raise state.ports.retry._LeaseLost("lease lost after history pack")
            state.streaming.input_list = packed.input_list
            state.streaming.instructions = (
                state.ports.context._instructions_with_summary_guardrail(
                    state.preparation.system_prompt,
                    enabled=packed.summary_used or packed.sticky_used,
                )
            )
            memory_meta = await state.ports.context._inject_user_memory_context(
                session,
                input_list=state.streaming.input_list,
                user_id=state.preparation.user_id,
                conversation_id=state.preparation.conversation_id,
                parent_user_message_id=(
                    getattr(state.preparation.target_msg, "parent_message_id", None)
                    if state.preparation.target_msg is not None
                    else None
                ),
                redis=state.request.redis,
            )
            state.usage.memory_meta_for_event = memory_meta
            await state.ports.context._record_completion_context_metadata(
                session,
                task_id=state.request.task_id,
                attempt_epoch=state.preparation.attempt_epoch,
                packed=packed,
            )
            if memory_meta.get("used_memory_ids"):
                completion = await session.get(
                    state.ports.persistence.Completion, state.request.task_id
                )
                if (
                    completion is not None
                    and completion.attempt == state.preparation.attempt_epoch
                ):
                    upstream_request = dict(completion.upstream_request or {})
                    upstream_request["memory"] = memory_meta
                    completion.upstream_request = upstream_request
                    await session.commit()

        if (
            state.preparation.target_msg is not None
            and state.preparation.target_msg.parent_message_id
        ):
            parent = await session.get(
                state.ports.persistence.Message,
                state.preparation.target_msg.parent_message_id,
            )
            if parent is not None and isinstance(parent.content, dict):
                effort = parent.content.get("reasoning_effort")
                if effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
                    state.preparation.reasoning_effort = effort
                state.preparation.fast_mode = parent.content.get("fast") is True
                state.streaming.chat_tools = (
                    await state.ports.tools._chat_tools_from_content(parent.content)
                )


async def _prepare_request(state: CompletionExecution) -> None:
    await _resolve_runtime_override(state)
    await _load_request_context(state)
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
        b64_image=image_b64,
        revised_prompt=state.ports.tools._extract_response_revised_prompt(event),
        reserved_tool_image_micro=state.streaming.reserved_tool_image_budget_micro,
    )
    if image_payload is None:
        return
    state.streaming.tool_images.append(image_payload)
    state.streaming.stored_image_call_ids.add(dedupe_key)
    state.streaming.reserved_tool_image_budget_micro += image_budget_micro


async def _handle_tool_call(
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
    if not state.usage.dispatch_started_recorded:
        await _record_completion_upstream_marker(state, response_received=False)
        state.usage.dispatch_started_recorded = True
    stream = state.ports.upstream.stream_completion(
        body,
        runtime_override=state.preparation.runtime_override,
    )
    async for event in state.ports.retry._iter_completion_stream_with_abort(
        stream,
        cancel_requested=state.settlement.cancel_requested,
        lease_lost=state.settlement.lease_lost,
        tool_tracker=state.usage.tool_tracker,
        tool_idle_timeout_s=state.streaming.tool_idle_timeout_s,
    ):
        if not state.usage.response_receipt_recorded:
            await _record_completion_upstream_marker(state, response_received=True)
            state.usage.response_receipt_recorded = True
        if state.settlement.lease_lost.is_set():
            raise state.ports.retry._LeaseLost(f"lease lost during {phase} stream")
        event_type = event.get("type", "")
        if event_type == "provider_used":
            provider_event = state.ports.upstream._completion_upstream_provider_event(
                event
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
        if track_tool_calls:
            if await _handle_tool_call(
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


async def _record_completion_upstream_marker(
    state: CompletionExecution,
    *,
    response_received: bool,
) -> None:
    recorded_at = datetime.now(timezone.utc).isoformat()
    async with state.ports.persistence.SessionLocal() as session:
        completion = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(
                    state.ports.persistence.Completion.id == state.request.task_id,
                    state.ports.persistence.Completion.attempt
                    == state.preparation.attempt,
                    state.ports.persistence.Completion.status
                    == CompletionStatus.STREAMING.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion marker stale task={state.request.task_id} attempt={state.preparation.attempt}"
            )
        marker = (
            mark_upstream_response_received
            if response_received
            else mark_upstream_dispatch_started
        )
        completion.upstream_request = marker(
            completion,
            at=recorded_at,
            attempt=state.preparation.attempt,
        )
        await session.commit()


async def _consume_stream(state: CompletionExecution) -> None:
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


async def _cancel_completion_row(
    state: CompletionExecution,
) -> tuple[str, str, dict[str, Any]] | None:
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
            )
            .values(
                status=CompletionStatus.CANCELED.value,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=EC.CANCELLED.value,
                error_message="cancelled by user",
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion cancel superseded task={state.request.task_id} "
                f"attempt_epoch={state.preparation.attempt_epoch}"
            )
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status not in (
            MessageStatus.SUCCEEDED,
            MessageStatus.FAILED,
            MessageStatus.CANCELED,
        ):
            tool_calls = state.usage.tool_tracker.content()
            if tool_calls:
                content = dict(message.content or {})
                content["tool_calls"] = tool_calls
                message.content = content
            message.status = MessageStatus.FAILED
        completion = await session.get(
            state.ports.persistence.Completion, state.request.task_id
        )
        if completion is not None:
            await state.ports.billing._settle_cancelled_completion_billing(
                session,
                completion,
                has_partial=state.streaming.has_partial,
                input_list=state.streaming.input_list
                if state.usage.request_sent
                else None,
                instructions=state.streaming.instructions
                if state.usage.request_sent
                else None,
                usage_is_finalized=True,
                accumulated_text=state.streaming.accumulated_text,
                tokens_in=state.usage.usage_totals.tokens_in,
                tokens_out=state.usage.usage_totals.tokens_out,
                cache_read_tokens=state.usage.usage_totals.cache_read_tokens,
                cache_creation_tokens=state.usage.usage_totals.cache_creation_tokens,
                cache_creation_5m_tokens=state.usage.usage_totals.cache_creation_5m_tokens,
                cache_creation_1h_tokens=state.usage.usage_totals.cache_creation_1h_tokens,
                reasoning_tokens=state.usage.usage_totals.reasoning_tokens,
                image_output_tokens=state.usage.usage_totals.image_output_tokens,
                tool_images=state.streaming.tool_images,
                reserved_tool_image_budget_micro=(
                    state.streaming.reserved_tool_image_budget_micro
                ),
                reason=EC.CANCELLED.value,
            )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                code="cancelled",
                message="cancelled by user",
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
        return delivery


async def _settle_cancelled(state: CompletionExecution) -> None:
    state.usage.usage_totals.finish_round(
        output_text=state.streaming.accumulated_text[state.usage.round_text_start :],
        reasoning_text=state.streaming.accumulated_thinking[
            state.usage.round_thinking_start :
        ],
        tool_output_tokens=state.ports.tools._estimate_completion_tool_output_tokens(
            state.usage.tool_tracker.content()
        ),
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
        updates=state.usage.tool_tracker.finalize_active(ToolStatus.CANCELLED.value),
    )
    delivery: tuple[str, str, dict[str, Any]] | None = None
    try:
        delivery = await _cancel_completion_row(state)
    except state.ports.retry._CompletionEpochSuperseded as exc:
        state.ports.events.logger.info(
            "completion cancel skipped by newer epoch task=%s attempt_epoch=%s err=%s",
            state.request.task_id,
            state.preparation.attempt_epoch,
            exc,
        )
        state.settlement.task_outcome = "superseded"
        return
    except Exception as exc:  # noqa: BLE001
        state.ports.events.logger.warning(
            "completion cancel DB update failed task=%s err=%s",
            state.request.task_id,
            exc,
        )
    if delivery is not None:
        await state.ports.events._deliver_completion_event(
            state.request.redis, delivery
        )
    state.settlement.task_outcome = "failed"


def _failure_details(
    state: CompletionExecution,
    exc: BaseException,
) -> tuple[Any, str, str]:
    decision = state.ports.retry._classify_exception(exc, state.streaming.has_partial)
    _, byok_error = state.ports.billing.classify_user_credential_error(exc)
    if state.preparation.user_api_credential_id and byok_error:
        decision = state.ports.retry.RetryDecision(False, f"byok {byok_error}")
        err_code = state.ports.billing.byok_error_to_generation_code(byok_error)
        err_msg = state.ports.billing.byok_error_message(byok_error)
    else:
        err_code = (
            getattr(exc, "error_code", None)
            or getattr(exc, "code", None)
            or type(exc).__name__
        )
        err_msg = str(getattr(exc, "message", None) or exc)[:2000]
    return decision, str(err_code), err_msg


async def _mark_retry_queued(
    state: CompletionExecution,
    *,
    err_code: str,
    err_msg: str,
) -> bool:
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
            )
            .values(
                status=CompletionStatus.QUEUED.value,
                progress_stage=CompletionStage.QUEUED,
                error_code=err_code,
                error_message=err_msg,
            )
        )
        await session.commit()
        if state.ports.persistence.affected_rows(result) == 0:
            state.ports.events.logger.info(
                "completion retry skipped by newer epoch task=%s attempt_epoch=%s",
                state.request.task_id,
                state.preparation.attempt_epoch,
            )
            state.settlement.task_outcome = "superseded"
            return False
    return True


async def _settle_retry_enqueue_failure(
    state: CompletionExecution,
    *,
    enqueue_msg: str,
) -> None:
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
            error=enqueue_msg,
        ),
    )
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status
                == CompletionStatus.QUEUED.value,
            )
            .values(
                status=CompletionStatus.FAILED.value,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code="retry_enqueue_failed",
                error_message=enqueue_msg,
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            await session.commit()
            state.settlement.task_outcome = "superseded"
            return
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            message.status = MessageStatus.FAILED
        completion = await session.get(
            state.ports.persistence.Completion, state.request.task_id
        )
        if completion is not None:
            await state.ports.billing.worker_billing.release_completion(
                session,
                completion,
                reason="retry_enqueue_failed",
            )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                code="retry_enqueue_failed",
                message=enqueue_msg,
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "failed"


async def _settle_terminal_failure(
    state: CompletionExecution,
    *,
    err_code: str,
    err_msg: str,
) -> None:
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
            error=err_msg,
        ),
    )
    async with state.ports.persistence.SessionLocal() as session:
        result = await session.execute(
            state.ports.persistence.update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
            )
            .values(
                status=CompletionStatus.FAILED.value,
                progress_stage=CompletionStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=err_code,
                error_message=err_msg,
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            await session.commit()
            state.settlement.task_outcome = "superseded"
            return
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            tool_calls = state.usage.tool_tracker.content()
            if tool_calls:
                content = dict(message.content or {})
                content["tool_calls"] = tool_calls
                message.content = content
            message.status = MessageStatus.FAILED
        if (
            state.streaming.has_partial
            or state.streaming.tool_loop_truncated
            or any(state.usage.usage_totals.values())
        ):
            completion = await session.get(
                state.ports.persistence.Completion, state.request.task_id
            )
            if completion is not None:
                if (
                    state.streaming.tool_images
                    and state.usage.usage_totals.image_output_tokens <= 0
                    and state.streaming.reserved_tool_image_budget_micro > 0
                ):
                    state.usage.usage_totals.image_output_tokens = await state.ports.billing._fallback_completion_tool_image_tokens(
                        session,
                        completion,
                        budget_micro=state.streaming.reserved_tool_image_budget_micro,
                    )
                    state.usage.usage_totals.tokens_out = max(
                        state.usage.usage_totals.tokens_out,
                        state.usage.usage_totals.image_output_tokens,
                    )
                state.usage.usage_totals.apply_to(completion)
                await state.ports.billing._settle_failed_completion_billing(
                    session,
                    completion,
                    usage_values=state.usage.usage_totals.values(),
                    reason=str(err_code),
                )
        else:
            completion = await session.get(
                state.ports.persistence.Completion, state.request.task_id
            )
            if completion is not None:
                await state.ports.billing.worker_billing.release_completion(
                    session,
                    completion,
                    reason=str(err_code),
                )
        delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_FAILED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                code=err_code,
                message=err_msg,
                retriable=False,
            ),
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(session)
    await state.ports.events._deliver_completion_event(state.request.redis, delivery)
    state.settlement.task_outcome = "failed"


async def _handle_failure(
    state: CompletionExecution,
    exc: BaseException,
) -> None:
    if state.streaming.has_partial or state.streaming.tool_loop_truncated:
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
    if isinstance(exc, state.ports.retry._ToolIdleTimeout):
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
                ToolStatus.TIMED_OUT.value,
                error="tool call idle timeout",
            ),
        )
        exc = state.ports.upstream.UpstreamError(
            "tool call idle timeout",
            error_code=EC.TIMEOUT.value,
            status_code=200,
        )
    state.ports.events.upstream_calls_total.labels(
        kind="completion", outcome="error"
    ).inc()
    decision, err_code, err_msg = _failure_details(state, exc)
    _, byok_error = state.ports.billing.classify_user_credential_error(exc)
    if state.preparation.user_api_credential_id and byok_error:
        await state.ports.billing.record_user_credential_runtime_error(
            state.preparation.user_api_credential_id,
            exc,
        )
    state.ports.events.logger.warning(
        "completion failed task=%s attempt=%s retriable=%s reason=%s "
        "error_code=%s http_status=%s",
        state.request.task_id,
        state.preparation.attempt,
        decision.retriable,
        decision.reason,
        err_code,
        getattr(exc, "status_code", None),
    )
    state.ports.events.logger.debug(
        "completion exc trace task=%s", state.request.task_id, exc_info=True
    )
    if (
        decision.retriable
        and state.preparation.attempt < state.ports.retry._MAX_ATTEMPTS
    ):
        state.settlement.task_outcome = "retry"
        delay_index = min(
            state.preparation.attempt - 1,
            len(state.ports.retry.RETRY_BACKOFF_SECONDS) - 1,
        )
        delay = state.ports.retry.RETRY_BACKOFF_SECONDS[delay_index]
        if not await _mark_retry_queued(
            state,
            err_code=err_code,
            err_msg=err_msg,
        ):
            return
        try:
            await state.request.redis.enqueue_job(
                "run_completion",
                state.request.task_id,
                _defer_by=delay,
                _job_try=state.preparation.attempt + 1,
            )
        except Exception as enqueue_exc:  # noqa: BLE001
            state.ports.events.logger.error(
                "re-enqueue failed task=%s err=%s",
                state.request.task_id,
                enqueue_exc,
            )
            await _settle_retry_enqueue_failure(
                state,
                enqueue_msg=f"failed to enqueue retry: {enqueue_exc}"[:2000],
            )
        return
    await _settle_terminal_failure(
        state,
        err_code=err_code,
        err_msg=err_msg,
    )


async def _publish_started(state: CompletionExecution) -> None:
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before completion start event")
    await state.ports.events.publish_event(
        state.request.redis,
        state.preparation.user_id,
        state.request.channel,
        EV_COMP_RESTARTED if state.preparation.was_restarted else EV_COMP_STARTED,
        _event_payload(state, **state.preparation.queue_metadata_payload),
    )
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost during completion start event")


async def run_completion(
    command: CompletionCommand,
    ports: LegacyCompletionAdapter,
    services: CompletionServices,
) -> CompletionResult:
    """ARQ entrypoint; phases are split by context, stream, and terminal state."""
    state = _new_execution(command, ports, services)
    try:
        claim = await services.repository.claim(state)
        if claim.claimed:
            services.tool_executor.initialize(state)
            _start_stream_span(state)
            await services.events.publish_started(state)
            await services.context_builder.prepare(state)
            await services.upstream_client.consume(state)
            await services.billing.settle_success(state)
    except BaseException as failure:
        if services.lease_retry.is_lease_lost(failure):
            state.ports.events.logger.warning(
                "completion lease lost task=%s attempt=%s err=%s",
                command.task_id,
                state.preparation.attempt,
                failure,
            )
            services.events.record_outcome(state, CompletionOutcome.LEASE_LOST)
        elif services.lease_retry.is_superseded(failure):
            state.ports.events.logger.info(
                "completion worker superseded task=%s err=%s",
                command.task_id,
                failure,
            )
            services.events.record_outcome(state, CompletionOutcome.SUPERSEDED)
        elif services.lease_retry.is_cancelled(failure):
            state.ports.events.logger.info(
                "completion cancelled by user task=%s reason=%s",
                command.task_id,
                failure,
            )
            await services.billing.settle_cancelled(state)
        elif isinstance(failure, Exception):
            await services.billing.settle_failure(state, failure)
        else:
            raise
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


__all__ = ["CompletionExecution", "run_completion"]
