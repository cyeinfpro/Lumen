"""Canonical Agent history payloads and conservative token estimates."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .context_window import estimate_text_tokens


AGENT_HISTORY_TEXT_LIMIT = 20_000
AGENT_PI_COMPACTION_MAX_RESERVE_TOKENS = 16_384
AGENT_PI_COMPACTION_MAX_KEEP_RECENT_TOKENS = 20_000
AGENT_PI_COMPACTION_MIN_TOKENS = 1_024
AGENT_PI_COMPACTION_SUMMARY_RATIO_X1000 = 800
AGENT_PI_COMPACTION_BOUNDARY_SLACK_TOKENS = 8_192
AGENT_PI_CONTEXT_OVERHEAD_TOKENS = 2_048


@dataclass(frozen=True, slots=True)
class AgentContextPlan:
    mode: Literal["direct", "compact_before_prompt", "impossible"]
    estimated_input_tokens: int
    direct_input_limit: int
    compaction_source_limit: int
    estimated_post_compaction_tokens: int


def plan_agent_runtime_context(
    *,
    context_window: int,
    max_output_tokens: int,
    fixed_input_tokens: int,
    history_tokens: int,
    largest_history_entry_tokens: int = 0,
) -> AgentContextPlan:
    """Mirror the Runtime's Pi compaction budgets without duplicating its loop."""

    window = max(1, int(context_window))
    output_reserve = max(1, int(max_output_tokens))
    fixed = max(0, int(fixed_input_tokens))
    history = max(0, int(history_tokens))
    largest_entry = max(0, int(largest_history_entry_tokens))
    compaction_reserve = min(
        AGENT_PI_COMPACTION_MAX_RESERVE_TOKENS,
        max(AGENT_PI_COMPACTION_MIN_TOKENS, window // 4),
    )
    keep_recent = min(
        AGENT_PI_COMPACTION_MAX_KEEP_RECENT_TOKENS,
        max(
            AGENT_PI_COMPACTION_MIN_TOKENS,
            (window - compaction_reserve) // 2,
        ),
    )
    execution_reserve = max(output_reserve, compaction_reserve)
    direct_limit = max(0, window - execution_reserve)
    source_limit = max(0, window - compaction_reserve)
    estimated_input = fixed + history
    summary_tokens = (
        compaction_reserve * AGENT_PI_COMPACTION_SUMMARY_RATIO_X1000 // 1_000
    )
    retained_history = min(
        history,
        keep_recent
        + max(AGENT_PI_COMPACTION_BOUNDARY_SLACK_TOKENS, largest_entry),
    )
    post_compaction = fixed + retained_history + summary_tokens
    if estimated_input <= direct_limit:
        mode: Literal["direct", "compact_before_prompt", "impossible"] = "direct"
    elif (
        history + AGENT_PI_CONTEXT_OVERHEAD_TOKENS <= source_limit
        and post_compaction <= direct_limit
    ):
        mode = "compact_before_prompt"
    else:
        mode = "impossible"
    return AgentContextPlan(
        mode=mode,
        estimated_input_tokens=estimated_input,
        direct_input_limit=direct_limit,
        compaction_source_limit=source_limit,
        estimated_post_compaction_tokens=post_compaction,
    )


def agent_tool_history_result_text(
    *,
    status: str,
    mode: str | None,
    generation_ids: Iterable[object],
    error_code: str | None,
) -> str:
    safe_generation_ids = [value for value in generation_ids if isinstance(value, str)][
        :4
    ]
    return json.dumps(
        {
            "status": status,
            "mode": mode,
            "generation_ids": safe_generation_ids,
            "error_code": error_code,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )[:AGENT_HISTORY_TEXT_LIMIT]


def estimate_agent_runtime_history_tokens(
    *,
    text: str,
    final_text: str | None = None,
    tool_arguments: Iterable[Mapping[str, Any]] = (),
    tool_result_texts: Iterable[str] = (),
    image_tokens: Iterable[int] = (),
) -> int:
    total = estimate_text_tokens(text)
    total += estimate_text_tokens(final_text or "")
    for arguments in tool_arguments:
        serialized = json.dumps(
            dict(arguments),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        total += estimate_text_tokens(serialized)
    total += sum(estimate_text_tokens(value) for value in tool_result_texts)
    total += sum(max(0, int(value)) for value in image_tokens)
    return total


__all__ = [
    "AGENT_HISTORY_TEXT_LIMIT",
    "AGENT_PI_CONTEXT_OVERHEAD_TOKENS",
    "AgentContextPlan",
    "agent_tool_history_result_text",
    "estimate_agent_runtime_history_tokens",
    "plan_agent_runtime_context",
]
