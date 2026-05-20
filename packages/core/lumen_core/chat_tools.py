"""Shared chat tool status contract.

Responses-compatible providers use slightly different status strings for built-in
tools. Keep the mapping in core so API, worker, and web-facing payloads do not
grow separate state machines.
"""

from __future__ import annotations

from enum import StrEnum
import math
from typing import Any


class ToolStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


UPSTREAM_STATUS_MAP: dict[str, ToolStatus] = {
    "queued": ToolStatus.QUEUED,
    "pending": ToolStatus.QUEUED,
    "created": ToolStatus.QUEUED,
    "in_progress": ToolStatus.RUNNING,
    "running": ToolStatus.RUNNING,
    "searching": ToolStatus.RUNNING,
    "interpreting": ToolStatus.RUNNING,
    "generating": ToolStatus.RUNNING,
    "requires_action": ToolStatus.RUNNING,
    "interrupted": ToolStatus.FAILED,
    "completed": ToolStatus.SUCCEEDED,
    "complete": ToolStatus.SUCCEEDED,
    "succeeded": ToolStatus.SUCCEEDED,
    "done": ToolStatus.SUCCEEDED,
    "failed": ToolStatus.FAILED,
    "incomplete": ToolStatus.FAILED,
    "error": ToolStatus.FAILED,
    "errored": ToolStatus.FAILED,
    "cancelled": ToolStatus.CANCELLED,
    "canceled": ToolStatus.CANCELLED,
    "timeout": ToolStatus.TIMED_OUT,
    "timed_out": ToolStatus.TIMED_OUT,
    "expired": ToolStatus.TIMED_OUT,
}

TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
        ToolStatus.CANCELLED,
        ToolStatus.TIMED_OUT,
    }
)

# Backwards-compatible string set for worker code that persists statuses as
# JSON strings in message content.
TOOL_TERMINAL_STATUSES = frozenset(status.value for status in TERMINAL_TOOL_STATUSES)

_EVENT_SUFFIX_STATUS_MAP: tuple[tuple[tuple[str, ...], ToolStatus], ...] = (
    (
        (".failed", ".incomplete", ".error", ".errored"),
        ToolStatus.FAILED,
    ),
    (
        (".interrupted",),
        ToolStatus.FAILED,
    ),
    (
        (".cancelled", ".canceled"),
        ToolStatus.CANCELLED,
    ),
    (
        (".timed_out", ".timeout", ".expired"),
        ToolStatus.TIMED_OUT,
    ),
    (
        (".completed", ".complete", ".done"),
        ToolStatus.SUCCEEDED,
    ),
    (
        (
            ".in_progress",
            ".running",
            ".searching",
            ".interpreting",
            ".generating",
            ".requires_action",
            ".delta",
        ),
        ToolStatus.RUNNING,
    ),
    (
        (".created", ".queued", ".pending"),
        ToolStatus.QUEUED,
    ),
)


def normalize_tool_status(
    raw: Any,
    *,
    event_type: str | None = None,
    default: ToolStatus | str | None = None,
) -> ToolStatus:
    """Normalize upstream status/event strings to the shared enum.

    Unknown explicit values use ``default`` when provided; otherwise they return
    ``ToolStatus.UNKNOWN`` instead of guessing.
    """
    if isinstance(raw, ToolStatus):
        return raw

    value = str(raw).strip().lower() if raw is not None else ""
    if value:
        status = UPSTREAM_STATUS_MAP.get(value)
        if status is not None:
            return status
        if default is not None:
            return normalize_tool_status(default)
        return ToolStatus.UNKNOWN

    event_value = (event_type or "").strip().lower()
    if event_value == "response.output_item.added":
        return ToolStatus.RUNNING
    if event_value == "response.output_item.done":
        return ToolStatus.SUCCEEDED
    if event_value == "response.requires_action":
        return ToolStatus.RUNNING
    for suffixes, status in _EVENT_SUFFIX_STATUS_MAP:
        if event_value.endswith(suffixes):
            return status

    if default is not None:
        return normalize_tool_status(default)
    return ToolStatus.UNKNOWN


def is_terminal_tool_status(status: ToolStatus | str | None) -> bool:
    return normalize_tool_status(status) in TERMINAL_TOOL_STATUSES


def normalize_tool_idle_timeout_seconds(
    raw: Any,
    *,
    default: float,
) -> float:
    """Return a non-negative idle timeout, preserving ``0`` as disabled.

    Runtime settings may come from DB/env/tests as strings, ints, or floats.  A
    missing or invalid value means "use the caller's default"; an explicit zero
    keeps idle timeout disabled for local/debug runs.
    """
    if raw in (None, ""):
        return float(default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < 0:
        return float(default)
    return value


def tool_status_idle_timed_out(
    last_update_ts: float | None,
    *,
    now: float,
    timeout_s: float,
) -> bool:
    """Return true when an active tool call has exceeded its idle timeout."""
    if last_update_ts is None or timeout_s <= 0:
        return False
    return (now - last_update_ts) >= timeout_s


def tool_status_idle_timeout_remaining_seconds(
    last_update_ts: float | None,
    *,
    now: float,
    timeout_s: float,
) -> float | None:
    """Return seconds remaining before a tool idle timeout, or ``None`` if off."""
    if last_update_ts is None or timeout_s <= 0:
        return None
    return max(0.0, timeout_s - (now - last_update_ts))


__all__ = [
    "TERMINAL_TOOL_STATUSES",
    "TOOL_TERMINAL_STATUSES",
    "ToolStatus",
    "UPSTREAM_STATUS_MAP",
    "is_terminal_tool_status",
    "normalize_tool_idle_timeout_seconds",
    "normalize_tool_status",
    "tool_status_idle_timed_out",
    "tool_status_idle_timeout_remaining_seconds",
]
