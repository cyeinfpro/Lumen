"""Typed mutable state for one Agent Worker execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any, Literal

from ...agent_runtime_client import AgentRuntimeEvent


AGENT_NO_COST_HTTP_STATUSES = frozenset(
    {400, 401, 403, 404, 405, 409, 413, 415, 422, 429}
)
AGENT_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "reasoning_tokens",
    "total_tokens",
)


@dataclass(frozen=True, slots=True)
class AgentClaim:
    action: Literal["execute", "terminal", "result_unknown", "missing"]
    run_id: str
    execution_epoch: int = 0


@dataclass(slots=True)
class AgentRuntimeAccumulator:
    started_monotonic: float = field(default_factory=time.monotonic)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    text: str = ""
    pending_delta: str = ""
    last_flush_at: float = 0.0
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cache_write_1h_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
    )
    turn_count: int = 0
    runtime_tool_call_count: int = 0
    provider_dispatch_count: int = 0
    provider_completed_count: int = 0
    provider_response_statuses: list[int] = field(default_factory=list)
    terminal_status: str | None = None
    terminal_error_code: str | None = None
    limit_reason: str | None = None
    tool_started_at: dict[str, float] = field(default_factory=dict)
    pi_compaction_count: int = 0

    def apply(self, event: AgentRuntimeEvent) -> None:
        if event.type == "text.delta" and event.delta:
            self.text += event.delta
            self.pending_delta += event.delta
            return
        if event.type == "provider.dispatched":
            self.provider_dispatch_count += 1
            return
        if event.type == "provider.response":
            raw_status = event.status
            if isinstance(raw_status, int) and not isinstance(raw_status, bool):
                self.provider_response_statuses.append(raw_status)
            return
        if event.type == "turn.completed":
            self._apply_turn(event)
            return
        if event.type == "compaction.completed":
            self.pi_compaction_count += 1
            self.provider_completed_count += event.provider_call_count or 0
            self._add_usage(event.usage.model_dump() if event.usage else {})
            return
        if event.type == "tool.started" and event.name == "lumen_create_image":
            self.runtime_tool_call_count += 1
            if event.tool_call_id:
                self.tool_started_at[event.tool_call_id] = time.monotonic()
            return
        if event.type == "limit.reached":
            self.limit_reason = event.reason
            return
        if event.type in {"run.completed", "run.failed", "run.cancelled"}:
            self._apply_terminal(event)

    def consume_tool_duration(self, event: AgentRuntimeEvent) -> float | None:
        if not event.tool_call_id:
            return None
        started = self.tool_started_at.pop(event.tool_call_id, None)
        if started is None:
            return None
        return max(0.0, time.monotonic() - started)

    def _apply_turn(self, event: AgentRuntimeEvent) -> None:
        if event.turn is not None:
            self.turn_count = max(self.turn_count, event.turn)
        if (
            event.usage is not None
            and event.usage.total_tokens > 0
            and event.stop_reason not in {"error", "aborted"}
        ):
            self.provider_completed_count += 1
        self._add_usage(event.usage.model_dump() if event.usage else {})

    def _apply_terminal(self, event: AgentRuntimeEvent) -> None:
        self.terminal_status = str(event.status) if event.status is not None else None
        self.terminal_error_code = event.error_code
        if event.turn_count is not None:
            self.turn_count = max(self.turn_count, event.turn_count)
        if event.tool_call_count is not None:
            self.runtime_tool_call_count = max(
                self.runtime_tool_call_count,
                event.tool_call_count,
            )
        if event.provider_dispatch_count is not None:
            self.provider_dispatch_count = max(
                self.provider_dispatch_count,
                event.provider_dispatch_count,
            )
        if event.provider_completed_count is not None:
            self.provider_completed_count = max(
                self.provider_completed_count,
                event.provider_completed_count,
            )
        if event.usage is not None:
            terminal_usage = event.usage.model_dump()
            for key in AGENT_USAGE_KEYS:
                self.usage[key] = max(
                    self.usage.get(key, 0),
                    max(0, int(terminal_usage.get(key) or 0)),
                )

    def _add_usage(self, value: dict[str, Any]) -> None:
        for key in AGENT_USAGE_KEYS:
            if key == "total_tokens":
                continue
            raw = value.get(key)
            if isinstance(raw, int) and not isinstance(raw, bool):
                self.usage[key] += max(0, raw)
        self.usage["total_tokens"] = sum(
            self.usage.get(key, 0)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        )

    @property
    def has_exact_usage(self) -> bool:
        return any(self.usage.get(key, 0) > 0 for key in self.usage)

    @property
    def response_proves_no_cost(self) -> bool:
        return (
            self.provider_dispatch_count > 0
            and len(self.provider_response_statuses) >= self.provider_dispatch_count
            and all(
                status in AGENT_NO_COST_HTTP_STATUSES
                for status in self.provider_response_statuses
            )
        )

    @property
    def has_unresolved_dispatch(self) -> bool:
        pending = self.provider_dispatch_count - self.provider_completed_count
        if pending <= 0:
            return False
        pending_statuses = self.provider_response_statuses[-pending:]
        return len(pending_statuses) < pending or any(
            status not in AGENT_NO_COST_HTTP_STATUSES for status in pending_statuses
        )


__all__ = [
    "AGENT_NO_COST_HTTP_STATUSES",
    "AGENT_USAGE_KEYS",
    "AgentClaim",
    "AgentRuntimeAccumulator",
]
