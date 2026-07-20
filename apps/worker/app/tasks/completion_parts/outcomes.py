"""Completion success settlement phase."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import (
    EV_COMP_SUCCEEDED,
    CompletionStage,
    CompletionStatus,
    GenerationErrorCode as EC,
    MessageStatus,
)
from lumen_core.chat_tools import ToolStatus
from ._facade import _g
from ..auto_title import maybe_enqueue_auto_title


def _final_text(state: Any) -> str:
    if state.tool_loop_truncated and state.accumulated_text:
        final_text = _g._apply_url_citations(
            state.accumulated_text,
            _g._extract_url_citations(state.completed_response or {}),
        )
    else:
        final_text = _g._finalize_completion_text(
            state.accumulated_text,
            state.completed_response,
        )
    if not final_text and state.tool_images:
        return "已生成图片。"
    if not final_text:
        raise _g.UpstreamError(
            "upstream returned empty completion",
            error_code=EC.NO_TEXT_RETURNED.value,
            status_code=200,
        )
    return final_text


async def _persist_success(
    state: Any,
    final_text: str,
) -> tuple[tuple[str, str, dict[str, Any]], Any]:
    await _g._publish_completion_tool_updates(
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
    async with _g.SessionLocal() as session:
        completion_for_usage = await session.get(_g.Completion, state.task_id)
        if (
            completion_for_usage is not None
            and completion_for_usage.attempt == state.attempt_epoch
            and completion_for_usage.status in _g._RUNNING_COMPLETION_STATUSES
            and state.tool_images
            and state.usage_totals.image_output_tokens <= 0
            and state.reserved_tool_image_budget_micro > 0
        ):
            state.usage_totals.image_output_tokens = (
                await _g._fallback_completion_tool_image_tokens(
                    session,
                    completion_for_usage,
                    budget_micro=state.reserved_tool_image_budget_micro,
                )
            )
            state.usage_totals.tokens_out = max(
                state.usage_totals.tokens_out,
                state.usage_totals.image_output_tokens,
            )
        result = await session.execute(
            _g.update(_g.Completion)
            .where(
                _g.Completion.id == state.task_id,
                _g.Completion.attempt == state.attempt_epoch,
                _g.Completion.status.in_(_g._RUNNING_COMPLETION_STATUSES),
            )
            .values(
                status=CompletionStatus.SUCCEEDED.value,
                progress_stage=CompletionStage.FINALIZING,
                text=final_text,
                **state.usage_totals.model_values(),
                finished_at=datetime.now(timezone.utc),
                error_code=None,
                error_message=None,
            )
        )
        if _g.affected_rows(result) == 0:
            raise _g._CompletionEpochSuperseded(
                f"completion epoch superseded before success task={state.task_id} "
                f"attempt_epoch={state.attempt_epoch}"
            )
        message = await session.get(_g.Message, state.message_id)
        if message is not None and message.status != MessageStatus.CANCELED:
            content = dict(message.content or {})
            content["text"] = final_text
            if state.accumulated_thinking:
                content["thinking"] = state.accumulated_thinking
            tool_calls = state.tool_tracker.content()
            if tool_calls:
                content["tool_calls"] = tool_calls
            if state.memory_meta_for_event.get("used_memory_ids"):
                content["used_memory_ids"] = state.memory_meta_for_event.get(
                    "used_memory_ids",
                    [],
                )
                content["used_memory_summary"] = state.memory_meta_for_event.get(
                    "used_memory_summary",
                    [],
                )
                if state.memory_meta_for_event.get("confirmation_candidate_id"):
                    content["confirmation_candidate_id"] = (
                        state.memory_meta_for_event.get("confirmation_candidate_id")
                    )
            message.content = content
            message.status = MessageStatus.SUCCEEDED
        completion_for_billing = await session.get(_g.Completion, state.task_id)
        if completion_for_billing is not None:
            upstream_request = dict(completion_for_billing.upstream_request or {})
            upstream_request = _g._merge_completion_upstream_metadata(
                upstream_request,
                provider_event=state.upstream_provider_event,
                fast_mode=state.fast_mode,
            )
            completion_for_billing.upstream_request = upstream_request or None
            state.usage_totals.apply_to(completion_for_billing)
            await _g._raise_if_completion_cancelled(
                state.redis,
                state.task_id,
                "cancelled before billing settle",
            )
            await _g.worker_billing.charge_completion(
                session,
                completion_for_billing,
            )
            await _g._raise_if_completion_cancelled(
                state.redis,
                state.task_id,
                "cancelled before success commit",
            )
        success_delivery = _g._stage_completion_event(
            session,
            state.user_id,
            state.channel,
            EV_COMP_SUCCEEDED,
            _g._completion_event_payload(
                state.task_id,
                state.message_id,
                state.attempt,
                state.attempt_epoch,
                text=final_text,
                tokens_in=state.usage_totals.tokens_in,
                tokens_out=state.usage_totals.tokens_out,
                tool_calls=state.tool_tracker.content(),
                tool_loop_truncated=state.tool_loop_truncated,
                used_memory_ids=state.memory_meta_for_event.get(
                    "used_memory_ids",
                    [],
                ),
                used_memory_summary=state.memory_meta_for_event.get(
                    "used_memory_summary",
                    [],
                ),
                confirmation_candidate_id=state.memory_meta_for_event.get(
                    "confirmation_candidate_id"
                ),
            ),
        )
        memory_delivery = (
            await _g._completion_tool_images._stage_completion_memory_extract(
                session,
                feature_enabled=_g.memory_extraction is not None,
                user_id=state.user_id,
                conversation_id=state.conversation_id,
                source_message_id=(
                    getattr(message, "parent_message_id", None)
                    if message is not None
                    else None
                ),
                assistant_message_id=state.message_id,
                hooks=_g._COMPLETION_EVENT_HOOKS,
            )
        )
        await session.commit()
        await _g.worker_billing.flush_balance_cache_refreshes(session)

    return success_delivery, memory_delivery


async def settle_success(state: Any) -> None:
    final_text = _final_text(state)
    if state.lease_lost.is_set():
        raise _g._LeaseLost("lease lost before success commit")
    await _g._raise_if_completion_cancelled(
        state.redis,
        state.task_id,
        "cancelled before success commit",
    )
    success_delivery, memory_delivery = await _persist_success(state, final_text)
    await _g._deliver_completion_event(state.redis, success_delivery)
    if memory_delivery is not None:
        await _g._deliver_completion_event(state.redis, memory_delivery)
    state.task_outcome = "succeeded"
    _g.upstream_calls_total.labels(kind="completion", outcome="ok").inc()
    if state.conversation_id:
        await maybe_enqueue_auto_title(state.redis, state.conversation_id)


__all__ = ["settle_success"]
