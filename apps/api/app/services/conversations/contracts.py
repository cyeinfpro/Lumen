"""Public response/input contracts for conversation APIs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from lumen_core.schemas import (
    CompletionOut,
    ConversationOut,
    GenerationOut,
    ImageOut,
    MessageOut,
)


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None = None


class MessageListOut(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None = None
    # Optional task data used to restore a conversation after a refresh.
    generations: list[GenerationOut] | None = None
    completions: list[CompletionOut] | None = None
    images: list[ImageOut] | None = None


class ConversationContextOut(BaseModel):
    input_budget_tokens: int
    total_target_tokens: int
    response_reserve_tokens: int
    estimated_input_tokens: int
    estimated_history_tokens: int
    estimated_system_tokens: int
    included_messages_count: int
    truncated: bool
    percent: float
    compression_enabled: bool
    summary_available: bool
    summary_tokens: int
    summary_up_to_message_id: str | None
    summary_updated_at: datetime | None
    summary_first_user_message_id: str | None
    summary_compression_runs: int
    compressible_messages_count: int
    compressible_tokens: int
    estimated_tokens_freed: int
    summary_target_tokens: int
    compressed: bool
    last_fallback_reason: str | None
    manual_compact_available: bool
    manual_compact_reset_seconds: int
    manual_compact_min_input_tokens: int
    manual_compact_cooldown_seconds: int
    manual_compact_unavailable_reason: str | None = None


class ConversationCompactIn(BaseModel):
    force: bool = False
    extra_instruction: str | None = None
    target_tokens: int | None = None
    dry_run: bool = False


class ConversationCompactOut(BaseModel):
    ok: bool
    summary_tokens: int | None = None
    source_message_count: int | None = None
    source_token_estimate: int | None = None
    summary_up_to_message_id: str | None = None
    model: str | None = None
    cached: bool = False
    elapsed_ms: int | None = None
    fallback_reason: str | None = None
    image_caption_count: int = 0
    rate_limit_remaining: int | None = None
    rate_limit_reset_seconds: int | None = None
    would_compress: bool | None = None
    estimated_source_messages: int | None = None
    estimated_source_tokens: int | None = None
    estimated_output_tokens_max: int | None = None


class ManualCompactIn(BaseModel):
    extra_instruction: str | None = None
    # force=False keeps the inexpensive budget short-circuit for normal calls.
    force: bool = False
    # Reserve room for the next user input and reasoning tokens.
    safety_margin: int | None = None
    # Background compaction is queued to ARQ and polled through the status API.
    background: bool = False


__all__ = [
    "ConversationCompactIn",
    "ConversationCompactOut",
    "ConversationContextOut",
    "ConversationListOut",
    "ManualCompactIn",
    "MessageListOut",
]
