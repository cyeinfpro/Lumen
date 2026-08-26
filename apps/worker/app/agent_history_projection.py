"""Versioned typed Pi history projection for Agent Runtime requests."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.context_window import estimate_text_tokens
from lumen_core.agent_history import (
    AGENT_HISTORY_TEXT_LIMIT,
    agent_tool_history_result_text,
    estimate_agent_runtime_history_tokens,
    plan_agent_runtime_context,
)
from lumen_core.message_content import public_message_content
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
    AgentToolCall,
    Message,
)

from .agent_context_errors import AgentContextError
from .agent_reference_previews import current_turn_reference_rows, reference_previews
from .agent_runtime_client import (
    AgentRuntimeCompaction,
    AgentRuntimeHistoryImage,
    AgentRuntimeHistoryMessage,
    AgentRuntimeHistoryToolCall,
    AgentRuntimeHistoryToolResult,
    AgentRuntimeProviderEnvelope,
    AgentRuntimeReference,
)


_HISTORY_TEXT_LIMIT = AGENT_HISTORY_TEXT_LIMIT
_AGENT_APIS = frozenset(
    {"openai-responses", "openai-completions", "anthropic-messages"}
)


def _safe_text(value: Any, *, maximum: int = _HISTORY_TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized[:maximum]


def project_history_message(
    message: Message,
    *,
    run: AgentRun | None = None,
    tool_rows: list[AgentToolCall] | None = None,
    image_previews: list[AgentRuntimeReference] | None = None,
) -> AgentRuntimeHistoryMessage | None:
    content = public_message_content(
        message.content if isinstance(message.content, dict) else {}
    )
    text = _safe_text(content.get("text"))
    notes: list[str] = []
    attachments = content.get("attachments")
    if isinstance(attachments, list) and not image_previews:
        safe_attachments = [item for item in attachments if isinstance(item, dict)][:4]
        for index, item in enumerate(safe_attachments, 1):
            role = _safe_text(item.get("role"), maximum=32) or "reference"
            label = _safe_text(item.get("label"), maximum=80)
            suffix = f", label {label}" if label else ""
            notes.append(
                f"[Historical image attachment {index}: role {role}{suffix}; binary omitted]"
            )
    tools = content.get("tool_calls")
    if isinstance(tools, list) and not tool_rows:
        for item in [value for value in tools if isinstance(value, dict)][:8]:
            name = _safe_text(item.get("name"), maximum=64) or "tool"
            status = _safe_text(item.get("status"), maximum=32) or "unknown"
            mode = _safe_text(item.get("mode"), maximum=32)
            count = item.get("generation_count")
            details = f", mode {mode}" if mode else ""
            if isinstance(count, int) and not isinstance(count, bool):
                details += f", jobs {max(0, min(count, 4))}"
            notes.append(f"[Historical tool summary: {name}, status {status}{details}]")
    combined = "\n".join(part for part in (text, *notes) if part).strip()
    if not combined and not tool_rows and not image_previews:
        return None
    if not combined:
        combined = "Historical image turn" if image_previews else "Agent tool turn"
    role: Literal["user", "assistant"] = (
        "assistant" if message.role == "assistant" else "user"
    )
    typed_calls: list[AgentRuntimeHistoryToolCall] = []
    typed_results: list[AgentRuntimeHistoryToolResult] = []
    for tool in (tool_rows or [])[:8]:
        arguments = (
            dict(tool.arguments_jsonb) if isinstance(tool.arguments_jsonb, dict) else {}
        )
        typed_calls.append(
            AgentRuntimeHistoryToolCall(
                id=tool.pi_tool_call_id,
                name=tool.name,
                arguments=dict(list(arguments.items())[:32]),
            )
        )
        result = tool.result_jsonb if isinstance(tool.result_jsonb, dict) else {}
        generation_ids = [
            value
            for value in result.get("generation_ids", [])
            if isinstance(value, str)
        ][:4]
        typed_results.append(
            AgentRuntimeHistoryToolResult(
                tool_call_id=tool.pi_tool_call_id,
                name=tool.name,
                text=agent_tool_history_result_text(
                    status=tool.status,
                    mode=tool.mode,
                    generation_ids=generation_ids,
                    error_code=tool.error_code,
                ),
                is_error=tool.status != "succeeded",
            )
        )
    dispatch = (
        run.dispatch_jsonb if run and isinstance(run.dispatch_jsonb, dict) else {}
    )
    raw_api = dispatch.get("provider_api")
    api = raw_api if raw_api in _AGENT_APIS else None
    provider_id = None
    if run and run.provider_name:
        provider_id = (
            "lumen-history-"
            + hashlib.sha256(run.provider_name.encode("utf-8")).hexdigest()[:20]
        )
    final_text = combined[:_HISTORY_TEXT_LIMIT] if typed_calls else None
    return AgentRuntimeHistoryMessage(
        message_id=message.id,
        role=role,
        text=("Agent image tool request" if typed_calls else combined)[
            :_HISTORY_TEXT_LIMIT
        ],
        final_text=final_text,
        api=cast(Any, api),
        provider_id=provider_id,
        model=run.model[:256] if run and run.model else None,
        stop_reason=(
            ("toolUse" if typed_calls else "stop") if role == "assistant" else None
        ),
        tool_calls=typed_calls,
        tool_results=typed_results,
        images=[
            AgentRuntimeHistoryImage(
                mime_type=item.mime_type,
                data_base64=item.data_base64,
                estimated_input_tokens=item.estimated_input_tokens,
            )
            for item in image_previews or []
        ],
    )


def pack_history(
    rows: list[Message],
    *,
    provider: AgentRuntimeProviderEnvelope,
    system_prompt: str,
    current_prompt: str,
    max_output_tokens: int,
    references: list[AgentRuntimeReference] | None = None,
    reference_count: int | None = None,
    runs_by_assistant: dict[str, AgentRun] | None = None,
    tools_by_run: dict[str, list[AgentToolCall]] | None = None,
    images_by_message: dict[str, list[AgentRuntimeReference]] | None = None,
    compaction: AgentRuntimeCompaction | None = None,
) -> list[AgentRuntimeHistoryMessage]:
    reference_tokens = (
        sum(item.estimated_input_tokens or 2048 for item in references)
        if references is not None
        else max(0, int(reference_count or 0)) * 2048
    )
    fixed_tokens = (
        estimate_text_tokens(system_prompt)
        + estimate_text_tokens(current_prompt)
        + 2048
        + reference_tokens
    )
    projected: list[AgentRuntimeHistoryMessage] = []
    for row in rows:
        source_run = (runs_by_assistant or {}).get(row.id)
        item = project_history_message(
            row,
            run=source_run,
            tool_rows=(
                (tools_by_run or {}).get(source_run.id, []) if source_run else None
            ),
            image_previews=(images_by_message or {}).get(row.id),
        )
        if item is not None:
            projected.append(item)
    projected_token_counts = [
        estimate_agent_runtime_history_tokens(
            text=item.text,
            final_text=item.final_text,
            tool_arguments=(tool.arguments for tool in item.tool_calls),
            tool_result_texts=(result.text for result in item.tool_results),
            image_tokens=(
                image.estimated_input_tokens or 2048 for image in item.images
            ),
        )
        for item in projected
    ]
    projected_tokens = sum(projected_token_counts)
    compaction_tokens = estimate_text_tokens(compaction.summary) if compaction else 0
    context_plan = plan_agent_runtime_context(
        context_window=provider.context_window,
        max_output_tokens=max_output_tokens,
        fixed_input_tokens=fixed_tokens,
        history_tokens=projected_tokens + compaction_tokens,
        largest_history_entry_tokens=max(
            [compaction_tokens, *projected_token_counts]
        ),
    )
    if context_plan.mode == "impossible":
        raise AgentContextError("agent_context_window_exceeded")
    return projected


async def history_tool_projection(
    db: AsyncSession,
    rows: list[Message],
) -> tuple[dict[str, AgentRun], dict[str, list[AgentToolCall]]]:
    assistant_ids = [row.id for row in rows if row.role == "assistant"]
    if not assistant_ids:
        return {}, {}
    runs = list(
        (
            await db.execute(
                select(AgentRun).where(AgentRun.assistant_message_id.in_(assistant_ids))
            )
        )
        .scalars()
        .all()
    )
    tools = (
        list(
            (
                await db.execute(
                    select(AgentToolCall)
                    .where(AgentToolCall.agent_run_id.in_([run.id for run in runs]))
                    .order_by(AgentToolCall.agent_run_id, AgentToolCall.ordinal)
                )
            )
            .scalars()
            .all()
        )
        if runs
        else []
    )
    tools_by_run: dict[str, list[AgentToolCall]] = {}
    for tool in tools:
        tools_by_run.setdefault(tool.agent_run_id, []).append(tool)
    return {run.assistant_message_id: run for run in runs}, tools_by_run


async def history_image_projection(
    db: AsyncSession,
    rows: list[Message],
    *,
    run_user_id: str,
    visible_after: datetime | None,
    provider_api: str,
    redis: Any,
) -> dict[str, list[AgentRuntimeReference]]:
    user_messages = {row.id: row for row in rows if row.role == "user"}
    if not user_messages:
        return {}
    runs = list(
        (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.user_message_id.in_(list(user_messages))
                )
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return {}
    references = list(
        (
            await db.execute(
                select(AgentRunReference)
                .where(AgentRunReference.agent_run_id.in_([run.id for run in runs]))
                .order_by(AgentRunReference.agent_run_id, AgentRunReference.ordinal)
            )
        )
        .scalars()
        .all()
    )
    references_by_run: dict[str, list[AgentRunReference]] = {}
    for reference in references:
        references_by_run.setdefault(reference.agent_run_id, []).append(reference)
    projected: dict[str, list[AgentRuntimeReference]] = {}
    total_bytes = 0
    for source_run in runs:
        user_message = user_messages.get(source_run.user_message_id)
        if user_message is None:
            continue
        selected = current_turn_reference_rows(
            user_message, references_by_run.get(source_run.id, [])
        )
        previews = await reference_previews(
            db,
            selected,
            run_user_id=run_user_id,
            visible_after=visible_after,
            provider_api=provider_api,
            redis=redis,
        )
        total_bytes += sum(
            len(item.data_base64.encode("ascii")) * 3 // 4 for item in previews
        )
        if total_bytes > 8 * 1024 * 1024:
            raise AgentContextError("agent_history_transport_limit")
        if previews:
            projected[source_run.user_message_id] = previews
    return projected


__all__ = [
    "history_image_projection",
    "history_tool_projection",
    "pack_history",
    "project_history_message",
]
