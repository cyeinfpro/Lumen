"""Completion execution state split by lifecycle stage."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from lumen_core.constants import DEFAULT_CHAT_INSTRUCTIONS, DEFAULT_CHAT_MODEL
from lumen_core.models import Message

from .runtime import CompletionPorts


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    redis: Any
    task_id: str
    lease_token: str
    task_start: float
    channel: str


@dataclass(slots=True)
class PreparationState:
    attempt: int = 0
    attempt_epoch: int = 0
    user_api_credential_id: str | None = None
    account_mode: str = "wallet"
    runtime_override: Any | None = None
    queue_metadata_payload: dict[str, Any] = field(default_factory=dict)
    was_restarted: bool = False
    user_id: str = ""
    message_id: str = ""
    system_prompt: str | None = None
    chat_model: str = DEFAULT_CHAT_MODEL
    conversation_id: str | None = None
    target_msg: Message | None = None
    reasoning_effort: str | None = None
    fast_mode: bool = False


@dataclass(slots=True)
class StreamingState:
    chat_tools: list[dict[str, Any]] = field(default_factory=list)
    input_list: list[dict[str, Any]] = field(default_factory=list)
    instructions: str = DEFAULT_CHAT_INSTRUCTIONS
    body: dict[str, Any] = field(default_factory=dict)
    max_tool_invocations: int = 8
    cancel_poll_interval_s: float = 0.1
    tool_idle_timeout_s: float = 30.0
    accumulated_text: str = ""
    accumulated_thinking: str = ""
    flushed_len: int = 0
    has_partial: bool = False
    tool_images: list[dict[str, Any]] = field(default_factory=list)
    stored_image_call_ids: set[str] = field(default_factory=set)
    reserved_tool_image_budget_micro: int = 0
    tool_loop_truncated: bool = False


@dataclass(slots=True)
class UsageState:
    tool_tracker: Any = None
    usage_totals: Any = None
    round_text_start: int = 0
    round_thinking_start: int = 0
    request_sent: bool = False
    dispatch_started_recorded: bool = False
    response_receipt_recorded: bool = False
    upstream_provider_event: dict[str, str] | None = None
    delta_counter: int = 0
    completed_response: dict[str, Any] | None = None
    memory_meta_for_event: dict[str, Any] = field(
        default_factory=lambda: {
            "used_memory_ids": [],
            "used_memory_summary": [],
        }
    )


@dataclass(slots=True)
class SettlementState:
    task_outcome: str = "unknown"
    lease_lost: asyncio.Event = field(default_factory=asyncio.Event)
    lease_acquired: bool = False
    renewer: asyncio.Task[None] | None = None
    cancel_requested: asyncio.Event | None = None
    cancel_stop_requested: asyncio.Event | None = None
    cancel_watcher: asyncio.Task[None] | None = None
    stream_span_cm: Any | None = None


@dataclass(slots=True)
class CompletionExecution:
    ports: CompletionPorts
    request: CompletionRequest
    preparation: PreparationState = field(default_factory=PreparationState)
    streaming: StreamingState = field(default_factory=StreamingState)
    usage: UsageState = field(default_factory=UsageState)
    settlement: SettlementState = field(default_factory=SettlementState)
