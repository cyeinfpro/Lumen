"""Context-window accounting and summary-aware history helpers."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    CONTEXT_RESPONSE_TOKEN_RESERVE,
    CONTEXT_TOTAL_TOKEN_TARGET,
    HISTORY_FETCH_BATCH,
    MESSAGE_OVERHEAD_TOKENS,
    compare_message_position,
    compose_summary_guardrail,
    estimate_message_tokens,
    estimate_summary_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    format_sticky_input_text,
    is_summary_usable,
)
from lumen_core.models import Completion, Conversation, Message, SystemPrompt
from lumen_core.runtime_settings import get_spec, parse_value

from .cursor import coerce_aware, message_alive_filters
from ...runtime_settings import get_setting
from .contracts import ConversationContextOut


SUMMARY_TARGET_DEFAULT_TOKENS = 1200
SUMMARY_MIN_RECENT_DEFAULT_MESSAGES = 16
SUMMARY_MODEL_DEFAULT = "gpt-5.4"
MANUAL_COMPACT_DEFAULT_MIN_INPUT_TOKENS = 4000
MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS = 600
COMPACTION_MESSAGE_LOAD_LIMIT = 2000
CIRCUIT_BREAKER_KEY = "context:circuit:breaker:state"
_MANUAL_COMPACT_COOLDOWN_LUA = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])
local ok = redis.call('SET', key, '1', 'EX', ttl, 'NX')
if ok then
  return {1, ttl}
end
local existing_ttl = redis.call('TTL', key)
if existing_ttl < 0 then
  redis.call('EXPIRE', key, ttl)
  existing_ttl = ttl
end
return {0, existing_ttl}
"""

logger = logging.getLogger(__name__)


async def setting_int(db: AsyncSession, key: str, default: int) -> int:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    try:
        parsed = parse_value(spec, raw)
    except Exception:
        logger.warning("invalid runtime setting key=%s value=%r", key, raw)
        return default
    return parsed if isinstance(parsed, int) else default


async def setting_float(db: AsyncSession, key: str, default: float) -> float:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    try:
        parsed = parse_value(spec, raw)
    except Exception:
        logger.warning("invalid runtime setting key=%s value=%r", key, raw)
        return default
    if isinstance(parsed, (int, float)) and float(parsed) > 0:
        return float(parsed)
    return default


async def setting_str(db: AsyncSession, key: str, default: str) -> str:
    spec = get_spec(key)
    if spec is None:
        return default
    raw = await get_setting(db, spec)
    if raw is None:
        return default
    try:
        parsed = parse_value(spec, raw)
    except Exception:
        logger.warning("invalid runtime setting key=%s value=%r", key, raw)
        return default
    return parsed if isinstance(parsed, str) and parsed.strip() else default


def summary_updated_at(summary: dict[str, Any] | None) -> datetime | None:
    if not isinstance(summary, dict) or not is_summary_usable(summary):
        return None
    raw = summary.get("compressed_at") or summary.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return coerce_aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def summary_int(summary: dict[str, Any] | None, key: str) -> int:
    if not isinstance(summary, dict):
        return 0
    value = summary.get(key)
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


