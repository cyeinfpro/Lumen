"""Pure terminal-state projection policy for Agent runs."""

from __future__ import annotations

from typing import Any, Literal

from lumen_core.agent_events import (
    EV_AGENT_RUN_CANCELLED,
    EV_AGENT_RUN_FAILED,
    EV_AGENT_RUN_PARTIAL,
    EV_AGENT_RUN_SUCCEEDED,
    AgentRunStatus,
)
from lumen_core.agent_dispatch import (
    provider_dispatch_authorized_count,
    provider_dispatch_checkpointed_count,
)
from lumen_core.model_entities import AgentToolCall, Message

from .contracts import AGENT_NO_COST_HTTP_STATUSES, AgentRuntimeAccumulator


def cancelled_dispatch_unknown(dispatch: dict[str, Any]) -> bool:
    delivery = str(dispatch.get("runtime_delivery") or "")
    response_statuses = [
        value
        for value in dispatch.get("provider_response_statuses", [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    checkpointed = provider_dispatch_checkpointed_count(dispatch)
    authorized = provider_dispatch_authorized_count(dispatch)
    dispatch_count = max(checkpointed, authorized)
    completed = max(0, int(dispatch.get("provider_completed_count") or 0))
    pending = max(0, dispatch_count - completed)
    pending_statuses = response_statuses[-pending:] if pending else []
    pending_proven_absent = (
        pending > 0
        and len(pending_statuses) == pending
        and all(value in AGENT_NO_COST_HTTP_STATUSES for value in pending_statuses)
    )
    return (
        authorized > checkpointed
        or (pending > 0 and not pending_proven_absent)
        or (dispatch_count == 0 and delivery in {"starting", "unknown"})
    )


def terminal_request(
    accumulator: AgentRuntimeAccumulator,
) -> tuple[
    Literal["succeeded", "partial", "failed", "cancelled"],
    str | None,
    Literal["actual", "proven_absent", "unknown"],
    str,
]:
    unresolved = accumulator.has_unresolved_dispatch
    if accumulator.terminal_status == "cancelled":
        knowledge: Literal["actual", "proven_absent", "unknown"] = (
            "unknown"
            if unresolved
            else "actual"
            if accumulator.has_exact_usage
            else "proven_absent"
            if accumulator.provider_dispatch_count == 0
            else "unknown"
        )
        return "cancelled", "agent_cancelled", knowledge, "runtime_cancelled"
    if accumulator.terminal_status == "partial":
        knowledge = (
            "unknown" if unresolved or not accumulator.has_exact_usage else "actual"
        )
        return (
            "partial",
            accumulator.terminal_error_code or "agent_result_unknown",
            knowledge,
            "runtime_partial",
        )
    if accumulator.terminal_status == "succeeded":
        if unresolved:
            return (
                "succeeded",
                None,
                "unknown",
                "runtime_success_with_unknown_billing",
            )
        return "succeeded", None, "actual", "runtime_success"
    if accumulator.has_exact_usage and not unresolved:
        return (
            "failed",
            accumulator.terminal_error_code or "agent_provider_error",
            "actual",
            "runtime_failed_with_usage",
        )
    if accumulator.response_proves_no_cost and not unresolved:
        return (
            "failed",
            accumulator.terminal_error_code or "agent_provider_error",
            "proven_absent",
            "provider_rejected_before_usage",
        )
    if (
        accumulator.terminal_status == "failed"
        and accumulator.provider_dispatch_count == 0
    ):
        return (
            "failed",
            accumulator.terminal_error_code or "agent_runtime_error",
            "proven_absent",
            "runtime_failed_before_provider_dispatch",
        )
    return (
        "failed",
        accumulator.terminal_error_code or "agent_result_unknown",
        "unknown",
        "provider_result_unknown",
    )


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


__all__ = [
    "cancelled_dispatch_unknown",
    "has_partial_result",
    "terminal_event_name",
    "terminal_request",
    "tool_projection",
]
