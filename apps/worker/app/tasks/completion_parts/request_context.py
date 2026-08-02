"""Completion request-context loading with explicit transaction boundaries."""

from __future__ import annotations

from typing import Any

from lumen_core.constants import DEFAULT_CHAT_INSTRUCTIONS

from ...reconciliation.task_domains import completion_execution_epoch
from .execution import CompletionExecution


async def _record_memory_metadata(
    state: CompletionExecution,
    memory_meta: dict[str, Any],
) -> None:
    if not memory_meta.get("used_memory_ids"):
        return
    async with state.ports.persistence.SessionLocal() as session:
        completion = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(
                    state.ports.persistence.Completion.id == state.request.task_id,
                    state.ports.persistence.Completion.attempt
                    == state.preparation.attempt_epoch,
                    state.ports.persistence.Completion.execution_epoch
                    == completion_execution_epoch(state),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            return
        upstream_request = dict(completion.upstream_request or {})
        upstream_request["memory"] = memory_meta
        completion.upstream_request = upstream_request
        await session.commit()


async def load_request_context(state: CompletionExecution) -> None:
    state.streaming.instructions = (
        state.preparation.system_prompt or DEFAULT_CHAT_INSTRUCTIONS
    )
    packed = None
    parent_message_id = None
    parent_content: dict[str, Any] | None = None
    async with state.ports.persistence.SessionLocal() as session:
        state.preparation.target_msg = await session.get(
            state.ports.persistence.Message,
            state.preparation.message_id,
        )
        if state.preparation.target_msg is not None:
            parent_message_id = state.preparation.target_msg.parent_message_id
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
            await state.ports.context._record_completion_context_metadata(
                session,
                task_id=state.request.task_id,
                attempt_epoch=state.preparation.attempt_epoch,
                packed=packed,
            )
        if parent_message_id:
            parent = await session.get(
                state.ports.persistence.Message,
                parent_message_id,
            )
            if parent is not None and isinstance(parent.content, dict):
                parent_content = dict(parent.content)

    if packed is not None:
        memory_meta = await state.ports.context._inject_user_memory_context(
            None,
            input_list=state.streaming.input_list,
            user_id=state.preparation.user_id,
            conversation_id=state.preparation.conversation_id,
            parent_user_message_id=parent_message_id,
            redis=state.request.redis,
        )
        state.usage.memory_meta_for_event = memory_meta
        await _record_memory_metadata(state, memory_meta)

    if parent_content is None:
        return
    effort = parent_content.get("reasoning_effort")
    if effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
        state.preparation.reasoning_effort = effort
    state.preparation.fast_mode = parent_content.get("fast") is True
    state.streaming.chat_tools = await state.ports.tools._chat_tools_from_content(
        parent_content
    )