def summary_str(summary: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return value if isinstance(value, str) and value.strip() else None


def parse_summary_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return coerce_aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def summary_boundary(
    summary: dict[str, Any] | None,
) -> tuple[datetime, str | None] | None:
    if not isinstance(summary, dict) or not is_summary_usable(summary):
        return None
    created_at = parse_summary_datetime(summary.get("up_to_created_at"))
    if created_at is None:
        return None
    return created_at, summary_str(summary, "up_to_message_id")


def message_after_summary(
    message: Message,
    summary: dict[str, Any] | None,
) -> bool:
    boundary = summary_boundary(summary)
    if boundary is None:
        return True
    boundary_created_at, boundary_id = boundary
    return (
        compare_message_position(
            message.created_at,
            message.id,
            boundary_created_at,
            boundary_id,
        )
        > 0
    )


def with_summary_guardrail(
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


def truncate_sticky_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... truncated original task ...]"


def sticky_text_from_message(message: Message) -> str:
    content = message.content if isinstance(message.content, dict) else {}
    text = truncate_sticky_text(str(content.get("text") or ""))
    refs: list[str] = []
    for attachment in content.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        image_id = attachment.get("image_id")
        if image_id:
            refs.append(f"[user_image image_id={image_id}]")
        elif attachment.get("kind"):
            refs.append(f"[attachment kind={attachment.get('kind')!r}]")
    return "\n".join([text, *refs]).strip() if refs else text


def estimate_sticky_tokens(message: Message | None) -> int:
    if message is None:
        return 0
    sticky = sticky_text_from_message(message)
    if not sticky:
        return 0
    return MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(
        format_sticky_input_text(sticky)
    )


async def load_message_by_id(
    db: AsyncSession,
    message_id: str | None,
) -> Message | None:
    if not message_id:
        return None
    try:
        getter = getattr(db, "get", None)
        if getter is not None:
            message = await getter(Message, message_id)
            if message is not None:
                return message
    except Exception:
        logger.debug("message lookup by id failed", exc_info=True)
    try:
        return (
            await db.execute(
                select(Message)
                .where(Message.id == message_id, *message_alive_filters())
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.debug("message lookup query failed", exc_info=True)
        return None


async def load_prompt_content(
    db: AsyncSession,
    *,
    user_id: str,
    prompt_id: str | None,
) -> str | None:
    if not prompt_id:
        return None
    return (
        await db.execute(
            select(SystemPrompt.content).where(
                SystemPrompt.id == prompt_id,
                SystemPrompt.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


def simple_structured_system_prompt(
    *,
    global_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
) -> str | None:
    sections: list[str] = []
    for tag, candidate in (
        ("SYSTEM_GLOBAL", global_prompt),
        ("SYSTEM_CONVERSATION_LEGACY", legacy_conversation_prompt),
        ("SYSTEM_CONVERSATION", conversation_prompt),
    ):
        if candidate and candidate.strip():
            sections.append(f"[{tag}]\n{candidate.strip()}\n[/{tag}]")
    return (
        "\n".join(("[SYSTEM_PROMPTS]", *sections, "[/SYSTEM_PROMPTS]"))
        if sections
        else None
    )


def manual_compact_cooldown_key(*, user_id: str, conv_id: str) -> str:
    return f"context:manual_compact:{user_id}:{conv_id}:cooldown"


async def manual_compact_limit_status(
    redis: Any,
    *,
    user_id: str,
    conv_id: str,
    cooldown_seconds: int,
) -> tuple[bool, int, int]:
    if cooldown_seconds <= 0:
        return True, 1, 0
    key = manual_compact_cooldown_key(user_id=user_id, conv_id=conv_id)
    try:
        raw = await redis.get(key)
        if not raw:
            return True, 1, 0
        ttl = await redis.ttl(key)
        reset_seconds = (
            int(ttl) if isinstance(ttl, int) and ttl > 0 else cooldown_seconds
        )
    except Exception:
        logger.warning("manual compact cooldown status unavailable", exc_info=True)
        return True, 1, 0
    return False, 0, reset_seconds


def trace_id() -> str:
    return uuid.uuid4().hex[:12]


async def circuit_breaker_retry_after(redis: Any) -> int | None:
    try:
        state = await redis.get(CIRCUIT_BREAKER_KEY)
        if not state:
            return None
        ttl = await redis.ttl(CIRCUIT_BREAKER_KEY)
    except Exception:
        logger.warning("context circuit breaker status unavailable", exc_info=True)
        return None
    return int(ttl) if isinstance(ttl, int) and ttl > 0 else 60


def first_user_message(messages: list[Message]) -> Message | None:
    return next((message for message in messages if message.role == "user"), None)


def compaction_source_messages(
    messages: list[Message],
    *,
    min_recent_messages: int,
) -> tuple[list[Message], Message | None]:
    first_user = first_user_message(messages)
    recent_start = (
        max(0, len(messages) - 2)
        if len(messages) <= min_recent_messages
        else max(0, len(messages) - min_recent_messages)
    )
    candidates = messages[:recent_start]
    if first_user is not None:
        candidates = [message for message in candidates if message.id != first_user.id]
    if sum(estimate_message_tokens(m.role, m.content) for m in candidates) <= 0:
        return [], first_user
    return candidates, first_user


def estimate_messages_tokens(messages: list[Message]) -> int:
    return sum(
        estimate_message_tokens(message.role, message.content) for message in messages
    )


async def _scan_history(
    db: AsyncSession,
    *,
    conv: Conversation,
    summary: dict[str, Any] | None,
    initial_tokens: int,
) -> tuple[int, int, int, bool, list[Message]]:
    used_tokens = initial_tokens
    history_tokens = 0
    included_count = 0
    truncated = False
    cursor_created_at: datetime | None = None
    cursor_id: str | None = None
    scanned_desc: list[Message] = []
    while True:
        filters: list[Any] = [
            Message.conversation_id == conv.id,
            *message_alive_filters(),
        ]
        if cursor_created_at is not None and cursor_id is not None:
            filters.append(
                or_(
                    Message.created_at < cursor_created_at,
                    and_(
                        Message.created_at == cursor_created_at,
                        Message.id < cursor_id,
                    ),
                )
            )
        stmt = (
            select(Message)
            .where(*filters)
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(HISTORY_FETCH_BATCH)
        )
        batch = list((await db.execute(stmt)).scalars())
        if not batch:
            break
        stop = False
        for message in batch:
            cursor_created_at = message.created_at
            cursor_id = message.id
            if summary is not None and not message_after_summary(message, summary):
                stop = True
                break
            if len(scanned_desc) < COMPACTION_MESSAGE_LOAD_LIMIT:
                scanned_desc.append(message)
            estimated = estimate_message_tokens(message.role, message.content)
            if estimated <= 0:
                continue
            if used_tokens + estimated > CONTEXT_INPUT_TOKEN_BUDGET:
                truncated = True
                stop = True
                break
            used_tokens += estimated
            history_tokens += estimated
            included_count += 1
        if stop or len(batch) < HISTORY_FETCH_BATCH:
            break
    return used_tokens, history_tokens, included_count, truncated, scanned_desc


async def _latest_context_meta(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
) -> dict[str, Any]:
    try:
        upstream_request = (
            await db.execute(
                select(Completion.upstream_request)
                .join(Message, Completion.message_id == Message.id)
                .where(Message.conversation_id == conv_id)
                .order_by(desc(Completion.created_at), desc(Completion.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        context = (
            upstream_request.get("context")
            if isinstance(upstream_request, dict)
            else None
        )
        return context if isinstance(context, dict) else {}
    except Exception:
        logger.warning(
            "latest completion context lookup failed",
            exc_info=True,
            extra={"trace_id": trace_id(), "user_id": user_id, "conv_id": conv_id},
        )
        return {}


async def estimate_context_window(
    db: AsyncSession,
    *,
    conv: Conversation,
    user_id: str,
    user_default_prompt_id: str | None,
    redis: Any | None = None,
) -> ConversationContextOut:
    conversation_prompt = await load_prompt_content(
        db,
        user_id=user_id,
        prompt_id=conv.default_system_prompt_id,
    )
    global_prompt = await load_prompt_content(
        db,
        user_id=user_id,
        prompt_id=user_default_prompt_id,
    )
    system_prompt = simple_structured_system_prompt(
        global_prompt=global_prompt,
        conversation_prompt=conversation_prompt,
        legacy_conversation_prompt=conv.default_system,
    )
    raw_summary = getattr(conv, "summary_jsonb", None)
    summary = raw_summary if isinstance(raw_summary, dict) else None
    summary_available = is_summary_usable(summary)
    sticky_message = None
    if summary_available:
        sticky_message = await load_message_by_id(
            db,
            summary_str(summary, "first_user_message_id"),
        )
        if sticky_message is not None and message_after_summary(
            sticky_message,
            summary,
        ):
            sticky_message = None

    effective_system_prompt = with_summary_guardrail(
        system_prompt,
        enabled=summary_available,
    )
    system_tokens = estimate_system_prompt_tokens(effective_system_prompt)
    summary_tokens = estimate_summary_tokens(summary)
    sticky_tokens = estimate_sticky_tokens(sticky_message)
    summary_block_tokens = (
        MESSAGE_OVERHEAD_TOKENS + summary_tokens if summary_available else 0
    )
    initial_tokens = system_tokens + sticky_tokens + summary_block_tokens
    (
        used_tokens,
        history_tokens,
        included_count,
        truncated,
        scanned_desc,
    ) = await _scan_history(
        db,
        conv=conv,
        summary=summary if summary_available else None,
        initial_tokens=initial_tokens,
    )
    if summary_available:
        history_tokens += sticky_tokens + summary_block_tokens
        included_count += int(sticky_tokens > 0)

    compression_enabled = bool(await setting_int(db, "context.compression_enabled", 0))
    latest_meta = await _latest_context_meta(
        db,
        conv_id=conv.id,
        user_id=user_id,
    )
    latest_fallback = latest_meta.get("fallback_reason")
    if not isinstance(latest_fallback, str):
        latest_fallback = summary_str(summary, "last_fallback_reason")
    summary_target_tokens = await setting_int(
        db,
        "context.summary_target_tokens",
        SUMMARY_TARGET_DEFAULT_TOKENS,
    )
    compressible_count, compressible_tokens = await _compressible_estimate(
        db,
        scanned_desc=scanned_desc,
    )
    effective_summary_cost = (
        summary_block_tokens + sticky_tokens
        if summary_available
        else summary_target_tokens
    )
    estimated_tokens_freed = max(0, compressible_tokens - effective_summary_cost)
    manual_min_input_tokens = await setting_int(
        db,
        "context.manual_compact_min_input_tokens",
        MANUAL_COMPACT_DEFAULT_MIN_INPUT_TOKENS,
    )
    manual_cooldown_seconds = await setting_int(
        db,
        "context.manual_compact_cooldown_seconds",
        MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS,
    )
    available, reset_seconds, unavailable_reason = await _manual_status(
        redis,
        user_id=user_id,
        conv_id=conv.id,
        used_tokens=used_tokens,
        min_input_tokens=manual_min_input_tokens,
        cooldown_seconds=manual_cooldown_seconds,
    )
    return ConversationContextOut(
        input_budget_tokens=CONTEXT_INPUT_TOKEN_BUDGET,
        total_target_tokens=CONTEXT_TOTAL_TOKEN_TARGET,
        response_reserve_tokens=CONTEXT_RESPONSE_TOKEN_RESERVE,
        estimated_input_tokens=used_tokens,
        estimated_history_tokens=history_tokens,
        estimated_system_tokens=system_tokens,
        included_messages_count=included_count,
        truncated=truncated,
        percent=min(100.0, round(used_tokens / CONTEXT_INPUT_TOKEN_BUDGET * 100, 1)),
        compression_enabled=compression_enabled,
        summary_available=summary_available,
        summary_tokens=summary_tokens,
        summary_up_to_message_id=summary_str(summary, "up_to_message_id"),
        summary_updated_at=summary_updated_at(summary),
        summary_first_user_message_id=summary_str(summary, "first_user_message_id"),
        summary_compression_runs=summary_int(summary, "compression_runs"),
        compressible_messages_count=compressible_count,
        compressible_tokens=compressible_tokens,
        estimated_tokens_freed=estimated_tokens_freed,
        summary_target_tokens=summary_target_tokens,
        compressed=bool(latest_meta.get("summary_used")),
        last_fallback_reason=latest_fallback,
        manual_compact_available=available,
        manual_compact_reset_seconds=reset_seconds,
        manual_compact_min_input_tokens=manual_min_input_tokens,
        manual_compact_cooldown_seconds=manual_cooldown_seconds,
        manual_compact_unavailable_reason=unavailable_reason,
    )


async def _compressible_estimate(
    db: AsyncSession,
    *,
    scanned_desc: list[Message],
) -> tuple[int, int]:
    try:
        min_recent_messages = await setting_int(
            db,
            "context.summary_min_recent_messages",
            SUMMARY_MIN_RECENT_DEFAULT_MESSAGES,
        )
        source_messages, _ = compaction_source_messages(
            list(reversed(scanned_desc)),
            min_recent_messages=min_recent_messages,
        )
        return len(source_messages), estimate_messages_tokens(source_messages)
    except Exception:
        logger.warning("context compressible estimate failed", exc_info=True)
        return 0, 0


async def _manual_status(
    redis: Any | None,
    *,
    user_id: str,
    conv_id: str,
    used_tokens: int,
    min_input_tokens: int,
    cooldown_seconds: int,
) -> tuple[bool, int, str | None]:
    if used_tokens < min_input_tokens:
        return False, 0, "below_min_tokens"
    if redis is None:
        return True, 0, None
    retry_after = await circuit_breaker_retry_after(redis)
    if retry_after is not None:
        return False, retry_after, "circuit_open"
    available, _, reset_seconds = await manual_compact_limit_status(
        redis,
        user_id=user_id,
        conv_id=conv_id,
        cooldown_seconds=cooldown_seconds,
    )
    return (
        available,
        reset_seconds,
        None if available else "cooldown",
    )


__all__ = [
    "CIRCUIT_BREAKER_KEY",
    "COMPACTION_MESSAGE_LOAD_LIMIT",
    "CONTEXT_INPUT_TOKEN_BUDGET",
    "CONTEXT_RESPONSE_TOKEN_RESERVE",
    "CONTEXT_TOTAL_TOKEN_TARGET",
    "MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS",
    "MANUAL_COMPACT_DEFAULT_MIN_INPUT_TOKENS",
    "SUMMARY_MIN_RECENT_DEFAULT_MESSAGES",
    "SUMMARY_TARGET_DEFAULT_TOKENS",
    "compaction_source_messages",
    "circuit_breaker_retry_after",
    "estimate_context_window",
    "estimate_messages_tokens",
    "estimate_sticky_tokens",
    "first_user_message",
    "load_message_by_id",
    "load_prompt_content",
    "manual_compact_cooldown_key",
    "manual_compact_limit_status",
    "message_after_summary",
    "parse_summary_datetime",
    "setting_float",
    "setting_int",
    "setting_str",
    "simple_structured_system_prompt",
    "sticky_text_from_message",
    "summary_boundary",
    "summary_int",
    "summary_str",
    "summary_updated_at",
    "trace_id",
    "with_summary_guardrail",
]
