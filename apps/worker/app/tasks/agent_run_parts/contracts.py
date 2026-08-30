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
    text_reset_pending: bool = False
    force_next_delta: bool = False
    blocks_dirty: bool = False
    blocks: list[dict[str, Any]] = field(default_factory=list)
    output_revision: int = 0
    output_runtime_seq: int = 0
    provider_dispatch_ordinals: set[int] = field(default_factory=set)
    provider_completed_ordinals: set[int] = field(default_factory=set)
    exact_usage_ordinals: set[int] = field(default_factory=set)
    no_charge_ordinals: set[int] = field(default_factory=set)
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
        if event.type == "text.reset":
            self._apply_text_reset(event)
            return
        if event.type == "text.delta":
            self._apply_text_delta(event)
            return
        if event.type == "provider.dispatched":
            self.provider_dispatch_count += 1
            ordinal = event.dispatch_ordinal or self.provider_dispatch_count
            self.provider_dispatch_ordinals.add(ordinal)
            return
        if event.type == "provider.response":
            self._apply_provider_response(event)
            return
        if event.type == "turn.completed":
            self._apply_turn(event)
            return
        if event.type == "compaction.completed":
            self._apply_compaction(event)
            return
        if event.type == "tool.started":
            self._apply_tool_started(event)
            return
        if event.type in {"tool.succeeded", "tool.failed"}:
            self.blocks_dirty = True
            self.output_runtime_seq = max(self.output_runtime_seq, event.seq)
            self._upsert_tool_block(
                event,
                "succeeded" if event.type == "tool.succeeded" else "failed",
            )
            return
        if event.type == "limit.reached":
            self.limit_reason = event.reason
            return
        if event.type in {"run.completed", "run.failed", "run.cancelled"}:
            self._apply_terminal(event)

    def _apply_text_reset(self, event: AgentRuntimeEvent) -> None:
        reset_turn = event.turn
        if reset_turn is None:
            self.blocks.clear()
        else:
            self.blocks = [
                block
                for block in self.blocks
                if int(block.get("turn") or 0) < reset_turn
            ]
        replacement = event.replacement_text
        if isinstance(replacement, str) and replacement:
            self.blocks.append(
                {
                    "kind": "text",
                    "turn": reset_turn or 1,
                    "text": replacement,
                }
            )
        self.text = self._render_text()
        self.pending_delta = ""
        self.text_reset_pending = True
        self.force_next_delta = True
        self.output_revision = max(
            self.output_revision + 1,
            int(event.output_revision or 0),
        )
        self.output_runtime_seq = max(self.output_runtime_seq, event.seq)

    def _apply_text_delta(self, event: AgentRuntimeEvent) -> None:
        if not event.delta:
            return
        turn = event.turn or (self.turn_count + 1)
        self._append_text(turn, event.delta)
        self.pending_delta += event.delta
        self.output_runtime_seq = max(self.output_runtime_seq, event.seq)

    def _apply_provider_response(self, event: AgentRuntimeEvent) -> None:
        raw_status = event.status
        ordinal = event.dispatch_ordinal or len(self.provider_response_statuses) + 1
        if isinstance(raw_status, int) and not isinstance(raw_status, bool):
            self.provider_response_statuses.append(raw_status)
            if event.no_charge_receipt is True:
                self.no_charge_ordinals.add(ordinal)

    def _apply_compaction(self, event: AgentRuntimeEvent) -> None:
        self.pi_compaction_count += 1
        call_count = event.provider_call_count or 0
        covered = sorted(
            self.provider_dispatch_ordinals
            - self.provider_completed_ordinals
            - self.no_charge_ordinals
        )[:call_count]
        self.provider_completed_ordinals.update(covered)
        if event.usage_evidence == "exact":
            self.exact_usage_ordinals.update(covered)
        self.provider_completed_count += call_count
        self._add_usage(event.usage.model_dump() if event.usage else {})

    def _apply_tool_started(self, event: AgentRuntimeEvent) -> None:
        self.blocks_dirty = True
        self.output_runtime_seq = max(self.output_runtime_seq, event.seq)
        if event.name == "lumen_create_image":
            self.runtime_tool_call_count += 1
        self._upsert_tool_block(event, "running")
        if event.tool_call_id:
            self.tool_started_at[event.tool_call_id] = time.monotonic()

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
        ordinal = event.dispatch_ordinal
        if ordinal is not None and event.stop_reason not in {"error", "aborted"}:
            self.provider_completed_ordinals.add(ordinal)
            if event.usage_evidence == "exact":
                self.exact_usage_ordinals.add(ordinal)
        if event.usage is not None and event.stop_reason not in {"error", "aborted"}:
            self.provider_completed_count += 1
        self._add_usage(event.usage.model_dump() if event.usage else {})

    def _apply_terminal(self, event: AgentRuntimeEvent) -> None:
        self.terminal_status = str(event.status) if event.status is not None else None
        self.terminal_error_code = event.error_code
        if event.usage_evidence == "exact":
            self.provider_completed_ordinals.update(self.provider_dispatch_ordinals)
            self.exact_usage_ordinals.update(self.provider_dispatch_ordinals)
        if event.no_charge_receipt is True:
            self.no_charge_ordinals.update(self.provider_dispatch_ordinals)
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

    def _append_text(self, turn: int, value: str) -> None:
        if (
            self.blocks
            and self.blocks[-1].get("kind") == "text"
            and self.blocks[-1].get("turn") == turn
        ):
            self.blocks[-1]["text"] = str(self.blocks[-1].get("text") or "") + value
        else:
            self.blocks.append({"kind": "text", "turn": turn, "text": value})
        self.text = self._render_text()

    def _upsert_tool_block(self, event: AgentRuntimeEvent, status: str) -> None:
        if not event.tool_call_id:
            return
        for block in self.blocks:
            if (
                block.get("kind") == "tool"
                and block.get("tool_call_id") == event.tool_call_id
            ):
                block["status"] = status
                if event.generation_ids:
                    block["generation_ids"] = list(event.generation_ids)
                return
        self.blocks.append(
            {
                "kind": "tool",
                "turn": event.turn or (self.turn_count + 1),
                "tool_call_id": event.tool_call_id,
                "ordinal": event.ordinal,
                "name": event.name,
                "status": status,
                "generation_ids": list(event.generation_ids or []),
            }
        )

    def _render_text(self) -> str:
        return "\n\n".join(
            str(block.get("text") or "")
            for block in self.blocks
            if block.get("kind") == "text" and str(block.get("text") or "")
        )

    @property
    def has_exact_usage(self) -> bool:
        return bool(self.exact_usage_ordinals)

    @property
    def response_proves_no_cost(self) -> bool:
        return bool(
            self.provider_dispatch_ordinals
            and self.provider_dispatch_ordinals.issubset(self.no_charge_ordinals)
        )

    @property
    def has_unresolved_dispatch(self) -> bool:
        if self.provider_dispatch_ordinals:
            return bool(
                self.provider_dispatch_ordinals
                - self.exact_usage_ordinals
                - self.no_charge_ordinals
            )
        pending = self.provider_dispatch_count - self.provider_completed_count
        return pending > 0


__all__ = [
    "AGENT_NO_COST_HTTP_STATUSES",
    "AGENT_USAGE_KEYS",
    "AgentClaim",
    "AgentRuntimeAccumulator",
]
