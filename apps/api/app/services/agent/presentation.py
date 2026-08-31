"""Safe Agent public projections."""

from __future__ import annotations

from typing import Any

from lumen_core.agent_dispatch import provider_dispatch_evidence_count
from lumen_core.agent_errors import (
    agent_error_allows_continuation,
    public_agent_error_code,
    public_agent_error_message,
)
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
    Conversation,
)
from lumen_core.schema_models import (
    AgentImageDefaultsIn,
    AgentReferenceOut,
    AgentRunOut,
    AgentSessionOut,
    AgentToolCallOut,
)


def generation_ids_from_tool(tool_call: AgentToolCall) -> list[str]:
    result = tool_call.result_jsonb if isinstance(tool_call.result_jsonb, dict) else {}
    values = result.get("generation_ids")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value][:4]


def agent_reference_out(reference: AgentRunReference) -> AgentReferenceOut:
    return AgentReferenceOut(
        id=reference.id,
        image_id=reference.image_id,
        ordinal=reference.ordinal,
        reference_label=reference.reference_label,
        role=reference.role,
        display_label=reference.display_label,
    )


def agent_tool_call_out(tool_call: AgentToolCall) -> AgentToolCallOut:
    generation_ids = generation_ids_from_tool(tool_call)
    return AgentToolCallOut(
        id=tool_call.id,
        agent_run_id=tool_call.agent_run_id,
        ordinal=tool_call.ordinal,
        name=tool_call.name,
        mode=tool_call.mode,
        status=tool_call.status,
        generation_ids=generation_ids,
        generation_count=len(generation_ids),
        error_code=tool_call.error_code,
        started_at=tool_call.started_at,
        finished_at=tool_call.finished_at,
        created_at=tool_call.created_at,
        updated_at=tool_call.updated_at,
    )


def agent_run_out(
    run: AgentRun,
    *,
    references: list[AgentRunReference] | None = None,
    tool_calls: list[AgentToolCall] | None = None,
    is_latest: bool = False,
) -> AgentRunOut:
    usage = run.usage_jsonb if isinstance(run.usage_jsonb, dict) else {}
    dispatch = run.dispatch_jsonb if isinstance(run.dispatch_jsonb, dict) else {}
    memory_state = dispatch.get("memory_state")
    if memory_state not in {"disabled", "empty", "ready", "degraded"}:
        memory_state = None
    unresolved_tool = any(
        tool.status in {"queued", "running", "timed_out"}
        or tool.error_code == "agent_tool_result_unknown"
        for tool in tool_calls or []
    )
    billing = run.billing_jsonb if isinstance(run.billing_jsonb, dict) else {}
    provider_evidence_safe = provider_dispatch_evidence_count(
        dispatch
    ) == 0 or billing.get("knowledge") in {"actual", "proven_absent"}
    transcript = run.transcript_jsonb if isinstance(run.transcript_jsonb, dict) else {}
    transcript_coherent = transcript.get("projection") != "ordered_blocks" or (
        transcript.get("output_revision") == int(run.output_revision or 0)
        and transcript.get("output_runtime_seq") == int(run.output_runtime_seq or 0)
    )
    continuable = (
        is_latest
        and run.status == "partial"
        and agent_error_allows_continuation(run.error_code)
        and not unresolved_tool
        and provider_evidence_safe
        and transcript_coherent
    )
    public_error = public_agent_error_code(run.error_code)
    return AgentRunOut(
        id=run.id,
        agent_session_id=run.agent_session_id,
        user_message_id=run.user_message_id,
        assistant_message_id=run.assistant_message_id,
        status=run.status,
        execution_epoch=run.execution_epoch,
        last_event_seq=run.last_event_seq,
        output_revision=int(run.output_revision or 0),
        output_runtime_seq=int(run.output_runtime_seq or 0),
        idempotency_key=run.idempotency_key,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        memory_state=memory_state,
        continuable=continuable,
        turn_count=run.turn_count,
        tool_call_count=run.tool_call_count,
        usage=usage,
        error_code=public_error,
        error_message=public_agent_error_message(run.error_code),
        started_at=run.started_at,
        finished_at=run.finished_at,
        cancel_requested_at=run.cancel_requested_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
        references=[agent_reference_out(item) for item in references or []],
        tool_calls=[agent_tool_call_out(item) for item in tool_calls or []],
    )


def conversation_agent_defaults(
    conversation: Conversation,
) -> tuple[AgentImageDefaultsIn, bool, bool, bool]:
    params = (
        conversation.default_params
        if isinstance(conversation.default_params, dict)
        else {}
    )
    raw_agent = params.get("agent")
    if not isinstance(raw_agent, dict):
        return AgentImageDefaultsIn(), True, False, True
    try:
        defaults = AgentImageDefaultsIn.model_validate(
            raw_agent.get("image_defaults", {})
        )
    except (TypeError, ValueError):
        defaults = AgentImageDefaultsIn()
    allow_image = raw_agent.get("allow_image")
    allow_web_search = raw_agent.get("allow_web_search")
    allow_file_tools = raw_agent.get("allow_file_tools")
    return (
        defaults,
        allow_image if isinstance(allow_image, bool) else True,
        allow_web_search if isinstance(allow_web_search, bool) else False,
        allow_file_tools if isinstance(allow_file_tools, bool) else True,
    )


def agent_session_out(
    session: AgentSession,
    conversation: Conversation,
    *,
    active_run: AgentRunOut | None = None,
) -> AgentSessionOut:
    (
        image_defaults,
        allow_image,
        allow_web_search,
        allow_file_tools,
    ) = conversation_agent_defaults(conversation)
    return AgentSessionOut(
        id=session.id,
        conversation_id=conversation.id,
        title=conversation.title,
        pinned=conversation.pinned,
        archived=conversation.archived,
        memory_disabled=conversation.memory_disabled,
        active_scope_id=conversation.active_scope_id,
        default_system=conversation.default_system,
        default_system_prompt_id=conversation.default_system_prompt_id,
        image_defaults=image_defaults,
        allow_image=allow_image,
        allow_web_search=allow_web_search,
        allow_file_tools=allow_file_tools,
        runtime_version=session.runtime_version,
        last_activity_at=conversation.last_activity_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        active_run=active_run,
    )


def agent_default_params(
    *,
    image_defaults: AgentImageDefaultsIn,
    allow_image: bool,
    allow_web_search: bool,
    allow_file_tools: bool,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(existing or {})
    params["agent"] = {
        "image_defaults": image_defaults.model_dump(mode="json"),
        "allow_image": allow_image,
        "allow_web_search": allow_web_search,
        "allow_file_tools": allow_file_tools,
    }
    return params


__all__ = [
    "agent_default_params",
    "agent_reference_out",
    "agent_run_out",
    "agent_session_out",
    "agent_tool_call_out",
    "conversation_agent_defaults",
    "generation_ids_from_tool",
    "public_agent_error_message",
]
