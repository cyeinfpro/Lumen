"""Pure completion context-packing value objects and constructors."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from lumen_core.context_window import (
    MESSAGE_OVERHEAD_TOKENS,
    count_tokens,
    estimate_summary_tokens,
    estimate_system_prompt_tokens,
    format_sticky_input_text,
)
from lumen_core.models import Message

from .history import (
    _count_message_tokens,
    _message_after_summary,
    _sticky_text_from_message,
    _summary_age_seconds,
    _with_summary_guardrail,
)


@dataclass(frozen=True)
class PackedContext:
    input_list: list[dict[str, Any]]
    estimated_tokens: int
    summary_used: bool
    summary_created: bool
    summary_up_to_message_id: str | None
    sticky_used: bool
    included_messages_count: int
    truncated_without_summary: bool
    fallback_reason: str | None
    compression_enabled: bool = False
    recent_messages_count: int = 0
    summary_tokens: int = 0
    summary_age_seconds: int | None = None
    compressor_model: str | None = None
    image_caption_count: int = 0
    quality_probes: dict[str, Any] | None = None
    _system_prompt: str | None = None
    _sticky_message: Message | None = None
    _summary_text: str | None = None
    _recent_rows: tuple[Message, ...] = ()


def _make_quality_probes(packed: PackedContext) -> dict[str, Any]:
    return {
        "summary_used": packed.summary_used,
        "summary_age_seconds": packed.summary_age_seconds,
        "summary_tokens": packed.summary_tokens,
        "recent_messages_count": packed.recent_messages_count,
        "first_user_message_pinned": packed.sticky_used,
        "user_repeated_facts_score": None,
        "model_signaled_missing_context": False,
    }


def _packed_with_input(
    input_list: list[dict[str, Any]],
    packed: PackedContext,
) -> PackedContext:
    return replace(
        packed,
        input_list=input_list,
        quality_probes=packed.quality_probes or _make_quality_probes(packed),
    )


def _estimated_summary_source(
    rows: list[Message],
    *,
    skip_message_id: str | None,
) -> int:
    """Estimate summary-source tokens through JSON serialization."""
    total = 0
    for message in rows:
        if message.id == skip_message_id:
            continue
        content_json = json.dumps(message.content or {}, ensure_ascii=False)
        total += MESSAGE_OVERHEAD_TOKENS + count_tokens(content_json)
    return total


def _pack_with_existing_summary(
    *,
    system_prompt: str | None,
    all_rows_desc: list[Message],
    summary: dict[str, Any],
    summary_model: str,
    input_budget: int,
    current_user: Message | None,
    first_user: Message | None,
) -> PackedContext:
    summary_token_count = estimate_summary_tokens(summary)
    sticky_message = (
        first_user
        if first_user is not None and not _message_after_summary(summary, first_user)
        else None
    )
    sticky_tokens = 0
    if sticky_message is not None:
        sticky_input_text = format_sticky_input_text(
            _sticky_text_from_message(sticky_message)
        )
        sticky_tokens = MESSAGE_OVERHEAD_TOKENS + count_tokens(sticky_input_text)

    used_tokens = (
        estimate_system_prompt_tokens(
            _with_summary_guardrail(system_prompt, enabled=True)
        )
        + sticky_tokens
        + MESSAGE_OVERHEAD_TOKENS
        + summary_token_count
    )
    after_summary_desc = [
        message
        for message in all_rows_desc
        if _count_message_tokens(message.role, message.content) > 0
        and _message_after_summary(summary, message)
    ]
    recent_desc: list[Message] = []
    recent_ids: set[str] = set()

    if current_user is not None and _message_after_summary(summary, current_user):
        current_tokens = _count_message_tokens(current_user.role, current_user.content)
        if current_tokens > 0:
            recent_desc.append(current_user)
            recent_ids.add(current_user.id)
            used_tokens += current_tokens

    for message in after_summary_desc:
        if message.id in recent_ids:
            continue
        estimated = _count_message_tokens(message.role, message.content)
        if used_tokens + estimated > input_budget:
            break
        recent_desc.append(message)
        recent_ids.add(message.id)
        used_tokens += estimated

    summary_text = str(summary.get("text") or "")
    recent_rows = tuple(reversed(recent_desc))
    return PackedContext(
        input_list=[],
        estimated_tokens=used_tokens,
        summary_used=True,
        summary_created=False,
        summary_up_to_message_id=str(summary.get("up_to_message_id") or ""),
        sticky_used=sticky_message is not None,
        included_messages_count=len(recent_rows)
        + (1 if sticky_message is not None else 0),
        truncated_without_summary=False,
        fallback_reason=None,
        compression_enabled=True,
        recent_messages_count=len(recent_rows),
        summary_tokens=summary_token_count,
        summary_age_seconds=_summary_age_seconds(summary),
        compressor_model=summary_model,
        image_caption_count=int(summary.get("image_caption_count") or 0),
        _system_prompt=system_prompt,
        _sticky_message=sticky_message,
        _summary_text=summary_text,
        _recent_rows=recent_rows,
    )


def _fallback_pack(
    *,
    system_prompt: str | None,
    rows_desc: list[Message],
    used_tokens: int,
    truncated: bool,
    compression_enabled: bool = False,
    fallback_reason: str | None = None,
    force_include_message: Message | None = None,
    compressor_model: str | None = None,
) -> PackedContext:
    selected_desc = list(rows_desc)
    if force_include_message is not None and all(
        message.id != force_include_message.id for message in selected_desc
    ):
        selected_desc.insert(0, force_include_message)
        used_tokens += _count_message_tokens(
            force_include_message.role,
            force_include_message.content,
        )
    selected = tuple(reversed(selected_desc))
    return PackedContext(
        input_list=[],
        estimated_tokens=used_tokens,
        summary_used=False,
        summary_created=False,
        summary_up_to_message_id=None,
        sticky_used=False,
        included_messages_count=len(selected),
        truncated_without_summary=truncated,
        fallback_reason=fallback_reason,
        compression_enabled=compression_enabled,
        recent_messages_count=len(selected),
        compressor_model=compressor_model,
        _system_prompt=system_prompt,
        _recent_rows=selected,
    )
