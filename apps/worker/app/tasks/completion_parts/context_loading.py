"""Database-backed completion history loading and context compression."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import and_, desc, or_, select

from lumen_core.constants import Role
from lumen_core.context_window import (
    HISTORY_FETCH_BATCH,
    MESSAGE_OVERHEAD_TOKENS,
    estimate_summary_tokens,
    format_sticky_input_text,
    format_summary_input_text,
    is_summary_usable,
)
from lumen_core.models import Conversation, Image, ImageVariant, Message

from .context import (
    PackedContext,
    _estimated_summary_source,
    _fallback_pack,
    _pack_with_existing_summary,
    _packed_with_input,
)
from .history import (
    _SummaryBoundary,
    _message_after_summary,
    _message_created_at,
    _role_eq,
    _sticky_text_from_message,
    _summary_age_seconds,
    _summary_compressed_at,
    _summary_covers_boundary,
    _instructions_with_summary_guardrail,
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
    input_budget = (
        hooks.get_input_budget(chat_model) if chat_model else hooks.input_token_budget
    )
    target = await session.get(Message, up_to_message_id)
    if target is None:
        return PackedContext(
            input_list=[],
            estimated_tokens=0,
            summary_used=False,
            summary_created=False,
            summary_up_to_message_id=None,
            sticky_used=False,
            included_messages_count=0,
            truncated_without_summary=False,
            fallback_reason=None,
            _system_prompt=system_prompt,
        )

    summary_model = await hooks.resolve_summary_model()
    conversation = await session.get(Conversation, conversation_id)
    summary = (
        getattr(conversation, "summary_jsonb", None)
        if conversation is not None
        else None
    )
    retention_filter = await hooks.message_retention_filter_for_account(account_mode)
    if retention_filter is not None:
        summary = None
    all_rows_desc: list[Message] | None = None
    total_used_tokens = 0
    total_truncated = False

    compression_enabled = bool(
        await hooks.resolve_int_setting(
            "context.compression_enabled",
            hooks.compression_enabled_default,
        )
    )
    trigger_percent = await hooks.resolve_int_setting(
        "context.compression_trigger_percent",
        hooks.compression_trigger_percent_default,
    )
    trigger_tokens = input_budget * trigger_percent // 100
    existing_summary_packed: PackedContext | None = None
    if is_summary_usable(summary):
        usable_summary = cast(dict[str, Any], summary)
        rows_after_summary = await hooks.load_rows_desc_after_summary(
            session,
            conversation_id=conversation_id,
            target=target,
            summary=usable_summary,
            retention_filter=retention_filter,
        )
        first_user = await hooks.pick_first_user_from_summary(
            session,
            usable_summary,
        )
        current_user = await hooks.pick_current_user_with_lookup(
            session,
            rows_after_summary,
            target,
            usable_summary,
        )
        existing_summary_packed = _pack_with_existing_summary(
            system_prompt=system_prompt,
            all_rows_desc=rows_after_summary,
            summary=usable_summary,
            summary_model=summary_model,
            input_budget=input_budget,
            current_user=current_user,
            first_user=first_user,
        )
        existing_summary_packed = _reestimate_existing_summary_tokens(
            existing_summary_packed,
            system_prompt=system_prompt,
            hooks=hooks,
        )
        if (
            not compression_enabled
            or existing_summary_packed.estimated_tokens < trigger_tokens
        ):
            return _packed_with_input(
                await hooks.build_input_from_packed_context(
                    session,
                    existing_summary_packed,
                ),
                existing_summary_packed,
            )

    all_rows_desc, total_used_tokens, total_truncated = await hooks.load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=None,
        system_prompt=system_prompt,
        retention_filter=retention_filter,
    )
    first_user = hooks.pick_first_user(all_rows_desc)
    current_user = await hooks.pick_current_user_with_lookup(
        session,
        all_rows_desc,
        target,
    )

    if not compression_enabled:
        rows_desc: list[Message] = []
        used_tokens = hooks.estimate_system_prompt_tokens(system_prompt)
        truncated = False
        for message in all_rows_desc:
            estimated = hooks.count_message_tokens(message.role, message.content)
            if estimated <= 0:
                continue
            if used_tokens + estimated > input_budget:
                truncated = True
                break
            rows_desc.append(message)
            used_tokens += estimated
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=rows_desc,
            used_tokens=used_tokens,
            truncated=truncated,
        )
        return _packed_with_input(
            await hooks.build_input_from_packed_context(session, packed),
            packed,
        )

    if await hooks.context_circuit_open(redis):
        rows_desc, used_tokens, truncated = await hooks.load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=rows_desc,
            used_tokens=used_tokens,
            truncated=truncated,
            compression_enabled=True,
            fallback_reason="circuit_open",
            force_include_message=hooks.pick_current_user(rows_desc, target),
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await hooks.build_input_from_packed_context(session, packed),
            packed,
        )

    target_tokens = await hooks.resolve_int_setting(
        "context.summary_target_tokens",
        hooks.summary_target_tokens_default,
    )
    min_recent_messages = max(
        1,
        await hooks.resolve_int_setting(
            "context.summary_min_recent_messages",
            hooks.summary_min_recent_messages_default,
        ),
    )
    min_interval_s = await hooks.resolve_int_setting(
        "context.summary_min_interval_seconds",
        hooks.summary_min_interval_seconds_default,
    )

    if (
        not is_summary_usable(summary)
        and total_used_tokens < trigger_tokens
        and not total_truncated
    ):
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=all_rows_desc,
            used_tokens=total_used_tokens,
            truncated=False,
            compression_enabled=True,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await hooks.build_input_from_packed_context(session, packed),
            packed,
        )

    forced_recent_desc: list[Message] = []
    for message in all_rows_desc:
        if hooks.count_message_tokens(message.role, message.content) <= 0:
            continue
        if len(forced_recent_desc) < min_recent_messages:
            forced_recent_desc.append(message)
    if current_user is not None and all(
        message.id != current_user.id for message in forced_recent_desc
    ):
        forced_recent_desc.insert(0, current_user)

    forced_ids = {message.id for message in forced_recent_desc}
    first_user_in_recent = first_user is not None and first_user.id in forced_ids
    sticky_message = (
        first_user if first_user is not None and not first_user_in_recent else None
    )
    sticky_tokens = 0
    if sticky_message is not None:
        sticky_input_text = format_sticky_input_text(
            _sticky_text_from_message(sticky_message)
        )
        sticky_tokens = MESSAGE_OVERHEAD_TOKENS + hooks.count_tokens(sticky_input_text)

    used_tokens = (
        hooks.estimate_system_prompt_tokens(
            _instructions_with_summary_guardrail(system_prompt, enabled=True)
        )
        + sticky_tokens
        + target_tokens
        + MESSAGE_OVERHEAD_TOKENS
    )
    recent_desc = list(forced_recent_desc)
    for message in forced_recent_desc:
        used_tokens += hooks.count_message_tokens(message.role, message.content)

    for message in all_rows_desc:
        if message.id in forced_ids:
            continue
        if sticky_message is not None and message.id == sticky_message.id:
            continue
        estimated = hooks.count_message_tokens(message.role, message.content)
        if estimated <= 0:
            continue
        if used_tokens + estimated > input_budget:
            break
        recent_desc.append(message)
        forced_ids.add(message.id)
        used_tokens += estimated

    recent_ids = {message.id for message in recent_desc}
    summary_rows = [
        message
        for message in all_rows_desc
        if hooks.count_message_tokens(message.role, message.content) > 0
        and message.id not in recent_ids
        and (sticky_message is None or message.id != sticky_message.id)
    ]
    if not summary_rows:
        if existing_summary_packed is not None:
            return _packed_with_input(
                await hooks.build_input_from_packed_context(
                    session,
                    existing_summary_packed,
                ),
                existing_summary_packed,
            )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=all_rows_desc,
            used_tokens=total_used_tokens,
            truncated=False,
            compression_enabled=True,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await hooks.build_input_from_packed_context(session, packed),
            packed,
        )

    boundary_message = max(
        summary_rows,
        key=lambda message: (_message_created_at(message), message.id),
    )
    summary_created = False

    summary_recently_refreshed = False
    if is_summary_usable(summary) and min_interval_s > 0:
        compressed_at = _summary_compressed_at(summary)
        if compressed_at is not None:
            now = datetime.now(compressed_at.tzinfo or timezone.utc)
            summary_recently_refreshed = (
                now - compressed_at
            ).total_seconds() < min_interval_s

    if summary_recently_refreshed and not _summary_covers_boundary(
        summary,
        boundary_message,
    ):
        (
            fallback_rows_desc,
            fallback_tokens,
            fallback_truncated,
        ) = await hooks.load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=fallback_rows_desc,
            used_tokens=fallback_tokens,
            truncated=fallback_truncated,
            compression_enabled=True,
            fallback_reason="rate_limited",
            force_include_message=current_user,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await hooks.build_input_from_packed_context(session, packed),
            packed,
        )

    if (
        not _summary_covers_boundary(
            summary,
            boundary_message,
        )
        and conversation is not None
    ):
        boundary = _SummaryBoundary(
            conversation_id=conversation_id,
            up_to_message_id=boundary_message.id,
            up_to_created_at=_message_created_at(boundary_message),
            first_user_message_id=first_user.id if first_user is not None else None,
            recent_message_ids=[message.id for message in reversed(recent_desc)],
            summary_message_ids=[message.id for message in reversed(summary_rows)],
            source_message_count=len(summary_rows),
            source_token_estimate=_estimated_summary_source(
                summary_rows,
                skip_message_id=(
                    sticky_message.id if sticky_message is not None else None
                ),
            ),
        )
        try:
            new_summary = await hooks.ensure_context_summary(
                session,
                conversation,
                boundary,
                target_tokens=target_tokens,
                model=summary_model,
                redis=redis,
            )
        except Exception as exc:  # noqa: BLE001
            hooks.logger.warning(
                "context summary generation failed conversation=%s err=%s",
                conversation_id,
                exc,
            )
            new_summary = None
        if is_summary_usable(new_summary):
            summary_created = True
            summary = new_summary

    if not _summary_covers_boundary(summary, boundary_message):
        (
            fallback_rows_desc,
            fallback_tokens,
            fallback_truncated,
        ) = await hooks.load_rows_desc(
            session,
            conversation_id=conversation_id,
            target=target,
            budget_tokens=input_budget,
            system_prompt=system_prompt,
            retention_filter=retention_filter,
        )
        packed = _fallback_pack(
            system_prompt=system_prompt,
            rows_desc=fallback_rows_desc,
            used_tokens=fallback_tokens,
            truncated=fallback_truncated,
            compression_enabled=True,
            fallback_reason="summary_failed",
            force_include_message=current_user,
            compressor_model=summary_model,
        )
        return _packed_with_input(
            await hooks.build_input_from_packed_context(session, packed),
            packed,
        )

    summary_text = str((summary or {}).get("text") or "")
    summary_token_count = estimate_summary_tokens(summary)
    recent_rows = tuple(reversed(recent_desc))
    estimated_tokens = (
        hooks.estimate_system_prompt_tokens(
            _instructions_with_summary_guardrail(system_prompt, enabled=True)
        )
        + (sticky_tokens if sticky_message is not None else 0)
        + MESSAGE_OVERHEAD_TOKENS
        + summary_token_count
        + sum(
            hooks.count_message_tokens(message.role, message.content)
            for message in recent_rows
        )
    )
    packed = PackedContext(
        input_list=[],
        estimated_tokens=estimated_tokens,
        summary_used=True,
        summary_created=summary_created,
        summary_up_to_message_id=str(
            (summary or {}).get("up_to_message_id") or boundary_message.id
        ),
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
        image_caption_count=int((summary or {}).get("image_caption_count") or 0),
        _system_prompt=system_prompt,
        _sticky_message=sticky_message,
        _summary_text=summary_text,
        _recent_rows=recent_rows,
    )
    return _packed_with_input(
        await hooks.build_input_from_packed_context(session, packed),
        packed,
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
