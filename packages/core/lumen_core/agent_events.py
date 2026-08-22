"""Shared Agent state-machine and realtime event contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .immutables import immutable_mapping


AGENT_TOOL_CREATE_IMAGE = "lumen_create_image"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentToolCallStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


AGENT_RUN_TERMINAL_STATUSES = frozenset(
    {
        AgentRunStatus.SUCCEEDED.value,
        AgentRunStatus.PARTIAL.value,
        AgentRunStatus.FAILED.value,
        AgentRunStatus.CANCELLED.value,
    }
)
AGENT_RUN_ACTIVE_STATUSES = frozenset(
    {AgentRunStatus.QUEUED.value, AgentRunStatus.RUNNING.value}
)
AGENT_TOOL_TERMINAL_STATUSES = frozenset(
    {
        AgentToolCallStatus.SUCCEEDED.value,
        AgentToolCallStatus.FAILED.value,
        AgentToolCallStatus.CANCELLED.value,
        AgentToolCallStatus.TIMED_OUT.value,
    }
)

AGENT_RUN_TRANSITIONS: Mapping[str, frozenset[str]] = immutable_mapping(
    {
        AgentRunStatus.QUEUED.value: frozenset(
            {
                AgentRunStatus.RUNNING.value,
                AgentRunStatus.FAILED.value,
                AgentRunStatus.CANCELLED.value,
            }
        ),
        AgentRunStatus.RUNNING.value: frozenset(
            {
                AgentRunStatus.SUCCEEDED.value,
                AgentRunStatus.PARTIAL.value,
                AgentRunStatus.FAILED.value,
                AgentRunStatus.CANCELLED.value,
            }
        ),
        AgentRunStatus.SUCCEEDED.value: frozenset(),
        AgentRunStatus.PARTIAL.value: frozenset(),
        AgentRunStatus.FAILED.value: frozenset(),
        AgentRunStatus.CANCELLED.value: frozenset(),
    }
)
AGENT_TOOL_TRANSITIONS: Mapping[str, frozenset[str]] = immutable_mapping(
    {
        AgentToolCallStatus.QUEUED.value: frozenset(
            {
                AgentToolCallStatus.RUNNING.value,
                AgentToolCallStatus.FAILED.value,
                AgentToolCallStatus.CANCELLED.value,
                AgentToolCallStatus.TIMED_OUT.value,
            }
        ),
        AgentToolCallStatus.RUNNING.value: frozenset(
            {
                AgentToolCallStatus.SUCCEEDED.value,
                AgentToolCallStatus.FAILED.value,
                AgentToolCallStatus.CANCELLED.value,
                AgentToolCallStatus.TIMED_OUT.value,
            }
        ),
        AgentToolCallStatus.SUCCEEDED.value: frozenset(),
        AgentToolCallStatus.FAILED.value: frozenset(),
        AgentToolCallStatus.CANCELLED.value: frozenset(),
        AgentToolCallStatus.TIMED_OUT.value: frozenset(),
    }
)

EV_AGENT_RUN_QUEUED = "agent.run.queued"
EV_AGENT_RUN_STARTED = "agent.run.started"
EV_AGENT_OUTPUT_DELTA = "agent.output.delta"
EV_AGENT_TOOL_STARTED = "agent.tool.started"
EV_AGENT_TOOL_UPDATED = "agent.tool.updated"
EV_AGENT_TOOL_SUCCEEDED = "agent.tool.succeeded"
EV_AGENT_TOOL_FAILED = "agent.tool.failed"
EV_AGENT_RUN_SUCCEEDED = "agent.run.succeeded"
EV_AGENT_RUN_PARTIAL = "agent.run.partial"
EV_AGENT_RUN_FAILED = "agent.run.failed"
EV_AGENT_RUN_CANCELLED = "agent.run.cancelled"

AGENT_EVENT_NAMES = frozenset(
    {
        EV_AGENT_RUN_QUEUED,
        EV_AGENT_RUN_STARTED,
        EV_AGENT_OUTPUT_DELTA,
        EV_AGENT_TOOL_STARTED,
        EV_AGENT_TOOL_UPDATED,
        EV_AGENT_TOOL_SUCCEEDED,
        EV_AGENT_TOOL_FAILED,
        EV_AGENT_RUN_SUCCEEDED,
        EV_AGENT_RUN_PARTIAL,
        EV_AGENT_RUN_FAILED,
        EV_AGENT_RUN_CANCELLED,
    }
)


def agent_channel(agent_session_id: str) -> str:
    return f"agent:{agent_session_id}"


def agent_event_id(agent_run_id: str, execution_epoch: int, event_seq: int) -> str:
    if execution_epoch < 0:
        raise ValueError("execution_epoch must be nonnegative")
    if event_seq < 1:
        raise ValueError("event_seq must be positive")
    return f"agent:{agent_run_id}:{execution_epoch}:{event_seq}"


def require_agent_run_transition(current: str, target: str) -> None:
    allowed = AGENT_RUN_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise ValueError(f"invalid agent run transition: {current} -> {target}")


def require_agent_tool_transition(current: str, target: str) -> None:
    allowed = AGENT_TOOL_TRANSITIONS.get(current)
    if allowed is None or target not in allowed:
        raise ValueError(f"invalid agent tool transition: {current} -> {target}")


__all__ = [
    "AGENT_EVENT_NAMES",
    "AGENT_RUN_ACTIVE_STATUSES",
    "AGENT_RUN_TERMINAL_STATUSES",
    "AGENT_TOOL_CREATE_IMAGE",
    "AGENT_TOOL_TERMINAL_STATUSES",
    "AgentRunStatus",
    "AgentToolCallStatus",
    "EV_AGENT_OUTPUT_DELTA",
    "EV_AGENT_RUN_CANCELLED",
    "EV_AGENT_RUN_FAILED",
    "EV_AGENT_RUN_PARTIAL",
    "EV_AGENT_RUN_QUEUED",
    "EV_AGENT_RUN_STARTED",
    "EV_AGENT_RUN_SUCCEEDED",
    "EV_AGENT_TOOL_FAILED",
    "EV_AGENT_TOOL_STARTED",
    "EV_AGENT_TOOL_SUCCEEDED",
    "EV_AGENT_TOOL_UPDATED",
    "agent_channel",
    "agent_event_id",
    "require_agent_run_transition",
    "require_agent_tool_transition",
]
