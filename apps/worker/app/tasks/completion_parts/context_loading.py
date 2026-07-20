"""Database-backed completion history loading and context compression."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import and_, desc, or_, select

from lumen_core.constants import Role
from lumen_core.context_window import (
    HISTORY_FETCH_BATCH,
    MESSAGE_OVERHEAD_TOKENS,
    format_sticky_input_text,
    format_summary_input_text,
    is_summary_usable,
)
from lumen_core.models import Conversation, Image, ImageVariant, Message

from .context import PackedContext
from .history import (
    _SummaryBoundary,
    _message_after_summary,
    _message_created_at,
    _role_eq,
    _instructions_with_summary_guardrail,
    _sticky_text_from_message,
)


@dataclass(frozen=True)
class ContextLoadingHooks:
    count_message_tokens: Callable[[str, dict[str, Any] | None], int]
    count_tokens: Callable[[str], int]
    estimate_system_prompt_tokens: Callable[[str | None], int]
    get_input_budget: Callable[[str | None], int]
    message_retention_filter_for_account: Callable[[str | None], Awaitable[Any]]
    resolve_summary_model: Callable[[], Awaitable[str]]
    resolve_int_setting: Callable[[str, int], Awaitable[int]]
    ensure_context_summary: Callable[..., Awaitable[dict[str, Any] | None]]
    build_input_from_packed_context: Callable[..., Awaitable[list[dict[str, Any]]]]
    load_rows_desc: Callable[..., Awaitable[tuple[list[Message], int, bool]]]
    load_rows_desc_after_summary: Callable[..., Awaitable[list[Message]]]
    pick_first_user_from_summary: Callable[..., Awaitable[Message | None]]
    pick_current_user_with_lookup: Callable[..., Awaitable[Message | None]]
    pick_first_user: Callable[[list[Message]], Message | None]
    pick_current_user: Callable[[list[Message], Message], Message | None]
    context_circuit_open: Callable[[Any | None], Awaitable[bool]]
    input_token_budget: int
    compression_enabled_default: int
    compression_trigger_percent_default: int
    summary_target_tokens_default: int
    summary_min_recent_messages_default: int
    summary_min_interval_seconds_default: int
    logger: logging.Logger


async def _attachment_to_data_url(
    session: Any,
    image_id: str,
    *,
    storage_get_bytes: Callable[[str], bytes],
    logger: logging.Logger,
) -> str | None:
    """Read a preview image and encode it as an inline Responses data URL."""
    image = await session.get(Image, image_id)
    if image is None or getattr(image, "deleted_at", None) is not None:
        return None

    preview = (
        await session.execute(
            select(ImageVariant).where(
                ImageVariant.image_id == image_id,
                ImageVariant.kind == "preview1024",
            )
        )
    ).scalar_one_or_none()

    if preview is not None:
        key = preview.storage_key
        mime = "image/webp"
    else:
        key = image.storage_key
        mime = image.mime or "image/png"

    try:
        raw = await asyncio.to_thread(storage_get_bytes, key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("attachment read failed image_id=%s err=%s", image_id, exc)
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def _message_to_input_item(
    session: Any,
    message: Message,
    *,
    attachment_to_data_url: Callable[..., Awaitable[str | None]],
) -> dict[str, Any] | None:
    content = message.content or {}
    text = content.get("text") or ""
    if _role_eq(message.role, Role.USER):
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "input_text", "text": text})
        for attachment in content.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            image_id = attachment.get("image_id")
            if not image_id:
                continue
            data_url = await attachment_to_data_url(session, image_id)
            if data_url:
                parts.append({"type": "input_image", "image_url": data_url})
        if parts:
            return {"role": "user", "content": parts}
    elif _role_eq(message.role, Role.ASSISTANT) and text:
        return {
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }
    return None


async def _build_input_from_packed_context(
    session: Any,
    packed: PackedContext,
    *,
    message_to_input_item: Callable[..., Awaitable[dict[str, Any] | None]],
) -> list[dict[str, Any]]:
    input_list: list[dict[str, Any]] = []
    if packed.sticky_used and packed._sticky_message is not None:
        sticky_text = _sticky_text_from_message(packed._sticky_message)
        if sticky_text:
            input_list.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": format_sticky_input_text(sticky_text),
                        }
                    ],
                }
            )

    if packed.summary_used and packed._summary_text:
        input_list.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": format_summary_input_text(packed._summary_text),
                    }
                ],
            }
        )

    for message in packed._recent_rows:
        item = await message_to_input_item(session, message)
        if item is not None:
            input_list.append(item)
    return input_list


def _reestimate_existing_summary_tokens(
    packed: PackedContext,
    *,
    system_prompt: str | None,
    hooks: ContextLoadingHooks,
) -> PackedContext:
    instructions = _instructions_with_summary_guardrail(system_prompt, enabled=True)
    used_tokens = hooks.estimate_system_prompt_tokens(instructions)
    if packed.sticky_used and packed._sticky_message is not None:
        sticky_text = format_sticky_input_text(
            _sticky_text_from_message(packed._sticky_message)
        )
        used_tokens += MESSAGE_OVERHEAD_TOKENS + hooks.count_tokens(sticky_text)
    if packed.summary_used:
        used_tokens += MESSAGE_OVERHEAD_TOKENS + packed.summary_tokens
    used_tokens += sum(
        hooks.count_message_tokens(message.role, message.content)
        for message in packed._recent_rows
    )
    return replace(packed, estimated_tokens=used_tokens)


async def _load_rows_desc(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    budget_tokens: int | None,
    system_prompt: str | None,
    retention_filter: Any | None = None,
    count_message_tokens: Callable[[str, dict[str, Any] | None], int],
    estimate_system_prompt_tokens: Callable[[str | None], int],
) -> tuple[list[Message], int, bool]:
    rows_desc: list[Message] = []
    used_tokens = estimate_system_prompt_tokens(system_prompt)
    cursor_created_at = target.created_at
    cursor_id = target.id
    cursor_inclusive = True
    truncated = False

    while True:
        same_timestamp_filter = (
            Message.id <= cursor_id if cursor_inclusive else Message.id < cursor_id
        )
        query = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.deleted_at.is_(None),
                or_(
                    Message.created_at < cursor_created_at,
                    and_(
                        Message.created_at == cursor_created_at,
                        same_timestamp_filter,
                    ),
                ),
                *((retention_filter,) if retention_filter is not None else ()),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(HISTORY_FETCH_BATCH)
        )
        batch = list((await session.execute(query)).scalars())
        if not batch:
            break

        stop = False
        for message in batch:
            estimated_tokens = count_message_tokens(message.role, message.content)
            if estimated_tokens <= 0:
                cursor_created_at = message.created_at
                cursor_id = message.id
                continue
            if (
                budget_tokens is not None
                and used_tokens + estimated_tokens > budget_tokens
            ):
                stop = True
                truncated = True
                break
            rows_desc.append(message)
            used_tokens += estimated_tokens
            cursor_created_at = message.created_at
            cursor_id = message.id

        if stop or len(batch) < HISTORY_FETCH_BATCH:
            break
        cursor_inclusive = False

    return rows_desc, used_tokens, truncated


async def _load_rows_desc_after_summary(
    session: Any,
    *,
    conversation_id: str,
    target: Message,
    summary: dict[str, Any],
    retention_filter: Any | None = None,
) -> list[Message]:
    rows_desc: list[Message] = []
    cursor_created_at = target.created_at
    cursor_id = target.id
    cursor_inclusive = True

    while True:
        same_timestamp_filter = (
            Message.id <= cursor_id if cursor_inclusive else Message.id < cursor_id
        )
        query = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.deleted_at.is_(None),
                or_(
                    Message.created_at < cursor_created_at,
                    and_(
                        Message.created_at == cursor_created_at,
                        same_timestamp_filter,
                    ),
                ),
                *((retention_filter,) if retention_filter is not None else ()),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(HISTORY_FETCH_BATCH)
        )
        batch = list((await session.execute(query)).scalars())
        if not batch:
            break

        stop = False
        for message in batch:
            cursor_created_at = message.created_at
            cursor_id = message.id
            if not _message_after_summary(summary, message):
                stop = True
                break
            rows_desc.append(message)

        if stop or len(batch) < HISTORY_FETCH_BATCH:
            break
        cursor_inclusive = False

    return rows_desc


async def _get_message(
    session: Any,
    message_id: str | None,
    *,
    logger: logging.Logger,
) -> Message | None:
    if not message_id:
        return None
    try:
        return await session.get(Message, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("completion.message_lookup_failed id=%s err=%r", message_id, exc)
        return None


async def _pick_first_user_from_summary(
    session: Any,
    summary: dict[str, Any],
    *,
    get_message: Callable[[Any, str | None], Awaitable[Message | None]],
) -> Message | None:
    first_id = summary.get("first_user_message_id")
    if not isinstance(first_id, str) or not first_id:
        return None
    message = await get_message(session, first_id)
    if message is not None and _role_eq(message.role, Role.USER):
        return message
    return None


async def _pick_current_user_with_lookup(
    session: Any,
    rows_desc: list[Message],
    target: Message,
    summary: dict[str, Any] | None = None,
    *,
    get_message: Callable[[Any, str | None], Awaitable[Message | None]],
) -> Message | None:
    current = _pick_current_user(rows_desc, target)
    if current is not None:
        return current
    parent_id = getattr(target, "parent_message_id", None)
    if not parent_id:
        return None
    candidate = await get_message(session, parent_id)
    if candidate is None or not _role_eq(candidate.role, Role.USER):
        return None
    if is_summary_usable(summary) and not _message_after_summary(summary, candidate):
        return None
    return candidate


def _pick_first_user(rows_desc: list[Message]) -> Message | None:
    users = [message for message in rows_desc if _role_eq(message.role, Role.USER)]
    if not users:
        return None
    return min(users, key=lambda message: (_message_created_at(message), message.id))


def _pick_current_user(
    rows_desc: list[Message],
    target: Message,
) -> Message | None:
    parent_id = getattr(target, "parent_message_id", None)
    if parent_id:
        for message in rows_desc:
            if message.id == parent_id and _role_eq(message.role, Role.USER):
                return message
    for message in rows_desc:
        if _role_eq(message.role, Role.USER):
            return message
    return None


async def _context_circuit_open(redis: Any | None) -> bool:
    if redis is None:
        return False
    try:
        value = await redis.get("context:circuit:breaker:state")
    except Exception:  # noqa: BLE001
        return False
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return bool(value and str(value).lower() not in {"0", "closed", "false"})


async def _resolve_summary_model(
    *,
    runtime_settings: Any,
    logger: logging.Logger,
) -> str:
    try:
        raw = await runtime_settings.resolve("context.summary_model")
    except Exception as exc:  # noqa: BLE001
        logger.debug("context summary model setting fallback err=%s", exc)
        raw = None
    return raw or "gpt-5.4"


async def _resolve_int_setting(
    spec_key: str,
    default: int,
    *,
    runtime_settings: Any,
    logger: logging.Logger,
) -> int:
    try:
        return await runtime_settings.resolve_int(spec_key, default)
    except Exception as exc:  # noqa: BLE001
        logger.debug("context int setting fallback key=%s err=%s", spec_key, exc)
        return default


async def _ensure_context_summary(
    session: Any,
    conversation: Conversation,
    boundary: _SummaryBoundary,
    *,
    target_tokens: int,
    model: str,
    redis: Any | None,
    service: Any,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    ensure = (
        getattr(service, "ensure_context_summary", None)
        if service is not None
        else None
    )
    if ensure is None:
        return None
    settings_payload = {
        "context.summary_target_tokens": target_tokens,
        "context.summary_model": model,
        "target_tokens": target_tokens,
        "summary_target_tokens": target_tokens,
        "model": model,
        "summary_model": model,
        "redis": redis,
        "trigger": "auto",
    }
    result = await ensure(
        session,
        conversation,
        boundary,
        settings_payload,
        force=False,
        extra_instruction=None,
        dry_run=False,
    )
    if isinstance(result, dict):
        summary = result.get("summary_jsonb") or result.get("summary") or result
        if is_summary_usable(summary):
            return summary
    try:
        await session.refresh(conversation)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "context summary refresh failed conv=%s err=%s",
            conversation.id,
            exc,
        )
    latest_summary = getattr(conversation, "summary_jsonb", None)
    if is_summary_usable(latest_summary):
        return latest_summary
    return None


async def _pack_recent_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    redis: Any | None = None,
    chat_model: str | None = None,
    account_mode: str | None = None,
    hooks: ContextLoadingHooks,
) -> PackedContext:
    from .context_packing import pack_recent_history

    return await pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
        redis=redis,
        chat_model=chat_model,
        account_mode=account_mode,
        hooks=hooks,
    )


async def _build_input_from_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    pack_recent_history: Callable[..., Awaitable[PackedContext]],
) -> list[dict[str, Any]]:
    packed = await pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
    )
    return packed.input_list
