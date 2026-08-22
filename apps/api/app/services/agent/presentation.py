"""Safe Agent public projections."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

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


_PUBLIC_ERROR_MESSAGES = MappingProxyType(
    {
        "agent_cancelled": "Agent run was cancelled",
        "agent_limit_reached": "Agent run reached a configured limit",
        "agent_provider_unavailable": "Agent provider is unavailable",
        "agent_runtime_unavailable": "Agent runtime is unavailable",
        "agent_vision_model_unavailable": "Image input is unavailable for this model",
        "agent_tool_result_unknown": "Image submission result is unknown",
        "INSUFFICIENT_BALANCE": "Insufficient wallet balance",
        "NO_ACTIVE_API_KEY": "No active API key is available",
    }
)


def public_agent_error_message(error_code: str | None) -> str | None:
    if not error_code:
        return None
    return _PUBLIC_ERROR_MESSAGES.get(error_code, "Agent run could not be completed")


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
) -> AgentRunOut:
    usage = run.usage_jsonb if isinstance(run.usage_jsonb, dict) else {}
    return AgentRunOut(
        id=run.id,
        agent_session_id=run.agent_session_id,
        user_message_id=run.user_message_id,
        assistant_message_id=run.assistant_message_id,
        status=run.status,
        execution_epoch=run.execution_epoch,
        last_event_seq=run.last_event_seq,
        idempotency_key=run.idempotency_key,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        turn_count=run.turn_count,
        tool_call_count=run.tool_call_count,
        usage=usage,
        error_code=run.error_code,
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
) -> tuple[AgentImageDefaultsIn, bool]:
    params = (
        conversation.default_params
        if isinstance(conversation.default_params, dict)
        else {}
    )
    raw_agent = params.get("agent")
    if not isinstance(raw_agent, dict):
        return AgentImageDefaultsIn(), True
    try:
        defaults = AgentImageDefaultsIn.model_validate(
            raw_agent.get("image_defaults", {})
        )
    except (TypeError, ValueError):
        defaults = AgentImageDefaultsIn()
    allow_image = raw_agent.get("allow_image")
    return defaults, allow_image if isinstance(allow_image, bool) else True


def agent_session_out(
    session: AgentSession,
    conversation: Conversation,
    *,
    active_run: AgentRunOut | None = None,
) -> AgentSessionOut:
    image_defaults, allow_image = conversation_agent_defaults(conversation)
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
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params = dict(existing or {})
    params["agent"] = {
        "image_defaults": image_defaults.model_dump(mode="json"),
        "allow_image": allow_image,
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
