"""Completion task execution phases.

This module contains the mutable orchestration state and the ARQ task phases.
It receives all external dependencies through the explicit completion runtime.
"""

from __future__ import annotations

from .runtime import (
    completion_context_ports,
    completion_tools_ports,
    completion_persistence_ports,
    completion_upstream_ports,
    completion_billing_ports,
    completion_events_ports,
    completion_retry_ports,
)
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import (
    DEFAULT_CHAT_INSTRUCTIONS,
    DEFAULT_CHAT_MODEL,
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
from lumen_core.models import Completion, Message
from lumen_core.upstream_billing import (
    mark_upstream_dispatch_started,
    mark_upstream_response_received,
)
from .outcomes import settle_success


@dataclass(slots=True)
class CompletionExecution:
    """All mutable state shared by setup, streaming, and terminal phases."""

    redis: Any
    task_id: str
    lease_token: str
    task_start: float
    channel: str
    task_outcome: str = "unknown"
    attempt: int = 0
    attempt_epoch: int = 0
    user_api_credential_id: str | None = None
    account_mode: str = "wallet"
    runtime_override: Any | None = None
    queue_metadata_payload: dict[str, Any] = field(default_factory=dict)
    lease_lost: asyncio.Event = field(default_factory=asyncio.Event)
    lease_acquired: bool = False
    renewer: asyncio.Task[None] | None = None
    cancel_requested: asyncio.Event | None = None
    cancel_stop_requested: asyncio.Event | None = None
    cancel_watcher: asyncio.Task[None] | None = None
    stream_span_cm: Any | None = None
    was_restarted: bool = False
    user_id: str = ""
    message_id: str = ""
    system_prompt: str | None = None
    chat_model: str = DEFAULT_CHAT_MODEL
    conversation_id: str | None = None
    target_msg: Message | None = None
    reasoning_effort: str | None = None
    fast_mode: bool = False
    chat_tools: list[dict[str, Any]] = field(default_factory=list)
    memory_meta_for_event: dict[str, Any] = field(
        default_factory=lambda: {
            "used_memory_ids": [],
            "used_memory_summary": [],
        }
    )
    input_list: list[dict[str, Any]] = field(default_factory=list)
    instructions: str = DEFAULT_CHAT_INSTRUCTIONS
    body: dict[str, Any] = field(default_factory=dict)
    max_tool_invocations: int = 8
    cancel_poll_interval_s: float = 0.1
    tool_idle_timeout_s: float = 30.0
    accumulated_text: str = ""
    accumulated_thinking: str = ""
    flushed_len: int = 0
    has_partial: bool = False
    tool_images: list[dict[str, Any]] = field(default_factory=list)
    stored_image_call_ids: set[str] = field(default_factory=set)
    reserved_tool_image_budget_micro: int = 0
    tool_tracker: Any = None
    usage_totals: Any = None
    round_text_start: int = 0
    round_thinking_start: int = 0
    request_sent: bool = False
    dispatch_started_recorded: bool = False
    response_receipt_recorded: bool = False
    upstream_provider_event: dict[str, str] | None = None
    delta_counter: int = 0
    completed_response: dict[str, Any] | None = None
    tool_loop_truncated: bool = False


def _new_execution(ctx: dict[str, Any], task_id: str) -> CompletionExecution:
    redis = ctx["redis"]
    worker_id = str(ctx.get("worker_id") or ctx.get("job_id") or "worker")
    return CompletionExecution(
        redis=redis,
        task_id=task_id,
        lease_token=f"{worker_id}:{completion_persistence_ports().new_uuid7()}",
        task_start=asyncio.get_event_loop().time(),
        channel=task_channel(task_id),
    )


def _event_payload(state: CompletionExecution, **extra: Any) -> dict[str, Any]:
    return {
        "completion_id": state.task_id,
        "message_id": state.message_id,
        "attempt": state.attempt,
        "attempt_epoch": state.attempt_epoch,
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
    completion.attempt = state.attempt
    completion.finished_at = datetime.now(timezone.utc)
    completion.error_code = err_code
    completion.error_message = err_msg
    message = await session.get(
        completion_persistence_ports().Message, state.message_id
    )
    if message is not None and message.status != MessageStatus.CANCELED:
        message.status = MessageStatus.FAILED
    failed = await session.get(completion_persistence_ports().Completion, state.task_id)
    if failed is not None:
        await completion_billing_ports().worker_billing.release_completion(
            session,
            failed,
            reason=err_code,
        )
    if state.lease_lost.is_set():
        raise completion_retry_ports()._LeaseLost(
            "lease lost before preflight failure commit"
        )
    delivery = completion_events_ports()._stage_completion_event(
        session,
        state.user_id,
        state.channel,
        EV_COMP_FAILED,
        completion_events_ports()._completion_event_payload(
            state.task_id,
            state.message_id,
            state.attempt,
            state.attempt_epoch,
            code=err_code,
            message=err_msg,
            retriable=False,
        ),
    )
    await session.commit()
    await completion_billing_ports().worker_billing.flush_balance_cache_refreshes(
        session
    )
    await completion_events_ports()._deliver_completion_event(state.redis, delivery)


async def _claim_completion(state: CompletionExecution) -> bool:
    """Acquire the lease and transition the completion row to streaming."""
    await completion_retry_ports()._acquire_lease(
        state.redis, state.task_id, state.lease_token
    )
    state.lease_acquired = True
    state.renewer = asyncio.create_task(
        completion_retry_ports()._lease_renewer(
            state.redis,
            state.task_id,
            state.lease_token,
            state.lease_lost,
        )
    )

    async with completion_persistence_ports().SessionLocal() as session:
        await completion_persistence_ports()._acquire_completion_xact_lock(
            session, state.task_id
        )
        completion: Completion | None = (
            await session.execute(
                completion_persistence_ports()
                .select(completion_persistence_ports().Completion)
                .where(completion_persistence_ports().Completion.id == state.task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            completion_events_ports().logger.warning(
                "completion not found task_id=%s", state.task_id
            )
            state.task_outcome = "not_found"
            return False
        if completion_persistence_ports().is_completion_terminal(completion.status):
            completion_events_ports().logger.info(
                "completion terminal task_id=%s status=%s",
                state.task_id,
                completion.status,
            )
            state.task_outcome = "terminal"
            return False
        if state.lease_lost.is_set():
            raise completion_retry_ports()._LeaseLost(
                "lease lost before completion claim"
            )

        state.was_restarted = (completion.attempt or 0) > 0 and bool(completion.text)
        state.user_id = completion.user_id
        state.message_id = completion.message_id
        state.system_prompt = completion.system_prompt
        state.user_api_credential_id = getattr(
            completion,
            "user_api_credential_id",
            None,
        )
        user = await session.get(completion_persistence_ports().User, state.user_id)
        state.account_mode = getattr(user, "account_mode", "wallet")
        state.chat_model = (
            completion.model or completion_context_ports().DEFAULT_CHAT_MODEL
        )
        (
            state.attempt,
            preflight_failure,
        ) = await completion_retry_ports()._completion_preflight_failure(
            session,
            completion,
        )
        state.attempt_epoch = state.attempt
        if state.lease_lost.is_set():
            raise completion_retry_ports()._LeaseLost(
                "lease lost during completion preflight"
            )
        if preflight_failure is not None:
            err_code, err_msg = preflight_failure
            await _stage_preflight_failure(
                state,
                session,
                completion,
                err_code=err_code,
                err_msg=err_msg,
            )
            state.task_outcome = "failed"
            return False

        completion.status = CompletionStatus.STREAMING.value
        completion.progress_stage = CompletionStage.STREAMING
        started_at = datetime.now(timezone.utc)
        completion.started_at = started_at
        completion.attempt = state.attempt
        upstream_request = dict(completion.upstream_request or {})
        state.queue_metadata_payload = (
            completion_retry_ports().completion_queue_metadata(
                upstream_request=upstream_request,
                created_at=completion.created_at,
                started_at=started_at,
                finished_at=completion.finished_at,
                now=started_at,
            )
        )
        completion.upstream_request = completion_retry_ports().merge_queue_metadata(
            upstream_request,
            state.queue_metadata_payload,
        )
        if state.was_restarted:
            completion.text = ""
        if state.lease_lost.is_set():
            raise completion_retry_ports()._LeaseLost(
                "lease lost before completion claim commit"
            )
        await session.commit()

        message = await session.get(
            completion_persistence_ports().Message, state.message_id
        )
        state.conversation_id = message.conversation_id if message is not None else None

    state.tool_tracker = completion_tools_ports()._CompletionToolTracker()
    state.usage_totals = completion_tools_ports()._CompletionUsageAccumulator()
    _start_stream_span(state)
    return True


def _start_stream_span(state: CompletionExecution) -> None:
    try:
        span_cm = completion_events_ports()._tracer.start_as_current_span(
            "upstream.stream_completion"
        )
        span = span_cm.__enter__()
        state.stream_span_cm = span_cm
        span.set_attribute("lumen.task_id", state.task_id)
    except Exception:  # noqa: BLE001
        if state.stream_span_cm is not None:
            with suppress(BaseException):
                state.stream_span_cm.__exit__(None, None, None)
            state.stream_span_cm = None


async def _resolve_runtime_override(state: CompletionExecution) -> None:
    if not state.user_api_credential_id:
        return
    async with completion_persistence_ports().SessionLocal() as session:
        state.runtime_override = (
            await completion_billing_ports().resolve_user_credential_runtime(
                session,
                state.user_api_credential_id,
            )
        )
    if "chat" not in (getattr(state.runtime_override, "purposes", ()) or ()):
        raise completion_upstream_ports().UpstreamError(
            "user API key supplier does not allow chat purpose",
            status_code=403,
            error_code="byok_purpose_mismatch",
            payload={"credential_id": state.user_api_credential_id},
        )


async def _load_request_context(state: CompletionExecution) -> None:
    state.instructions = state.system_prompt or DEFAULT_CHAT_INSTRUCTIONS
    async with completion_persistence_ports().SessionLocal() as session:
        state.target_msg = await session.get(
            completion_persistence_ports().Message, state.message_id
        )
        if state.conversation_id is not None:
            packed = await completion_context_ports()._pack_recent_history(
                session,
                conversation_id=state.conversation_id,
                up_to_message_id=state.message_id,
                system_prompt=state.system_prompt,
                redis=state.redis,
                chat_model=state.chat_model,
                account_mode=state.account_mode,
            )
            if state.lease_lost.is_set():
                raise completion_retry_ports()._LeaseLost(
                    "lease lost after history pack"
                )
            state.input_list = packed.input_list
            state.instructions = (
                completion_context_ports()._instructions_with_summary_guardrail(
                    state.system_prompt,
                    enabled=packed.summary_used or packed.sticky_used,
                )
            )
            memory_meta = await completion_context_ports()._inject_user_memory_context(
                session,
                input_list=state.input_list,
                user_id=state.user_id,
                conversation_id=state.conversation_id,
                parent_user_message_id=(
                    getattr(state.target_msg, "parent_message_id", None)
                    if state.target_msg is not None
                    else None
                ),
                redis=state.redis,
            )
            state.memory_meta_for_event = memory_meta
            await completion_context_ports()._record_completion_context_metadata(
                session,
                task_id=state.task_id,
                attempt_epoch=state.attempt_epoch,
                packed=packed,
            )
            if memory_meta.get("used_memory_ids"):
                completion = await session.get(
                    completion_persistence_ports().Completion, state.task_id
                )
                if completion is not None and completion.attempt == state.attempt_epoch:
                    upstream_request = dict(completion.upstream_request or {})
                    upstream_request["memory"] = memory_meta
                    completion.upstream_request = upstream_request
                    await session.commit()

        if state.target_msg is not None and state.target_msg.parent_message_id:
            parent = await session.get(
                completion_persistence_ports().Message,
                state.target_msg.parent_message_id,
            )
            if parent is not None and isinstance(parent.content, dict):
                effort = parent.content.get("reasoning_effort")
                if effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
                    state.reasoning_effort = effort
                state.fast_mode = parent.content.get("fast") is True
                state.chat_tools = (
                    await completion_tools_ports()._chat_tools_from_content(
                        parent.content
                    )
                )


async def _prepare_request(state: CompletionExecution) -> None:
    await _resolve_runtime_override(state)
    await _load_request_context(state)
    state.reasoning_effort = (
        completion_upstream_ports()._normalize_reasoning_effort_for_upstream(
            state.reasoning_effort
        )
    )
    state.body = {
        "model": state.chat_model,
        "input": state.input_list,
        "instructions": state.instructions,
        "stream": True,
        "store": True,
    }
    completion_tools_ports()._configure_chat_tools(state.body, state.chat_tools)
    if state.reasoning_effort:
        state.body["reasoning"] = {
            "effort": state.reasoning_effort,
            "summary": "auto",
        }
    if state.fast_mode:
        state.body["service_tier"] = "priority"
    state.max_tool_invocations = max(
        1,
        await completion_context_ports().runtime_settings.resolve_int(
            "chat.max_tool_invocations",
            completion_retry_ports()._MAX_TOOL_INVOCATIONS_DEFAULT,
        ),
    )
    state.cancel_poll_interval_s = max(
        0.05,
        (
            await completion_context_ports().runtime_settings.resolve_int(
                "chat.cancel_poll_interval_ms",
                int(completion_retry_ports()._CANCEL_POLL_INTERVAL_S * 1000),
            )
        )
        / 1000,
    )
    state.tool_idle_timeout_s = normalize_tool_idle_timeout_seconds(
        await completion_context_ports().runtime_settings.resolve_int(
            "chat.tool_status_idle_timeout_s",
            int(completion_retry_ports()._TOOL_IDLE_TIMEOUT_S_DEFAULT),
        ),
        default=completion_retry_ports()._TOOL_IDLE_TIMEOUT_S_DEFAULT,
    )


async def _publish_thinking(
    state: CompletionExecution,
    text: str,
) -> None:
    if not text:
        return
    if state.accumulated_thinking.endswith(text):
        return
    state.accumulated_thinking += text
    await completion_events_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        EV_COMP_THINKING_DELTA,
        _event_payload(state, thinking_delta=text),
    )


async def _store_image_event(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    mark_partial: bool,
) -> None:
    image_b64 = completion_tools_ports()._extract_response_image_b64(event)
    if not image_b64:
        return
    dedupe_key = completion_tools_ports()._tool_image_dedupe_key(event, image_b64)
    if dedupe_key in state.stored_image_call_ids:
        return
    if mark_partial:
        state.has_partial = True
    if state.lease_lost.is_set():
        raise completion_retry_ports()._LeaseLost("lease lost before tool image store")
    (
        image_payload,
        image_budget_micro,
    ) = await completion_tools_ports().tool_image_service.store_and_publish_tool_image(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        b64_image=image_b64,
        revised_prompt=completion_tools_ports()._extract_response_revised_prompt(event),
        reserved_tool_image_micro=state.reserved_tool_image_budget_micro,
    )
    if image_payload is None:
        return
    state.tool_images.append(image_payload)
    state.stored_image_call_ids.add(dedupe_key)
    state.reserved_tool_image_budget_micro += image_budget_micro


async def _handle_tool_call(
    state: CompletionExecution,
    event: dict[str, Any],
    *,
    allow_tool_limit: bool,
) -> bool:
    tool_call = state.tool_tracker.update(event)
    if tool_call is None:
        return False
    await completion_tools_ports()._publish_completion_tool_progress(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_call=tool_call,
        tool_calls=state.tool_tracker.content(),
    )
    if not allow_tool_limit or (
        state.tool_tracker.invocation_count <= state.max_tool_invocations
    ):
        return False
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.finalize_active(
            ToolStatus.FAILED.value,
            error="tool invocation limit exceeded",
        ),
    )
    await completion_events_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        EV_COMP_PROGRESS,
        _event_payload(
            state,
            stage="tool_loop_truncated",
            max_tool_invocations=state.max_tool_invocations,
        ),
    )
    state.tool_loop_truncated = True
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
    state.has_partial = True
    state.accumulated_text += delta
    state.delta_counter += 1
    if state.delta_counter % completion_retry_ports()._CANCEL_CHECK_EVERY_DELTAS == 0:
        if state.lease_lost.is_set():
            raise completion_retry_ports()._LeaseLost(
                f"lease lost during {phase} stream"
            )
        if await completion_retry_ports()._is_cancelled(state.redis, state.task_id):
            raise completion_retry_ports()._TaskCancelled(
                f"cancelled during {phase} stream"
            )
    total_len = len(state.accumulated_text)
    if total_len - state.flushed_len >= completion_retry_ports()._PG_FLUSH_EVERY_CHARS:
        state.flushed_len = total_len
        await completion_persistence_ports()._flush_completion_text(
            state.task_id,
            state.accumulated_text,
            attempt_epoch=state.attempt_epoch,
        )
    await completion_events_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
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
    state.has_partial = True
    raw_response = event.get("response")
    response = raw_response if isinstance(raw_response, dict) else {}
    state.completed_response = response
    raw_usage = response.get("usage")
    state.usage_totals.record_usage(
        completion_billing_ports().parse_usage(
            state.chat_model,
            raw_usage if isinstance(raw_usage, dict) else None,
        ),
        raw_usage=raw_usage if isinstance(raw_usage, dict) else None,
    )
    completed_text = completion_upstream_ports()._extract_completed_output_text(
        response
    )
    if append_completed_text:
        if completed_text and not state.accumulated_text.endswith(completed_text):
            state.accumulated_text = (
                f"{state.accumulated_text}\n\n{completed_text}"
                if state.accumulated_text
                else completed_text
            )
    elif not state.accumulated_text:
        state.accumulated_text = completed_text
    if not state.accumulated_thinking:
        await _publish_thinking(
            state,
            completion_upstream_ports()._extract_reasoning_text_from_response(response),
        )
    for image_event in completion_tools_ports()._extract_image_events_from_response(
        response
    ):
        await _store_image_event(state, image_event, mark_partial=False)
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.update_from_response(response),
    )
    if finalize_tools:
        await completion_tools_ports()._publish_completion_tool_updates(
            redis=state.redis,
            user_id=state.user_id,
            channel=state.channel,
            task_id=state.task_id,
            message_id=state.message_id,
            attempt=state.attempt,
            attempt_epoch=state.attempt_epoch,
            tool_tracker=state.tool_tracker,
            updates=state.tool_tracker.finalize_active(ToolStatus.SUCCEEDED.value),
        )


async def _handle_terminal_event(
    state: CompletionExecution,
    event: dict[str, Any],
) -> None:
    event_type = event.get("type", "")
    raw_response = event.get("response")
    response = raw_response if isinstance(raw_response, dict) else {}
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.update_from_response(response),
    )
    terminal_status = (
        ToolStatus.CANCELLED.value
        if event_type in {"response.cancelled", "response.canceled"}
        else ToolStatus.FAILED.value
    )
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.finalize_active(
            terminal_status,
            error=completion_tools_ports()._summarize_tool_error(
                response.get("error")
                or response.get("incomplete_details")
                or event.get("error")
            ),
        ),
    )
    completion_upstream_ports()._raise_for_terminal_response_event(
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
    if not state.dispatch_started_recorded:
        await _record_completion_upstream_marker(state, response_received=False)
        state.dispatch_started_recorded = True
    stream = completion_upstream_ports().stream_completion(
        body,
        runtime_override=state.runtime_override,
    )
    async for event in completion_retry_ports()._iter_completion_stream_with_abort(
        stream,
        cancel_requested=state.cancel_requested,
        lease_lost=state.lease_lost,
        tool_tracker=state.tool_tracker,
        tool_idle_timeout_s=state.tool_idle_timeout_s,
    ):
        if not state.response_receipt_recorded:
            await _record_completion_upstream_marker(state, response_received=True)
            state.response_receipt_recorded = True
        if state.lease_lost.is_set():
            raise completion_retry_ports()._LeaseLost(
                f"lease lost during {phase} stream"
            )
        event_type = event.get("type", "")
        if event_type == "provider_used":
            provider_event = (
                completion_upstream_ports()._completion_upstream_provider_event(event)
            )
            if provider_event:
                state.upstream_provider_event = provider_event
                await completion_upstream_ports()._record_completion_upstream_metadata(
                    task_id=state.task_id,
                    attempt_epoch=state.attempt_epoch,
                    provider_event=provider_event,
                    fast_mode=state.fast_mode,
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
            state, completion_upstream_ports()._extract_reasoning_delta(event)
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
    async with completion_persistence_ports().SessionLocal() as session:
        completion = (
            await session.execute(
                completion_persistence_ports()
                .select(completion_persistence_ports().Completion)
                .where(
                    completion_persistence_ports().Completion.id == state.task_id,
                    completion_persistence_ports().Completion.attempt == state.attempt,
                    completion_persistence_ports().Completion.status
                    == CompletionStatus.STREAMING.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            raise completion_retry_ports()._CompletionEpochSuperseded(
                f"completion marker stale task={state.task_id} attempt={state.attempt}"
            )
        marker = (
            mark_upstream_response_received
            if response_received
            else mark_upstream_dispatch_started
        )
        completion.upstream_request = marker(
            completion,
            at=recorded_at,
            attempt=state.attempt,
        )
        await session.commit()


async def _consume_stream(state: CompletionExecution) -> None:
    if await completion_retry_ports()._is_cancelled(state.redis, state.task_id):
        raise completion_retry_ports()._TaskCancelled("cancelled before stream start")
    if state.lease_lost.is_set():
        raise completion_retry_ports()._LeaseLost("lease lost before stream start")
    cancel_requested = asyncio.Event()
    state.cancel_requested = cancel_requested
    state.cancel_stop_requested = asyncio.Event()
    state.cancel_watcher = asyncio.create_task(
        completion_retry_ports()._watch_completion_cancel(
            state.redis,
            state.task_id,
            cancel_requested=cancel_requested,
            stop_requested=state.cancel_stop_requested,
            poll_interval_s=state.cancel_poll_interval_s,
        )
    )
    state.request_sent = True
    state.round_text_start = len(state.accumulated_text)
    state.round_thinking_start = len(state.accumulated_thinking)
    state.usage_totals.start_round(
        input_fallback_tokens=completion_tools_ports()._estimate_completion_request_input_tokens(
            state.input_list,
            instructions=state.instructions,
        ),
        tool_output_tokens=completion_tools_ports()._estimate_completion_tool_output_tokens(
            state.tool_tracker.content()
        ),
    )
    await _consume_round(
        state,
        state.body,
        phase="primary",
        allow_tool_limit=True,
        track_tool_calls=True,
        append_completed_text=False,
        finalize_tools=False,
    )
    if state.tool_loop_truncated:
        state.usage_totals.finish_round(
            output_text=state.accumulated_text[state.round_text_start :],
            reasoning_text=state.accumulated_thinking[state.round_thinking_start :],
            tool_output_tokens=completion_tools_ports()._estimate_completion_tool_output_tokens(
                state.tool_tracker.content()
            ),
        )
        fallback_body = completion_tools_ports()._tool_limited_completion_body(
            state.body
        )
        state.round_text_start = len(state.accumulated_text)
        state.round_thinking_start = len(state.accumulated_thinking)
        state.usage_totals.start_round(
            input_fallback_tokens=completion_tools_ports()._estimate_completion_request_input_tokens(
                fallback_body["input"],
                instructions=fallback_body.get("instructions"),
            ),
            tool_output_tokens=completion_tools_ports()._estimate_completion_tool_output_tokens(
                state.tool_tracker.content()
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
    state.usage_totals.finish_round(
        output_text=state.accumulated_text[state.round_text_start :],
        reasoning_text=state.accumulated_thinking[state.round_thinking_start :],
        tool_output_tokens=completion_tools_ports()._estimate_completion_tool_output_tokens(
            state.tool_tracker.content()
        ),
    )


async def _cancel_completion_row(
    state: CompletionExecution,
) -> tuple[str, str, dict[str, Any]] | None:
    async with completion_persistence_ports().SessionLocal() as session:
        result = await session.execute(
            completion_persistence_ports()
            .update(completion_persistence_ports().Completion)
            .where(
                completion_persistence_ports().Completion.id == state.task_id,
                completion_persistence_ports().Completion.attempt
                == state.attempt_epoch,
                completion_persistence_ports().Completion.status.in_(
                    completion_retry_ports()._RUNNING_COMPLETION_STATUSES
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
        if completion_persistence_ports().affected_rows(result) == 0:
            raise completion_retry_ports()._CompletionEpochSuperseded(
                f"completion cancel superseded task={state.task_id} "
                f"attempt_epoch={state.attempt_epoch}"
            )
        message = await session.get(
            completion_persistence_ports().Message, state.message_id
        )
        if message is not None and message.status not in (
            MessageStatus.SUCCEEDED,
            MessageStatus.FAILED,
            MessageStatus.CANCELED,
        ):
            tool_calls = state.tool_tracker.content()
            if tool_calls:
                content = dict(message.content or {})
                content["tool_calls"] = tool_calls
                message.content = content
            message.status = MessageStatus.FAILED
        completion = await session.get(
            completion_persistence_ports().Completion, state.task_id
        )
        if completion is not None:
            await completion_billing_ports()._settle_cancelled_completion_billing(
                session,
                completion,
                has_partial=state.has_partial,
                input_list=state.input_list if state.request_sent else None,
                instructions=state.instructions if state.request_sent else None,
                usage_is_finalized=True,
                accumulated_text=state.accumulated_text,
                tokens_in=state.usage_totals.tokens_in,
                tokens_out=state.usage_totals.tokens_out,
                cache_read_tokens=state.usage_totals.cache_read_tokens,
                cache_creation_tokens=state.usage_totals.cache_creation_tokens,
                cache_creation_5m_tokens=state.usage_totals.cache_creation_5m_tokens,
                cache_creation_1h_tokens=state.usage_totals.cache_creation_1h_tokens,
                reasoning_tokens=state.usage_totals.reasoning_tokens,
                image_output_tokens=state.usage_totals.image_output_tokens,
                tool_images=state.tool_images,
                reserved_tool_image_budget_micro=(
                    state.reserved_tool_image_budget_micro
                ),
                reason=EC.CANCELLED.value,
            )
        delivery = completion_events_ports()._stage_completion_event(
            session,
            state.user_id,
            state.channel,
            EV_COMP_FAILED,
            completion_events_ports()._completion_event_payload(
                state.task_id,
                state.message_id,
                state.attempt,
                state.attempt_epoch,
                code="cancelled",
                message="cancelled by user",
                retriable=False,
            ),
        )
        await session.commit()
        await completion_billing_ports().worker_billing.flush_balance_cache_refreshes(
            session
        )
        return delivery


async def _settle_cancelled(state: CompletionExecution) -> None:
    state.usage_totals.finish_round(
        output_text=state.accumulated_text[state.round_text_start :],
        reasoning_text=state.accumulated_thinking[state.round_thinking_start :],
        tool_output_tokens=completion_tools_ports()._estimate_completion_tool_output_tokens(
            state.tool_tracker.content()
        ),
    )
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.finalize_active(ToolStatus.CANCELLED.value),
    )
    delivery: tuple[str, str, dict[str, Any]] | None = None
    try:
        delivery = await _cancel_completion_row(state)
    except completion_retry_ports()._CompletionEpochSuperseded as exc:
        completion_events_ports().logger.info(
            "completion cancel skipped by newer epoch task=%s attempt_epoch=%s err=%s",
            state.task_id,
            state.attempt_epoch,
            exc,
        )
        state.task_outcome = "superseded"
        return
    except Exception as exc:  # noqa: BLE001
        completion_events_ports().logger.warning(
            "completion cancel DB update failed task=%s err=%s",
            state.task_id,
            exc,
        )
    if delivery is not None:
        await completion_events_ports()._deliver_completion_event(state.redis, delivery)
    state.task_outcome = "failed"


def _failure_details(
    state: CompletionExecution,
    exc: BaseException,
) -> tuple[Any, str, str]:
    decision = completion_retry_ports()._classify_exception(exc, state.has_partial)
    _, byok_error = completion_billing_ports().classify_user_credential_error(exc)
    if state.user_api_credential_id and byok_error:
        decision = completion_retry_ports().RetryDecision(False, f"byok {byok_error}")
        err_code = completion_billing_ports().byok_error_to_generation_code(byok_error)
        err_msg = completion_billing_ports().byok_error_message(byok_error)
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
    async with completion_persistence_ports().SessionLocal() as session:
        result = await session.execute(
            completion_persistence_ports()
            .update(completion_persistence_ports().Completion)
            .where(
                completion_persistence_ports().Completion.id == state.task_id,
                completion_persistence_ports().Completion.attempt
                == state.attempt_epoch,
                completion_persistence_ports().Completion.status.in_(
                    completion_retry_ports()._RUNNING_COMPLETION_STATUSES
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
        if completion_persistence_ports().affected_rows(result) == 0:
            completion_events_ports().logger.info(
                "completion retry skipped by newer epoch task=%s attempt_epoch=%s",
                state.task_id,
                state.attempt_epoch,
            )
            state.task_outcome = "superseded"
            return False
    return True


async def _settle_retry_enqueue_failure(
    state: CompletionExecution,
    *,
    enqueue_msg: str,
) -> None:
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.finalize_active(
            ToolStatus.FAILED.value,
            error=enqueue_msg,
        ),
    )
    async with completion_persistence_ports().SessionLocal() as session:
        result = await session.execute(
            completion_persistence_ports()
            .update(completion_persistence_ports().Completion)
            .where(
                completion_persistence_ports().Completion.id == state.task_id,
                completion_persistence_ports().Completion.attempt
                == state.attempt_epoch,
                completion_persistence_ports().Completion.status
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
        if completion_persistence_ports().affected_rows(result) == 0:
            await session.commit()
            state.task_outcome = "superseded"
            return
        message = await session.get(
            completion_persistence_ports().Message, state.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            message.status = MessageStatus.FAILED
        completion = await session.get(
            completion_persistence_ports().Completion, state.task_id
        )
        if completion is not None:
            await completion_billing_ports().worker_billing.release_completion(
                session,
                completion,
                reason="retry_enqueue_failed",
            )
        delivery = completion_events_ports()._stage_completion_event(
            session,
            state.user_id,
            state.channel,
            EV_COMP_FAILED,
            completion_events_ports()._completion_event_payload(
                state.task_id,
                state.message_id,
                state.attempt,
                state.attempt_epoch,
                code="retry_enqueue_failed",
                message=enqueue_msg,
                retriable=False,
            ),
        )
        await session.commit()
        await completion_billing_ports().worker_billing.flush_balance_cache_refreshes(
            session
        )
    await completion_events_ports()._deliver_completion_event(state.redis, delivery)
    state.task_outcome = "failed"


async def _settle_terminal_failure(
    state: CompletionExecution,
    *,
    err_code: str,
    err_msg: str,
) -> None:
    await completion_tools_ports()._publish_completion_tool_updates(
        redis=state.redis,
        user_id=state.user_id,
        channel=state.channel,
        task_id=state.task_id,
        message_id=state.message_id,
        attempt=state.attempt,
        attempt_epoch=state.attempt_epoch,
        tool_tracker=state.tool_tracker,
        updates=state.tool_tracker.finalize_active(
            ToolStatus.FAILED.value,
            error=err_msg,
        ),
    )
    async with completion_persistence_ports().SessionLocal() as session:
        result = await session.execute(
            completion_persistence_ports()
            .update(completion_persistence_ports().Completion)
            .where(
                completion_persistence_ports().Completion.id == state.task_id,
                completion_persistence_ports().Completion.attempt
                == state.attempt_epoch,
                completion_persistence_ports().Completion.status.in_(
                    completion_retry_ports()._RUNNING_COMPLETION_STATUSES
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
        if completion_persistence_ports().affected_rows(result) == 0:
            await session.commit()
            state.task_outcome = "superseded"
            return
        message = await session.get(
            completion_persistence_ports().Message, state.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            tool_calls = state.tool_tracker.content()
            if tool_calls:
                content = dict(message.content or {})
                content["tool_calls"] = tool_calls
                message.content = content
            message.status = MessageStatus.FAILED
        if (
            state.has_partial
            or state.tool_loop_truncated
            or any(state.usage_totals.values())
        ):
            completion = await session.get(
                completion_persistence_ports().Completion, state.task_id
            )
            if completion is not None:
                if (
                    state.tool_images
                    and state.usage_totals.image_output_tokens <= 0
                    and state.reserved_tool_image_budget_micro > 0
                ):
                    state.usage_totals.image_output_tokens = await completion_billing_ports()._fallback_completion_tool_image_tokens(
                        session,
                        completion,
                        budget_micro=state.reserved_tool_image_budget_micro,
                    )
                    state.usage_totals.tokens_out = max(
                        state.usage_totals.tokens_out,
                        state.usage_totals.image_output_tokens,
                    )
                state.usage_totals.apply_to(completion)
                await completion_billing_ports()._settle_failed_completion_billing(
                    session,
                    completion,
                    usage_values=state.usage_totals.values(),
                    reason=str(err_code),
                )
        else:
            completion = await session.get(
                completion_persistence_ports().Completion, state.task_id
            )
            if completion is not None:
                await completion_billing_ports().worker_billing.release_completion(
                    session,
                    completion,
                    reason=str(err_code),
                )
        delivery = completion_events_ports()._stage_completion_event(
            session,
            state.user_id,
            state.channel,
            EV_COMP_FAILED,
            completion_events_ports()._completion_event_payload(
                state.task_id,
                state.message_id,
                state.attempt,
                state.attempt_epoch,
                code=err_code,
                message=err_msg,
                retriable=False,
            ),
        )
        await session.commit()
        await completion_billing_ports().worker_billing.flush_balance_cache_refreshes(
            session
        )
    await completion_events_ports()._deliver_completion_event(state.redis, delivery)
    state.task_outcome = "failed"


async def _handle_failure(
    state: CompletionExecution,
    exc: BaseException,
) -> None:
    if state.has_partial or state.tool_loop_truncated:
        state.usage_totals.finish_round(
            output_text=state.accumulated_text[state.round_text_start :],
            reasoning_text=state.accumulated_thinking[state.round_thinking_start :],
            tool_output_tokens=completion_tools_ports()._estimate_completion_tool_output_tokens(
                state.tool_tracker.content()
            ),
        )
    if isinstance(exc, completion_retry_ports()._ToolIdleTimeout):
        await completion_tools_ports()._publish_completion_tool_updates(
            redis=state.redis,
            user_id=state.user_id,
            channel=state.channel,
            task_id=state.task_id,
            message_id=state.message_id,
            attempt=state.attempt,
            attempt_epoch=state.attempt_epoch,
            tool_tracker=state.tool_tracker,
            updates=state.tool_tracker.finalize_active(
                ToolStatus.TIMED_OUT.value,
                error="tool call idle timeout",
            ),
        )
        exc = completion_upstream_ports().UpstreamError(
            "tool call idle timeout",
            error_code=EC.TIMEOUT.value,
            status_code=200,
        )
    completion_events_ports().upstream_calls_total.labels(
        kind="completion", outcome="error"
    ).inc()
    decision, err_code, err_msg = _failure_details(state, exc)
    _, byok_error = completion_billing_ports().classify_user_credential_error(exc)
    if state.user_api_credential_id and byok_error:
        await completion_billing_ports().record_user_credential_runtime_error(
            state.user_api_credential_id,
            exc,
        )
    completion_events_ports().logger.warning(
        "completion failed task=%s attempt=%s retriable=%s reason=%s "
        "error_code=%s http_status=%s",
        state.task_id,
        state.attempt,
        decision.retriable,
        decision.reason,
        err_code,
        getattr(exc, "status_code", None),
    )
    completion_events_ports().logger.debug(
        "completion exc trace task=%s", state.task_id, exc_info=True
    )
    if decision.retriable and state.attempt < completion_retry_ports()._MAX_ATTEMPTS:
        state.task_outcome = "retry"
        delay_index = min(
            state.attempt - 1,
            len(completion_retry_ports().RETRY_BACKOFF_SECONDS) - 1,
        )
        delay = completion_retry_ports().RETRY_BACKOFF_SECONDS[delay_index]
        if not await _mark_retry_queued(
            state,
            err_code=err_code,
            err_msg=err_msg,
        ):
            return
        try:
            await state.redis.enqueue_job(
                "run_completion",
                state.task_id,
                _defer_by=delay,
                _job_try=state.attempt + 1,
            )
        except Exception as enqueue_exc:  # noqa: BLE001
            completion_events_ports().logger.error(
                "re-enqueue failed task=%s err=%s",
                state.task_id,
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


async def _run_active_completion(state: CompletionExecution) -> None:
    if state.lease_lost.is_set():
        raise completion_retry_ports()._LeaseLost(
            "lease lost before completion start event"
        )
    await completion_events_ports().publish_event(
        state.redis,
        state.user_id,
        state.channel,
        EV_COMP_RESTARTED if state.was_restarted else EV_COMP_STARTED,
        _event_payload(state, **state.queue_metadata_payload),
    )
    if state.lease_lost.is_set():
        raise completion_retry_ports()._LeaseLost(
            "lease lost during completion start event"
        )
    await _prepare_request(state)
    await _consume_stream(state)
    await settle_success(state)


async def run_completion(ctx: dict[str, Any], task_id: str) -> None:
    """ARQ entrypoint; phases are split by context, stream, and terminal state."""
    state = _new_execution(ctx, task_id)
    try:
        if not await _claim_completion(state):
            return
        await _run_active_completion(state)
    except completion_retry_ports()._LeaseLost as exc:
        completion_events_ports().logger.warning(
            "completion lease lost task=%s attempt=%s err=%s",
            task_id,
            state.attempt,
            exc,
        )
        state.task_outcome = "lease_lost"
    except completion_retry_ports()._CompletionEpochSuperseded as exc:
        completion_events_ports().logger.info(
            "completion worker superseded task=%s err=%s", task_id, exc
        )
        state.task_outcome = "superseded"
    except completion_retry_ports()._TaskCancelled as exc:
        completion_events_ports().logger.info(
            "completion cancelled by user task=%s reason=%s",
            task_id,
            exc,
        )
        await _settle_cancelled(state)
    except Exception as exc:  # noqa: BLE001
        await _handle_failure(state, exc)
    finally:
        await completion_persistence_ports()._cleanup_completion_runtime(
            redis=state.redis,
            task_id=state.task_id,
            lease_token=state.lease_token,
            lease_acquired=state.lease_acquired,
            renewer=state.renewer,
            cancel_stop_requested=state.cancel_stop_requested,
            cancel_watcher=state.cancel_watcher,
            stream_span_cm=state.stream_span_cm,
            task_start=state.task_start,
            task_outcome=state.task_outcome,
        )


__all__ = ["CompletionExecution", "run_completion"]
