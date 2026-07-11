"""Pure completion history and summary-boundary helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lumen_core.constants import DEFAULT_CHAT_INSTRUCTIONS, Role
from lumen_core.context_window import (
    IMAGE_INPUT_ESTIMATED_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    compose_summary_guardrail,
    count_tokens,
    is_summary_usable,
)
from lumen_core.models import Message


_STICKY_TEXT_CHAR_LIMIT = 16_000


@dataclass(frozen=True)
class _SummaryBoundary:
    conversation_id: str
    up_to_message_id: str
    up_to_created_at: datetime
    first_user_message_id: str | None
    recent_message_ids: list[str]
    summary_message_ids: list[str]
    source_message_count: int
    source_token_estimate: int


def _role_eq(role: Any, expected: Role) -> bool:
    return role == expected or role == expected.value


def _message_created_at(message: Message) -> datetime:
    value = message.created_at
    if isinstance(value, datetime):
        return value
    return datetime.min.replace(tzinfo=timezone.utc)


def _summary_created_at(summary: dict[str, Any] | None) -> datetime | None:
    if not isinstance(summary, dict):
        return None
    raw = summary.get("up_to_created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary_compressed_at(summary: dict[str, Any] | None) -> datetime | None:
    if not isinstance(summary, dict):
        return None
    raw = summary.get("compressed_at") or summary.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary_covers_boundary(
    summary: dict[str, Any] | None,
    boundary_message: Message | None,
) -> bool:
    if boundary_message is None or not is_summary_usable(summary):
        return False
    assert summary is not None
    summary_id = summary.get("up_to_message_id")
    summary_id = summary_id if isinstance(summary_id, str) and summary_id else None
    if summary_id == boundary_message.id:
        return True
    summary_dt = _summary_created_at(summary)
    boundary_dt = _message_created_at(boundary_message)
    if summary_dt is None:
        return False
    if summary_dt.tzinfo is None and boundary_dt.tzinfo is not None:
        summary_dt = summary_dt.replace(tzinfo=boundary_dt.tzinfo)
    if boundary_dt.tzinfo is None and summary_dt.tzinfo is not None:
        boundary_dt = boundary_dt.replace(tzinfo=summary_dt.tzinfo)
    if summary_dt > boundary_dt:
        return True
    if summary_dt < boundary_dt:
        return False
    return bool(summary_id and summary_id >= boundary_message.id)


def _message_after_summary(summary: dict[str, Any] | None, message: Message) -> bool:
    return not _summary_covers_boundary(summary, message)


def _summary_age_seconds(summary: dict[str, Any] | None) -> int | None:
    compressed_at = _summary_compressed_at(summary)
    if compressed_at is None:
        return None
    now = datetime.now(compressed_at.tzinfo or timezone.utc)
    return max(0, int((now - compressed_at).total_seconds()))


def _truncate_sticky_text(text: str) -> str:
    if len(text) <= _STICKY_TEXT_CHAR_LIMIT:
        return text
    return text[:_STICKY_TEXT_CHAR_LIMIT] + "\n[... truncated original task ...]"


def _sticky_text_from_message(message: Message) -> str:
    content = message.content or {}
    text = _truncate_sticky_text(content.get("text") or "")
    refs: list[str] = []
    for attachment in content.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        image_id = attachment.get("image_id")
        if image_id:
            refs.append(f"[user_image image_id={image_id}]")
        elif attachment.get("kind"):
            refs.append(f"[attachment kind={attachment.get('kind')!r}]")
    if refs:
        return "\n".join([text, *refs]).strip()
    return text


def _count_message_tokens_with_counter(
    role: str,
    content: dict[str, Any] | None,
    *,
    token_counter: Callable[[str], int],
) -> int:
    """Count a message with tiktoken, including image-input estimates."""
    content = content or {}
    text = content.get("text") or ""

    if role == Role.USER.value:
        attachments = content.get("attachments") or []
        image_count = sum(
            1
            for attachment in attachments
            if isinstance(attachment, dict) and attachment.get("image_id")
        )
        if not text and image_count == 0:
            return 0
        return (
            MESSAGE_OVERHEAD_TOKENS
            + token_counter(text)
            + image_count * IMAGE_INPUT_ESTIMATED_TOKENS
        )
    if role in (Role.ASSISTANT.value, Role.SYSTEM.value):
        if not text:
            return 0
        return MESSAGE_OVERHEAD_TOKENS + token_counter(text)
    return 0


def _count_message_tokens(role: str, content: dict[str, Any] | None) -> int:
    return _count_message_tokens_with_counter(
        role,
        content,
        token_counter=count_tokens,
    )


def _with_summary_guardrail(
    system_prompt: str | None,
    *,
    enabled: bool,
) -> str | None:
    if not enabled:
        return system_prompt
    guardrail = compose_summary_guardrail()
    if system_prompt:
        if guardrail in system_prompt:
            return system_prompt
        return f"{system_prompt.rstrip()}\n\n{guardrail}"
    return None


def _instructions_with_summary_guardrail(
    system_prompt: str | None,
    *,
    enabled: bool,
) -> str:
    base = system_prompt or DEFAULT_CHAT_INSTRUCTIONS
    if not enabled:
        return base
    guardrail = compose_summary_guardrail()
    if guardrail in base:
        return base
    return f"{base.rstrip()}\n\n{guardrail}"
