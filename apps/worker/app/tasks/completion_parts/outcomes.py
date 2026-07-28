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


async def _maybe_enqueue_auto_title(redis: Any, conversation_id: str) -> None:
    from ..auto_title import maybe_enqueue_auto_title

    await maybe_enqueue_auto_title(redis, conversation_id)


def _final_text(state: Any) -> str:
    if state.streaming.tool_loop_truncated and state.streaming.accumulated_text:
        final_text = state.ports.upstream._apply_url_citations(
            state.streaming.accumulated_text,
            state.ports.upstream._extract_url_citations(
                state.usage.completed_response or {}
            ),
        )
    else:
        final_text = state.ports.upstream._finalize_completion_text(
            state.streaming.accumulated_text,
            state.usage.completed_response,
        )
    if not final_text and state.streaming.tool_images:
        return "已生成图片。"
    if not final_text:
        raise state.ports.upstream.UpstreamError(
            "upstream returned empty completion",
            error_code=EC.NO_TEXT_RETURNED.value,
            status_code=200,
        )
    return final_text


async def _persist_success(
    state: Any,
    final_text: str,
) -> tuple[tuple[str, str, dict[str, Any]], Any]:
    await state.ports.tools._publish_completion_tool_updates(
        redis=state.request.redis,
        user_id=state.preparation.user_id,
        channel=state.request.channel,
        task_id=state.request.task_id,
        message_id=state.preparation.message_id,
        attempt=state.preparation.attempt,
        attempt_epoch=state.preparation.attempt_epoch,
        tool_tracker=state.usage.tool_tracker,
        updates=state.usage.tool_tracker.finalize_active(ToolStatus.SUCCEEDED.value),
    )
    async with state.ports.persistence.SessionLocal() as session:
        completion_for_usage = await session.get(
            state.ports.persistence.Completion, state.request.task_id
        )
        if (
            completion_for_usage is not None
            and completion_for_usage.attempt == state.preparation.attempt_epoch
            and completion_for_usage.status
            in state.ports.retry._RUNNING_COMPLETION_STATUSES
            and state.streaming.tool_images
            and state.usage.usage_totals.image_output_tokens <= 0
            and state.streaming.reserved_tool_image_budget_micro > 0
        ):
            state.usage.usage_totals.image_output_tokens = (
                await state.ports.billing._fallback_completion_tool_image_tokens(
                    session,
                    completion_for_usage,
                    budget_micro=state.streaming.reserved_tool_image_budget_micro,
                )
            )
            state.usage.usage_totals.tokens_out = max(
                state.usage.usage_totals.tokens_out,
                state.usage.usage_totals.image_output_tokens,
            )
        result = await session.execute(
            state.ports.persistence
            .update(state.ports.persistence.Completion)
            .where(
                state.ports.persistence.Completion.id == state.request.task_id,
                state.ports.persistence.Completion.attempt
                == state.preparation.attempt_epoch,
                state.ports.persistence.Completion.status.in_(
                    state.ports.retry._RUNNING_COMPLETION_STATUSES
                ),
            )
            .values(
                status=CompletionStatus.SUCCEEDED.value,
                progress_stage=CompletionStage.FINALIZING,
                text=final_text,
                **state.usage.usage_totals.model_values(),
                finished_at=datetime.now(timezone.utc),
                error_code=None,
                error_message=None,
            )
        )
        if state.ports.persistence.affected_rows(result) == 0:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion epoch superseded before success task={state.request.task_id} "
                f"attempt_epoch={state.preparation.attempt_epoch}"
            )
        message = await session.get(
            state.ports.persistence.Message, state.preparation.message_id
        )
        if message is not None and message.status != MessageStatus.CANCELED:
            content = dict(message.content or {})
            content["text"] = final_text
            if state.streaming.accumulated_thinking:
                content["thinking"] = state.streaming.accumulated_thinking
            tool_calls = state.usage.tool_tracker.content()
            if tool_calls:
                content["tool_calls"] = tool_calls
            if state.usage.memory_meta_for_event.get("used_memory_ids"):
                content["used_memory_ids"] = state.usage.memory_meta_for_event.get(
                    "used_memory_ids",
                    [],
                )
                content["used_memory_summary"] = state.usage.memory_meta_for_event.get(
                    "used_memory_summary",
                    [],
                )
                if state.usage.memory_meta_for_event.get("confirmation_candidate_id"):
                    content["confirmation_candidate_id"] = (
                        state.usage.memory_meta_for_event.get("confirmation_candidate_id")
                    )
            message.content = content
            message.status = MessageStatus.SUCCEEDED
        completion_for_billing = await session.get(
            state.ports.persistence.Completion, state.request.task_id
        )
        if completion_for_billing is not None:
            upstream_request = dict(completion_for_billing.upstream_request or {})
            upstream_request = (
                state.ports.upstream._merge_completion_upstream_metadata(
                    upstream_request,
                    provider_event=state.usage.upstream_provider_event,
                    fast_mode=state.preparation.fast_mode,
                )
            )
            completion_for_billing.upstream_request = upstream_request or None
            state.usage.usage_totals.apply_to(completion_for_billing)
            await state.ports.retry._raise_if_completion_cancelled(
                state.request.redis,
                state.request.task_id,
                "cancelled before billing settle",
            )
            await state.ports.billing.worker_billing.charge_completion(
                session,
                completion_for_billing,
            )
            # charge 之后不再检查取消。此处一旦抛 TaskCancelled，session 连同
            # 刚写入的扣费流水一起回滚，而 Completion 也没能标成 SUCCEEDED；
            # 随后 _settle_cancelled 会在状态不匹配的前提下二次处理这笔钱。
            # 取消只允许在 charge 之前生效。
        success_delivery = state.ports.events._stage_completion_event(
            session,
            state.preparation.user_id,
            state.request.channel,
            EV_COMP_SUCCEEDED,
            state.ports.events._completion_event_payload(
                state.request.task_id,
                state.preparation.message_id,
                state.preparation.attempt,
                state.preparation.attempt_epoch,
                text=final_text,
                tokens_in=state.usage.usage_totals.tokens_in,
                tokens_out=state.usage.usage_totals.tokens_out,
                tool_calls=state.usage.tool_tracker.content(),
                tool_loop_truncated=state.streaming.tool_loop_truncated,
                used_memory_ids=state.usage.memory_meta_for_event.get(
                    "used_memory_ids",
                    [],
                ),
                used_memory_summary=state.usage.memory_meta_for_event.get(
                    "used_memory_summary",
                    [],
                ),
                confirmation_candidate_id=state.usage.memory_meta_for_event.get(
                    "confirmation_candidate_id"
                ),
            ),
        )
        memory_delivery = await state.ports.tools._completion_tool_images._stage_completion_memory_extract(
            session,
            feature_enabled=state.ports.events.memory_extraction is not None,
            user_id=state.preparation.user_id,
            conversation_id=state.preparation.conversation_id,
            source_message_id=(
                getattr(message, "parent_message_id", None)
                if message is not None
                else None
            ),
            assistant_message_id=state.preparation.message_id,
            hooks=state.ports.events._COMPLETION_EVENT_HOOKS,
        )
        await session.commit()
        await state.ports.billing.worker_billing.flush_balance_cache_refreshes(
            session
        )

    return success_delivery, memory_delivery


async def settle_success(state: Any) -> None:
    final_text = _final_text(state)
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before success commit")
    await state.ports.retry._raise_if_completion_cancelled(
        state.request.redis,
        state.request.task_id,
        "cancelled before success commit",
    )
    success_delivery, memory_delivery = await _persist_success(state, final_text)
    await state.ports.events._deliver_completion_event(
        state.request.redis, success_delivery
    )
    if memory_delivery is not None:
        await state.ports.events._deliver_completion_event(
            state.request.redis, memory_delivery
        )
    state.settlement.task_outcome = "succeeded"
    state.ports.events.upstream_calls_total.labels(
        kind="completion", outcome="ok"
    ).inc()
    if state.preparation.conversation_id:
        await _maybe_enqueue_auto_title(state.request.redis, state.preparation.conversation_id)


__all__ = ["settle_success"]
