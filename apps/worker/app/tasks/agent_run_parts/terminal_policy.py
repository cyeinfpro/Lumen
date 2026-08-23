"""Pure terminal-state projection policy for Agent runs."""

from __future__ import annotations

from typing import Any

from lumen_core.agent_events import (
    EV_AGENT_RUN_CANCELLED,
    EV_AGENT_RUN_FAILED,
    EV_AGENT_RUN_PARTIAL,
    EV_AGENT_RUN_SUCCEEDED,
    AgentRunStatus,
)
from lumen_core.model_entities import AgentToolCall, Message


def has_partial_result(
    message: Message | None,
    text: str,
    *,
    has_side_effect: bool,
) -> bool:
    if has_side_effect or text.strip():
        return True
    if message is None or not isinstance(message.content, dict):
        return False
    persisted = message.content.get("text")
    return isinstance(persisted, str) and bool(persisted.strip())


def tool_projection(tool: AgentToolCall) -> dict[str, Any]:
    result = tool.result_jsonb if isinstance(tool.result_jsonb, dict) else {}
    generation_ids = result.get("generation_ids")
    safe_ids = (
        [value for value in generation_ids if isinstance(value, str)][:4]
        if isinstance(generation_ids, list)
        else []
    )
    return {
        "id": tool.id,
        "name": tool.name,
        "label": "Create image",
        "mode": tool.mode,
        "status": tool.status,
        "generation_ids": safe_ids,
        "generation_count": len(safe_ids),
        **({"error_code": tool.error_code} if tool.error_code else {}),
    }


def terminal_event_name(status: str) -> str:
    return {
        AgentRunStatus.SUCCEEDED.value: EV_AGENT_RUN_SUCCEEDED,
        AgentRunStatus.PARTIAL.value: EV_AGENT_RUN_PARTIAL,
        AgentRunStatus.CANCELLED.value: EV_AGENT_RUN_CANCELLED,
    }.get(status, EV_AGENT_RUN_FAILED)


__all__ = ["has_partial_result", "terminal_event_name", "tool_projection"]
