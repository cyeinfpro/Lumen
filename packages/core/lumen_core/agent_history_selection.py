"""Bounded newest-tail selection for complete Agent conversation units."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


T = TypeVar("T")
AGENT_HISTORY_MAX_ENTRIES = 2048
AGENT_HISTORY_SCAN_LIMIT = 8192


@dataclass(frozen=True, slots=True)
class AgentHistorySelection(Generic[T]):
    items: tuple[T, ...]
    truncated: bool
    first_retained_id: str | None
    removed_entries: int
    removed_tokens: int
    retained_tokens: int


def semantic_agent_message(
    *,
    role: str,
    content: Any,
    status: str | None = None,
) -> bool:
    if role not in {"user", "assistant"} or not isinstance(content, dict):
        return False
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        return True
    for key in ("attachments", "tool_calls", "generation_ids", "blocks"):
        value = content.get(key)
        if isinstance(value, list) and value:
            return True
    return status not in {"failed", "canceled", "cancelled"} and role == "user"


def select_agent_history_tail(
    items: Sequence[T],
    *,
    item_id: Callable[[T], str],
    role: Callable[[T], str],
    semantic: Callable[[T], bool],
    token_estimate: Callable[[T], int],
    max_entries: int = AGENT_HISTORY_MAX_ENTRIES,
    max_tokens: int | None = None,
) -> AgentHistorySelection[T]:
    """Keep newest complete user/assistant units and restore chronological order."""

    meaningful = [item for item in items if semantic(item)]
    units: list[list[T]] = []
    for item in meaningful:
        item_role = role(item)
        if item_role in {"user", "system"} or not units:
            units.append([item])
        else:
            units[-1].append(item)
    retained_reversed: list[list[T]] = []
    retained_count = 0
    retained_tokens = 0
    maximum_entries = max(1, int(max_entries))
    maximum_tokens = None if max_tokens is None else max(0, int(max_tokens))
    for unit in reversed(units):
        unit_count = len(unit)
        unit_tokens = sum(max(0, token_estimate(item)) for item in unit)
        if unit_count > maximum_entries:
            break
        if maximum_tokens is not None and unit_tokens > maximum_tokens:
            break
        if retained_reversed and retained_count + unit_count > maximum_entries:
            break
        if (
            retained_reversed
            and maximum_tokens is not None
            and retained_tokens + unit_tokens > maximum_tokens
        ):
            break
        retained_reversed.append(unit)
        retained_count += unit_count
        retained_tokens += unit_tokens
    retained_units = list(reversed(retained_reversed))
    selected = tuple(item for unit in retained_units for item in unit)
    selected_ids = {item_id(item) for item in selected}
    removed = [item for item in meaningful if item_id(item) not in selected_ids]
    removed_tokens = sum(max(0, token_estimate(item)) for item in removed)
    return AgentHistorySelection(
        items=selected,
        truncated=bool(removed),
        first_retained_id=item_id(selected[0]) if selected else None,
        removed_entries=len(removed),
        removed_tokens=removed_tokens,
        retained_tokens=retained_tokens,
    )


__all__ = [
    "AGENT_HISTORY_MAX_ENTRIES",
    "AGENT_HISTORY_SCAN_LIMIT",
    "AgentHistorySelection",
    "select_agent_history_tail",
    "semantic_agent_message",
]
