"""Conversations 路由（DESIGN §5.3）。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    SystemPrompt,
)
from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    CONTEXT_RESPONSE_TOKEN_RESERVE,
    CONTEXT_TOTAL_TOKEN_TARGET,
    HISTORY_FETCH_BATCH,
    MESSAGE_OVERHEAD_TOKENS,
    compose_summary_guardrail,
    estimate_message_tokens,
    estimate_summary_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    format_sticky_input_text,
    is_summary_usable,
    messages_token_count,
    would_exceed_budget,
    SUMMARY_KIND,
    SUMMARY_VERSION,
)
from lumen_core.runtime_settings import get_spec, parse_value
from lumen_core.schemas import (
    CompletionOut,
    ConversationOut,
    ConversationPatchIn,
    GenerationOut,
    ImageOut,
    MessageOut,
)

from ..audit import hash_email, request_ip_hash, write_audit
from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..runtime_settings import get_setting


router = APIRouter()
TASK_INCLUDE_LIMIT = 100
CURSOR_VERSION = 1
MANUAL_COMPACT_DEFAULT_MIN_INPUT_TOKENS = 4000
MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS = 600
SUMMARY_TARGET_DEFAULT_TOKENS = 1200
SUMMARY_MIN_RECENT_DEFAULT_MESSAGES = 16
SUMMARY_MODEL_DEFAULT = "gpt-5.4"
MANUAL_COMPACT_MIN_TARGET_TOKENS = 300
MANUAL_COMPACT_MAX_TARGET_TOKENS = 4000
MANUAL_COMPACT_EXTRA_INSTRUCTION_MAX_CHARS = 1000
COMPACTION_MESSAGE_LOAD_LIMIT = 2000
CIRCUIT_BREAKER_KEY = "context:circuit:breaker:state"
MANUAL_COMPACT_JOB_TTL_SECONDS = 24 * 3600
MANUAL_COMPACT_ACTIVE_TTL_SECONDS = 30 * 60
MANUAL_COMPACT_RETRY_AFTER_SECONDS = 2

logger = logging.getLogger(__name__)


# ---------- cursor helpers ----------

def _enc_cursor(payload: dict[str, Any]) -> str:
    body = {"v": CURSOR_VERSION, **payload}
    raw = json.dumps(body, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _dec_cursor(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        pad = "=" * (-len(raw) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(raw + pad).decode())
    except Exception:
        logger.warning("cursor decode failed", exc_info=True)
        return None
    if not isinstance(decoded, dict):
        return None
    version = decoded.get("v")
    if version is not None and version != CURSOR_VERSION:
        logger.warning("cursor version mismatch: got=%r want=%d", version, CURSOR_VERSION)
        return None
    return decoded


def _coerce_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "not_found", "message": "conversation not found"}},
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"error": {"code": "forbidden", "message": "conversation forbidden"}},
    )


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"code": code, "message": message}},
    )


def _service_unavailable(reason: str) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "code": "compression_unavailable",
                "message": "compression unavailable",
                "reason": reason,
                "details": {"reason": reason},
            }
        },
    )


def _message_alive_filters() -> tuple[Any, ...]:
    deleted_at = getattr(Message, "deleted_at", None)
    if deleted_at is None:
        return ()
    return (deleted_at.is_(None),)


async def _get_owned_conv(db: AsyncSession, conv_id: str, user_id: str) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise _not_found()
    return conv


async def _get_owned_conv_for_update(
    db: AsyncSession, conv_id: str, user_id: str
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update(of=Conversation)
        )
    ).scalar_one_or_none()
    if not conv:
        raise _not_found()
    return conv


async def _soft_delete_conversation_generated_images(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    deleted_at: datetime,
) -> int:
    generation_ids = (
        select(Generation.id)
        .join(Message, Message.id == Generation.message_id)
        .where(
            Message.conversation_id == conv_id,
            Generation.user_id == user_id,
        )
    )
    result = await db.execute(
        update(Image)
        .where(
            Image.user_id == user_id,
            Image.deleted_at.is_(None),
            Image.owner_generation_id.in_(generation_ids),
        )
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def _cancel_conversation_active_generations(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    canceled_at: datetime,
) -> int:
    message_ids = select(Message.id).where(Message.conversation_id == conv_id)
    result = await db.execute(
        update(Generation)
        .where(
            Generation.user_id == user_id,
            Generation.message_id.in_(message_ids),
            Generation.status.in_(
                [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
            ),
        )
        .values(
            status=GenerationStatus.CANCELED.value,
            progress_stage="finalizing",
            finished_at=canceled_at,
            error_code="cancelled",
            error_message="conversation deleted",
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


# ---------- list / search ----------

class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    next_cursor: str | None = None


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    q: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> ConversationListOut:
    stmt = select(Conversation).where(
        Conversation.user_id == user.id,
        Conversation.deleted_at.is_(None),
    )
    if q:
        # Why: escape LIKE wildcards so user input cannot match outside intent.
        # Cap the search length to bound worst-case backend work and reduce
        # surface for pathological / adversarial patterns.
        q_trimmed = q.strip()[:200]
        if q_trimmed:
            q_escaped = (
                q_trimmed.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            stmt = stmt.where(
                Conversation.title.ilike(f"%{q_escaped}%", escape="\\")
            )
    cur = _dec_cursor(cursor)
    if cur and "la" in cur and "id" in cur:
        la = _coerce_aware(datetime.fromisoformat(cur["la"]))
        stmt = stmt.where(
            or_(
                Conversation.last_activity_at < la,
                and_(
                    Conversation.last_activity_at == la,
                    Conversation.id < cur["id"],
                ),
            )
        )
    stmt = stmt.order_by(
        desc(Conversation.last_activity_at), desc(Conversation.id)
    ).limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = _enc_cursor(
            {"la": last.last_activity_at.isoformat(), "id": last.id}
        )
    return ConversationListOut(
        items=[ConversationOut.model_validate(c) for c in items],
        next_cursor=next_cursor,
    )


# ---------- create ----------

class ConversationCreateIn(BaseModel):
    title: str = ""
    default_system: str | None = None
    default_params: dict[str, Any] | None = None
    default_system_prompt_id: str | None = None


@router.post("", response_model=ConversationOut, dependencies=[Depends(verify_csrf)])
async def create_conversation(
    body: ConversationCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    if body.title and len(body.title) > 500:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "invalid_title",
                    "message": "title exceeds 500 characters",
                }
            },
        )
    if body.default_system_prompt_id is not None:
        prompt_exists = (
            await db.execute(
                select(SystemPrompt.id).where(
                    SystemPrompt.id == body.default_system_prompt_id,
                    SystemPrompt.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if prompt_exists is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "system_prompt_not_found",
                        "message": "system prompt not found",
                    }
                },
            )

    conv = Conversation(
        user_id=user.id,
        title=body.title or "",
        default_system=body.default_system,
        default_system_prompt_id=body.default_system_prompt_id,
        default_params=body.default_params or {},
    )
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return ConversationOut.model_validate(conv)


# ---------- get / patch / delete ----------

@router.get("/{conv_id}", response_model=ConversationOut)
async def get_conversation(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    conv = await _get_owned_conv(db, conv_id, user.id)
    return ConversationOut.model_validate(conv)


@router.patch(
    "/{conv_id}", response_model=ConversationOut, dependencies=[Depends(verify_csrf)]
)
async def patch_conversation(
    conv_id: str,
    body: ConversationPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    conv = await _get_owned_conv(db, conv_id, user.id)
    if body.title is not None:
        if len(body.title) > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_title",
                        "message": "title exceeds 500 characters",
                    }
                },
            )
        conv.title = body.title
    if body.pinned is not None:
        conv.pinned = body.pinned
    if body.archived is not None:
        conv.archived = body.archived
    if body.default_params is not None:
        conv.default_params = body.default_params
    if body.default_system is not None:
        conv.default_system = body.default_system
    if body.default_system_prompt_id is not None:
        prompt_exists = (
            await db.execute(
                select(SystemPrompt.id).where(
                    SystemPrompt.id == body.default_system_prompt_id,
                    SystemPrompt.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if prompt_exists is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "system_prompt_not_found",
                        "message": "system prompt not found",
                    }
                },
            )
        conv.default_system_prompt_id = body.default_system_prompt_id
    await db.commit()
    await db.refresh(conv)
    return ConversationOut.model_validate(conv)


@router.delete("/{conv_id}", dependencies=[Depends(verify_csrf)])
async def delete_conversation(
    conv_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    conv = await _get_owned_conv_for_update(db, conv_id, user.id)
    now = datetime.now(timezone.utc)
    conv.deleted_at = now
    deleted_images = await _soft_delete_conversation_generated_images(
        db,
        conv_id=conv.id,
        user_id=user.id,
        deleted_at=now,
    )
    canceled_generations = await _cancel_conversation_active_generations(
        db,
        conv_id=conv.id,
        user_id=user.id,
        canceled_at=now,
    )
    await write_audit(
        db,
        event_type="conversation.delete",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "conversation_id": conv.id,
            "images_deleted": deleted_images,
            "generations_canceled": canceled_generations,
        },
    )
    await db.commit()
    return {"ok": True}


# ---------- messages listing ----------

class MessageListOut(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None = None
    # 可选附带数据（include=tasks）。前端刷新后用来恢复 store.generations / completions / imagesById，
    # 让会话中的图片和进行中任务卡片在没有 SSE 上下文时也能正常展示。
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


async def _setting_int(db: AsyncSession, key: str, default: int) -> int:
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


async def _setting_float(db: AsyncSession, key: str, default: float) -> float:
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
    if isinstance(parsed, (int, float)):
        value = float(parsed)
        return value if value > 0 else default
    return default


async def _setting_str(db: AsyncSession, key: str, default: str) -> str:
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


def _summary_updated_at(summary: dict[str, Any] | None) -> datetime | None:
    if not is_summary_usable(summary):
        return None
    raw = summary.get("compressed_at") or summary.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return _coerce_aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _summary_int(summary: dict[str, Any] | None, key: str) -> int:
    if not isinstance(summary, dict):
        return 0
    value = summary.get(key)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0:
        return int(value)
    return 0


def _summary_str(summary: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(summary, dict):
        return None
    value = summary.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _parse_summary_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return _coerce_aware(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _compare_message_position(
    left_created_at: datetime,
    left_id: str | None,
    right_created_at: datetime,
    right_id: str | None,
) -> int:
    left_created_at = _coerce_aware(left_created_at)
    right_created_at = _coerce_aware(right_created_at)
    if left_created_at > right_created_at:
        return 1
    if left_created_at < right_created_at:
        return -1
    if not left_id or not right_id:
        return 0 if left_id == right_id else -1
    if left_id > right_id:
        return 1
    if left_id < right_id:
        return -1
    return 0


def _summary_boundary(summary: dict[str, Any] | None) -> tuple[datetime, str | None] | None:
    if not is_summary_usable(summary):
        return None
    created_at = _parse_summary_datetime(summary.get("up_to_created_at"))
    if created_at is None:
        return None
    return created_at, _summary_str(summary, "up_to_message_id")


def _message_after_summary(msg: Message, summary: dict[str, Any] | None) -> bool:
    boundary = _summary_boundary(summary)
    if boundary is None:
        return True
    boundary_created_at, boundary_id = boundary
    return _compare_message_position(
        msg.created_at,
        msg.id,
        boundary_created_at,
        boundary_id,
    ) > 0


def _with_summary_guardrail(system_prompt: str | None, *, enabled: bool) -> str | None:
    if not enabled:
        return system_prompt
    guardrail = compose_summary_guardrail()
    if system_prompt:
        if guardrail in system_prompt:
            return system_prompt
        return f"{system_prompt.rstrip()}\n\n{guardrail}"
    return None


def _truncate_sticky_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[... truncated original task ...]"


def _sticky_text_from_message(message: Message) -> str:
    content = message.content if isinstance(message.content, dict) else {}
    text = _truncate_sticky_text(str(content.get("text") or ""))
    refs: list[str] = []
    for att in content.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        image_id = att.get("image_id")
        if image_id:
            refs.append(f"[user_image image_id={image_id}]")
        elif att.get("kind"):
            refs.append(f"[attachment kind={att.get('kind')!r}]")
    if refs:
        return "\n".join([text, *refs]).strip()
    return text


def _estimate_sticky_tokens(message: Message | None) -> int:
    if message is None:
        return 0
    sticky = _sticky_text_from_message(message)
    if not sticky:
        return 0
    return MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(format_sticky_input_text(sticky))


async def _load_message_by_id(
    db: AsyncSession,
    message_id: str | None,
) -> Message | None:
    if not message_id:
        return None
    try:
        getter = getattr(db, "get", None)
        if getter is not None:
            msg = await getter(Message, message_id)
            if msg is not None:
                return msg
    except Exception:
        logger.debug("message lookup by id failed", exc_info=True)
    try:
        return (
            await db.execute(
                select(Message).where(Message.id == message_id, *_message_alive_filters()).limit(1)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.debug("message lookup query failed", exc_info=True)
        return None


def _manual_compact_cooldown_key(*, user_id: str, conv_id: str) -> str:
    return f"context:manual_compact:{user_id}:{conv_id}:cooldown"


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


async def _manual_compact_limit_status(
    redis: Any,
    *,
    user_id: str,
    conv_id: str,
    cooldown_seconds: int,
) -> tuple[bool, int, int]:
    if cooldown_seconds <= 0:
        return True, 1, 0
    key = _manual_compact_cooldown_key(user_id=user_id, conv_id=conv_id)
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


async def _check_manual_compact_cooldown(
    redis: Any,
    *,
    user_id: str,
    conv_id: str,
    cooldown_seconds: int,
) -> tuple[int, int]:
    if cooldown_seconds <= 0:
        return 1, 0
    key = _manual_compact_cooldown_key(user_id=user_id, conv_id=conv_id)
    try:
        result = await redis.eval(
            _MANUAL_COMPACT_COOLDOWN_LUA,
            1,
            key,
            str(cooldown_seconds),
        )
        allowed = int(result[0])
        raw_reset_seconds = int(result[1])
        reset_seconds = raw_reset_seconds if raw_reset_seconds > 0 else cooldown_seconds
    except Exception as exc:
        logger.error("manual compact cooldown limiter unavailable", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "cooldown_limiter_unavailable",
                    "message": "manual compact cooldown limiter unavailable",
                }
            },
            headers={"Retry-After": "1"},
        ) from exc

    if not allowed:
        cooldown_minutes = max(1, round(cooldown_seconds / 60))
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": "manual_compact_cooldown",
                    "message": f"同一会话 {cooldown_minutes} 分钟内只能手动压缩一次",
                    "rate_limit_remaining": 0,
                    "rate_limit_reset_seconds": reset_seconds,
                    "details": {
                        "rate_limit_remaining": 0,
                        "rate_limit_reset_seconds": reset_seconds,
                    },
                }
            },
            headers={"Retry-After": str(max(1, reset_seconds))},
        )
    return 0, reset_seconds


async def _circuit_breaker_retry_after(redis: Any) -> int | None:
    try:
        state = await redis.get(CIRCUIT_BREAKER_KEY)
        if not state:
            return None
        ttl = await redis.ttl(CIRCUIT_BREAKER_KEY)
    except Exception:
        logger.warning("context circuit breaker status unavailable", exc_info=True)
        return None
    return int(ttl) if isinstance(ttl, int) and ttl > 0 else 60


def _extra_instruction_hash(extra_instruction: str | None) -> str | None:
    if not extra_instruction:
        return None
    digest = hashlib.sha1(extra_instruction.encode("utf-8")).hexdigest()
    return f"sha1:{digest}"


def _message_summary_line(msg: Message) -> str:
    content = msg.content or {}
    text = content.get("text") if isinstance(content, dict) else None
    text = text if isinstance(text, str) else ""
    text = text.strip()
    if len(text) > 1200:
        text = f"{text[:700]}\n[... elided ...]\n{text[-300:]}"
    parts = [f"[{msg.role.upper()} #{msg.id} @ {msg.created_at.isoformat()}]"]
    if text:
        parts.append(text)
    if isinstance(content, dict):
        attachments = content.get("attachments") or []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            image_id = att.get("image_id")
            if image_id:
                parts.append(f"[user_image image_id={image_id}]")
    return "\n".join(parts)


async def _load_messages_for_compaction(
    db: AsyncSession, conv_id: str
) -> list[Message]:
    rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id == conv_id, *_message_alive_filters())
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(COMPACTION_MESSAGE_LOAD_LIMIT)
        )
    ).scalars().all()
    return list(reversed(rows))


def _first_user_message(messages: list[Message]) -> Message | None:
    for msg in messages:
        if msg.role == "user":
            return msg
    return None


def _compaction_source_messages(
    messages: list[Message],
    *,
    min_recent_messages: int,
) -> tuple[list[Message], Message | None]:
    first_user = _first_user_message(messages)
    if len(messages) <= min_recent_messages:
        # Manual compaction is token-gated, not only message-count-gated. A
        # short conversation can exceed the 4k manual threshold with repeated
        # large prompts, while still falling entirely inside the default
        # "keep recent 16 messages" window. In that case preserve the original
        # task and the latest turn, and allow the middle history to compact.
        recent_start = max(0, len(messages) - 2)
    else:
        recent_start = max(0, len(messages) - min_recent_messages)
    candidates = messages[:recent_start]
    if first_user is not None:
        candidates = [msg for msg in candidates if msg.id != first_user.id]
    if sum(estimate_message_tokens(msg.role, msg.content) for msg in candidates) <= 0:
        return [], first_user
    return candidates, first_user


def _estimate_messages_tokens(messages: list[Message]) -> int:
    return sum(estimate_message_tokens(msg.role, msg.content) for msg in messages)


def _truncate_to_estimated_tokens(text: str, target_tokens: int) -> str:
    if estimate_text_tokens(text) <= target_tokens:
        return text
    # This mirrors the core estimator's ASCII-heavy behavior closely enough for
    # the local fallback while avoiding a tokenization dependency in the API app.
    limit = max(1, target_tokens * 4)
    return text[:limit].rstrip() + "\n[... summary truncated ...]"


def _import_worker_context_summary() -> Any | None:
    """Resolve the worker-side ``ensure_context_summary`` from the api process.

    Why the dance: the api venv installs ``apps/api`` as an editable package
    whose top-level is ``app``, which shadows the worker's separate ``app``
    package. The dotted path ``apps.worker.app.tasks.context_summary`` only
    resolves when the repo root is on ``sys.path`` so PEP 420 namespace
    packages (``apps`` / ``apps.worker``) can be discovered without colliding
    with api's own ``app`` imports. Without this, the manual compact path
    silently fell back to "service unavailable" — which only ever surfaced
    once force=True started actually invoking ensure (the old below-budget
    short-circuit was hiding it).
    """
    module_name = "apps.worker.app.tasks.context_summary"
    try:
        return import_module(module_name)
    except ModuleNotFoundError:
        pass
    except Exception:
        logger.warning("worker context summary import failed", exc_info=True)
        return None

    project_root = str(Path(__file__).resolve().parents[4])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    try:
        return import_module(module_name)
    except ModuleNotFoundError:
        worker_module = Path(project_root) / "apps" / "worker" / "app" / "tasks" / "context_summary.py"
        logger.warning("worker context summary module not found at %s", worker_module)
        return None
    except Exception:
        logger.warning("worker context summary import failed", exc_info=True)
        return None


async def _local_ensure_context_summary(
    db: AsyncSession,
    *,
    conv: Conversation,
    force: bool,
    extra_instruction: str | None,
    target_tokens: int,
    dry_run: bool,
    min_recent_messages: int,
    model: str,
) -> dict[str, Any]:
    messages = await _load_messages_for_compaction(db, conv.id)
    source_messages, first_user = _compaction_source_messages(
        messages, min_recent_messages=min_recent_messages
    )
    source_token_estimate = _estimate_messages_tokens(source_messages)
    boundary = source_messages[-1] if source_messages else None

    if dry_run:
        return {
            "ok": True,
            "would_compress": bool(source_messages),
            "estimated_source_messages": len(source_messages),
            "estimated_source_tokens": source_token_estimate,
            "estimated_output_tokens_max": target_tokens,
        }

    summary = conv.summary_jsonb if isinstance(conv.summary_jsonb, dict) else None
    extra_hash = _extra_instruction_hash(extra_instruction)
    if (
        not force
        and boundary is not None
        and is_summary_usable(summary)
        and summary.get("up_to_message_id") == boundary.id
        and summary.get("extra_instruction_hash") == extra_hash
    ):
        return {
            "ok": True,
            "summary_tokens": estimate_summary_tokens(summary),
            "source_message_count": _summary_int(summary, "source_message_count"),
            "source_token_estimate": _summary_int(summary, "source_token_estimate"),
            "summary_up_to_message_id": summary.get("up_to_message_id"),
            "model": summary.get("model") or model,
            "cached": True,
            "fallback_reason": None,
            "image_caption_count": _summary_int(summary, "image_caption_count"),
        }

    if boundary is None or first_user is None:
        return {
            "ok": True,
            "summary_tokens": 0,
            "source_message_count": 0,
            "source_token_estimate": 0,
            "summary_up_to_message_id": None,
            "model": model,
            "cached": False,
            "fallback_reason": "insufficient_history",
            "image_caption_count": 0,
        }

    lines = [
        "## Earlier Context Summary",
        "### Source Messages",
        *(_message_summary_line(msg) for msg in source_messages),
    ]
    if extra_instruction:
        lines.extend(["### Additional Hints From User", extra_instruction.strip()])
    text = _truncate_to_estimated_tokens("\n\n".join(lines), target_tokens)
    compressed_at = datetime.now(timezone.utc).isoformat()
    summary_tokens = estimate_text_tokens(text)
    previous_runs = _summary_int(summary, "compression_runs")
    image_caption_count = sum(
        1
        for msg in source_messages
        for att in ((msg.content or {}).get("attachments") or [])
        if isinstance(att, dict) and att.get("image_id")
    )
    conv.summary_jsonb = {
        "version": SUMMARY_VERSION,
        "kind": SUMMARY_KIND,
        "up_to_message_id": boundary.id,
        "up_to_created_at": boundary.created_at.isoformat(),
        "first_user_message_id": first_user.id,
        "text": text,
        "tokens": summary_tokens,
        "source_message_count": len(source_messages),
        "source_token_estimate": source_token_estimate,
        "model": model,
        "image_caption_count": image_caption_count,
        "extra_instruction_hash": extra_hash,
        "compressed_at": compressed_at,
        "compression_runs": previous_runs + 1,
        "last_quality_signal": None,
    }
    # Preserve the caller's transaction boundary; this helper only stages work.
    await db.flush()
    return {
        "ok": True,
        "summary_tokens": summary_tokens,
        "source_message_count": len(source_messages),
        "source_token_estimate": source_token_estimate,
        "summary_up_to_message_id": boundary.id,
        "model": model,
        "cached": False,
        "fallback_reason": None,
        "image_caption_count": image_caption_count,
    }


async def _ensure_context_summary_compatible(
    db: AsyncSession,
    *,
    conv: Conversation,
    redis: Any | None,
    force: bool,
    extra_instruction: str | None,
    target_tokens: int,
    dry_run: bool,
    min_recent_messages: int,
    model: str,
) -> dict[str, Any]:
    module = _import_worker_context_summary()
    ensure = getattr(module, "ensure_context_summary", None) if module else None
    if ensure is None:
        if not dry_run:
            messages = await _load_messages_for_compaction(db, conv.id)
            source_messages, _first_user = _compaction_source_messages(
                messages, min_recent_messages=min_recent_messages
            )
            return {
                "ok": False,
                "summary_tokens": 0,
                "source_message_count": len(source_messages),
                "source_token_estimate": _estimate_messages_tokens(source_messages),
                "summary_up_to_message_id": None,
                "model": model,
                "cached": False,
                "fallback_reason": "summary_service_unavailable",
                "image_caption_count": 0,
            }
        return await _local_ensure_context_summary(
            db,
            conv=conv,
            force=force,
            extra_instruction=extra_instruction,
            target_tokens=target_tokens,
            dry_run=dry_run,
            min_recent_messages=min_recent_messages,
            model=model,
        )
    messages = await _load_messages_for_compaction(db, conv.id)
    source_messages, _first_user = _compaction_source_messages(
        messages, min_recent_messages=min_recent_messages
    )
    boundary = source_messages[-1] if source_messages else None
    if boundary is None:
        return await _local_ensure_context_summary(
            db,
            conv=conv,
            force=force,
            extra_instruction=extra_instruction,
            target_tokens=target_tokens,
            dry_run=dry_run,
            min_recent_messages=min_recent_messages,
            model=model,
        )

    input_budget = await _setting_int(db, "context.summary_input_budget", 80_000)
    result = await ensure(
        db,
        conv,
        boundary,
        {
            "context.summary_target_tokens": target_tokens,
            "context.summary_input_budget": input_budget,
            "context.summary_model": model,
            "redis": redis,
        },
        force=force,
        extra_instruction=extra_instruction,
        dry_run=dry_run,
        trigger="manual",
    )
    if not isinstance(result, dict):
        return {
            "ok": False,
            "summary_tokens": 0,
            "source_message_count": len(source_messages),
            "source_token_estimate": _estimate_messages_tokens(source_messages),
            "summary_up_to_message_id": None,
            "model": model,
            "cached": False,
            "fallback_reason": "summary_failed",
            "image_caption_count": 0,
        }

    if dry_run or result.get("dry_run"):
        return {
            "ok": True,
            "would_compress": bool(
                result.get("would_compress", result.get("would_call_upstream", False))
            ),
            "estimated_source_messages": int(
                result.get("estimated_source_messages")
                or result.get("source_message_count")
                or 0
            ),
            "estimated_source_tokens": int(
                result.get("estimated_source_tokens")
                or result.get("source_token_estimate")
                or 0
            ),
            "estimated_output_tokens_max": target_tokens,
        }

    status = str(result.get("status") or "")
    return {
        "ok": True,
        "summary_tokens": int(result.get("summary_tokens") or 0),
        "source_message_count": int(result.get("source_message_count") or 0),
        "source_token_estimate": int(result.get("source_token_estimate") or 0),
        "summary_up_to_message_id": result.get("summary_up_to_message_id"),
        "model": model,
        "cached": status.startswith("cached") or status == "cas_reused",
        "fallback_reason": None,
        "image_caption_count": int(result.get("image_caption_count") or 0),
    }


async def _load_prompt_content(
    db: AsyncSession, *, user_id: str, prompt_id: str | None
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


def _simple_structured_system_prompt(
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
    if not sections:
        return None
    return "\n".join(("[SYSTEM_PROMPTS]", *sections, "[/SYSTEM_PROMPTS]"))


async def _estimate_context_window(
    db: AsyncSession,
    *,
    conv: Conversation,
    user_id: str,
    user_default_prompt_id: str | None,
    redis: Any | None = None,
) -> ConversationContextOut:
    conversation_prompt = await _load_prompt_content(
        db, user_id=user_id, prompt_id=conv.default_system_prompt_id
    )
    global_prompt = await _load_prompt_content(
        db, user_id=user_id, prompt_id=user_default_prompt_id
    )
    system_prompt = _simple_structured_system_prompt(
        global_prompt=global_prompt,
        conversation_prompt=conversation_prompt,
        legacy_conversation_prompt=conv.default_system,
    )

    raw_summary = getattr(conv, "summary_jsonb", None)
    summary = raw_summary if isinstance(raw_summary, dict) else None
    summary_available = is_summary_usable(summary)
    sticky_message = None
    if summary_available:
        first_user_id = _summary_str(summary, "first_user_message_id")
        candidate = await _load_message_by_id(db, first_user_id)
        if candidate is not None and not _message_after_summary(candidate, summary):
            sticky_message = candidate

    effective_system_prompt = _with_summary_guardrail(
        system_prompt,
        enabled=summary_available,
    )
    system_tokens = estimate_system_prompt_tokens(effective_system_prompt)
    used_tokens = system_tokens
    history_tokens = 0
    included_messages_count = 0
    summary_tokens = estimate_summary_tokens(summary)
    sticky_tokens = 0
    summary_block_tokens = 0
    if summary_available:
        sticky_tokens = _estimate_sticky_tokens(sticky_message)
        summary_block_tokens = MESSAGE_OVERHEAD_TOKENS + summary_tokens
        used_tokens += sticky_tokens + summary_block_tokens
        history_tokens += sticky_tokens + summary_block_tokens
        included_messages_count += 1 if sticky_tokens > 0 else 0
    truncated = False
    cursor_created_at: datetime | None = None
    cursor_id: str | None = None
    alive_filters = _message_alive_filters()
    scanned_messages_desc: list[Message] = []

    while True:
        filters: list[Any] = [Message.conversation_id == conv.id, *alive_filters]
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
        for msg in batch:
            cursor_created_at = msg.created_at
            cursor_id = msg.id
            if summary_available and not _message_after_summary(msg, summary):
                stop = True
                break
            if len(scanned_messages_desc) < COMPACTION_MESSAGE_LOAD_LIMIT:
                scanned_messages_desc.append(msg)
            est_tokens = estimate_message_tokens(msg.role, msg.content)
            if est_tokens <= 0:
                continue
            if used_tokens + est_tokens > CONTEXT_INPUT_TOKEN_BUDGET:
                truncated = True
                stop = True
                break
            used_tokens += est_tokens
            history_tokens += est_tokens
            included_messages_count += 1

        if stop or len(batch) < HISTORY_FETCH_BATCH:
            break

    percent = min(100.0, round((used_tokens / CONTEXT_INPUT_TOKEN_BUDGET) * 100, 1))
    compression_enabled = bool(
        await _setting_int(db, "context.compression_enabled", 0)
    )
    latest_context_meta: dict[str, Any] = {}
    try:
        upstream_request = (
            await db.execute(
                select(Completion.upstream_request)
                .join(Message, Completion.message_id == Message.id)
                .where(Message.conversation_id == conv.id)
                .order_by(desc(Completion.created_at), desc(Completion.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if isinstance(upstream_request, dict) and isinstance(
            upstream_request.get("context"), dict
        ):
            latest_context_meta = upstream_request["context"]
    except Exception:
        logger.debug("latest completion context lookup failed", exc_info=True)
    latest_fallback = latest_context_meta.get("fallback_reason")
    if not isinstance(latest_fallback, str):
        latest_fallback = _summary_str(summary, "last_fallback_reason")
    summary_target_tokens = await _setting_int(
        db, "context.summary_target_tokens", SUMMARY_TARGET_DEFAULT_TOKENS
    )
    compressible_messages_count = 0
    compressible_tokens = 0
    try:
        min_recent_messages = await _setting_int(
            db,
            "context.summary_min_recent_messages",
            SUMMARY_MIN_RECENT_DEFAULT_MESSAGES,
        )
        source_messages, _first_user = _compaction_source_messages(
            list(reversed(scanned_messages_desc)),
            min_recent_messages=min_recent_messages,
        )
        compressible_messages_count = len(source_messages)
        compressible_tokens = _estimate_messages_tokens(source_messages)
    except Exception:
        logger.debug("context compressible estimate failed", exc_info=True)
    effective_summary_cost = (
        summary_block_tokens + sticky_tokens
        if summary_available
        else summary_target_tokens
    )
    estimated_tokens_freed = max(0, compressible_tokens - effective_summary_cost)
    manual_min_input_tokens = await _setting_int(
        db,
        "context.manual_compact_min_input_tokens",
        MANUAL_COMPACT_DEFAULT_MIN_INPUT_TOKENS,
    )
    manual_cooldown_seconds = await _setting_int(
        db,
        "context.manual_compact_cooldown_seconds",
        MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS,
    )
    manual_compact_available = used_tokens >= manual_min_input_tokens
    manual_compact_reset_seconds = 0
    manual_compact_unavailable_reason = (
        None if manual_compact_available else "below_min_tokens"
    )
    if manual_compact_available and redis is not None:
        circuit_retry_after = await _circuit_breaker_retry_after(redis)
        if circuit_retry_after is not None:
            manual_compact_available = False
            manual_compact_reset_seconds = circuit_retry_after
            manual_compact_unavailable_reason = "circuit_open"
        else:
            (
                manual_compact_available,
                _remaining,
                manual_compact_reset_seconds,
            ) = await _manual_compact_limit_status(
                redis,
                user_id=user_id,
                conv_id=conv.id,
                cooldown_seconds=manual_cooldown_seconds,
            )
            if not manual_compact_available:
                manual_compact_unavailable_reason = "cooldown"
    return ConversationContextOut(
        input_budget_tokens=CONTEXT_INPUT_TOKEN_BUDGET,
        total_target_tokens=CONTEXT_TOTAL_TOKEN_TARGET,
        response_reserve_tokens=CONTEXT_RESPONSE_TOKEN_RESERVE,
        estimated_input_tokens=used_tokens,
        estimated_history_tokens=history_tokens,
        estimated_system_tokens=system_tokens,
        included_messages_count=included_messages_count,
        truncated=truncated,
        percent=percent,
        compression_enabled=compression_enabled,
        summary_available=summary_available,
        summary_tokens=summary_tokens,
        summary_up_to_message_id=_summary_str(summary, "up_to_message_id"),
        summary_updated_at=_summary_updated_at(summary),
        summary_first_user_message_id=_summary_str(summary, "first_user_message_id"),
        summary_compression_runs=_summary_int(summary, "compression_runs"),
        compressible_messages_count=compressible_messages_count,
        compressible_tokens=compressible_tokens,
        estimated_tokens_freed=estimated_tokens_freed,
        summary_target_tokens=summary_target_tokens,
        compressed=bool(latest_context_meta.get("summary_used")),
        last_fallback_reason=latest_fallback,
        manual_compact_available=manual_compact_available,
        manual_compact_reset_seconds=manual_compact_reset_seconds,
        manual_compact_min_input_tokens=manual_min_input_tokens,
        manual_compact_cooldown_seconds=manual_cooldown_seconds,
        manual_compact_unavailable_reason=manual_compact_unavailable_reason,
    )


def _image_to_out(img: Image, variant_kinds: set[str] | None = None) -> ImageOut:
    # 相对同源路径，由前端 /api 反代到后端 /images/{id}/binary。
    url = f"/api/images/{img.id}/binary"
    variant_kinds = variant_kinds or set()
    # 直接构造：ImageOut.url 是 required 但 Image model 无 url 列，不能用 model_validate
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=url,
        display_url=f"/api/images/{img.id}/variants/display2048",
        preview_url=(
            f"/api/images/{img.id}/variants/preview1024"
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            f"/api/images/{img.id}/variants/thumb256"
            if "thumb256" in variant_kinds
            else None
        ),
        metadata_jsonb=img.metadata_jsonb or {},
    )


@router.get("/{conv_id}/messages", response_model=MessageListOut)
async def list_messages(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    since: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    include: str | None = Query(
        default=None,
        description='逗号分隔；含 "tasks" 时附带返回 generations/completions/images',
    ),
) -> MessageListOut:
    await _get_owned_conv(db, conv_id, user.id)
    alive_filters = _message_alive_filters()
    stmt = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conv_id,
            Conversation.id == conv_id,
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
            *alive_filters,
        )
    )

    # `since` accepts ISO8601 timestamp or a message_id.
    if since:
        parsed_dt: datetime | None = None
        try:
            parsed_dt = datetime.fromisoformat(since)
        except ValueError:
            parsed_dt = None
        if parsed_dt is not None:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
            stmt = stmt.where(Message.created_at > parsed_dt)
        else:
            ref = (
                await db.execute(
                    select(Message.created_at).where(
                        Message.id == since,
                        Message.conversation_id == conv_id,
                        Message.conversation_id.in_(
                            select(Conversation.id).where(
                                Conversation.id == conv_id,
                                Conversation.user_id == user.id,
                                Conversation.deleted_at.is_(None),
                            )
                        ),
                        *alive_filters,
                    )
                )
            ).scalar_one_or_none()
            if ref is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": {
                            "code": "invalid_since",
                            "message": "since must be an ISO8601 timestamp or a message id in this conversation",
                        }
                    },
                )
            stmt = stmt.where(
                or_(
                    Message.created_at > ref,
                    and_(Message.created_at == ref, Message.id > since),
                )
            )

    # `cursor` → backward pagination (older pages). The first page must be the
    # latest messages so a refresh can restore in-flight assistant tasks.
    cur = _dec_cursor(cursor)
    uses_desc_order = False
    if cur and "ca" in cur and "id" in cur:
        ca = _coerce_aware(datetime.fromisoformat(cur["ca"]))
        stmt = stmt.where(
            or_(
                Message.created_at < ca,
                and_(Message.created_at == ca, Message.id < cur["id"]),
            )
        ).order_by(desc(Message.created_at), desc(Message.id))
        uses_desc_order = True
    elif since:
        stmt = stmt.order_by(Message.created_at.asc(), Message.id.asc())
    else:
        stmt = stmt.order_by(desc(Message.created_at), desc(Message.id))
        uses_desc_order = True

    stmt = stmt.limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    if uses_desc_order:
        items = list(reversed(items))
    next_cursor = None
    if has_more and items:
        last = items[0] if uses_desc_order else items[-1]
        next_cursor = _enc_cursor(
            {"ca": last.created_at.isoformat(), "id": last.id}
        )
    out = MessageListOut(
        items=[MessageOut.model_validate(m) for m in items],
        next_cursor=next_cursor,
    )

    # ---- 可选：批量附带 generations/completions/images（用于刷新后恢复历史）----
    include_set = {p.strip() for p in (include or "").split(",") if p.strip()}
    if "tasks" in include_set and items:
        msg_ids = [m.id for m in items]
        gens = (
            await db.execute(
                select(Generation)
                .join(Message, Message.id == Generation.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Generation.message_id.in_(msg_ids),
                    Generation.user_id == user.id,
                    Conversation.id == conv_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(desc(Generation.created_at), desc(Generation.id))
                .limit(TASK_INCLUDE_LIMIT)
            )
        ).scalars().all()
        comps = (
            await db.execute(
                select(Completion)
                .join(Message, Message.id == Completion.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Completion.message_id.in_(msg_ids),
                    Completion.user_id == user.id,
                    Conversation.id == conv_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(desc(Completion.created_at), desc(Completion.id))
                .limit(TASK_INCLUDE_LIMIT)
            )
        ).scalars().all()
        # 收集所有相关 image id：generation 产出的图（owner_generation_id 反查）
        # + completion tool 产出的图（assistant.content.images）+ user 消息附件里的图
        gen_ids = [g.id for g in gens]
        attachment_image_ids: set[str] = set()
        assistant_image_ids: set[str] = set()
        for m in items:
            if m.role == "user":
                for a in (m.content or {}).get("attachments") or []:
                    iid = a.get("image_id") if isinstance(a, dict) else None
                    if isinstance(iid, str):
                        attachment_image_ids.add(iid)
            elif m.role == "assistant":
                for image_ref in (m.content or {}).get("images") or []:
                    iid = (
                        image_ref.get("image_id")
                        if isinstance(image_ref, dict)
                        else None
                    )
                    if isinstance(iid, str):
                        assistant_image_ids.add(iid)
        imgs: list[Image] = []
        image_ids = attachment_image_ids | assistant_image_ids
        if gen_ids or image_ids:
            stmt_img = select(Image).where(
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
            if gen_ids and image_ids:
                stmt_img = stmt_img.where(
                    or_(
                        Image.owner_generation_id.in_(gen_ids),
                        Image.id.in_(image_ids),
                    )
                )
            elif gen_ids:
                stmt_img = stmt_img.where(Image.owner_generation_id.in_(gen_ids))
            else:
                stmt_img = stmt_img.where(Image.id.in_(image_ids))
            stmt_img = stmt_img.order_by(desc(Image.created_at), desc(Image.id)).limit(
                TASK_INCLUDE_LIMIT
            )
            imgs = list((await db.execute(stmt_img)).scalars().all())

        variant_map: dict[str, set[str]] = {}
        if imgs:
            variant_rows = (
                await db.execute(
                    select(ImageVariant.image_id, ImageVariant.kind).where(
                        ImageVariant.image_id.in_([i.id for i in imgs])
                    )
                )
            ).all()
            for image_id, kind in variant_rows:
                variant_map.setdefault(image_id, set()).add(kind)

        out.generations = [GenerationOut.model_validate(g) for g in gens]
        out.completions = [CompletionOut.model_validate(c) for c in comps]
        out.images = [_image_to_out(i, variant_map.get(i.id)) for i in imgs]

    return out


@router.get("/{conv_id}/context", response_model=ConversationContextOut)
async def get_conversation_context(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationContextOut:
    conv = await _get_owned_conv(db, conv_id, user.id)
    return await _estimate_context_window(
        db,
        conv=conv,
        user_id=user.id,
        user_default_prompt_id=user.default_system_prompt_id,
        redis=get_redis(),
    )


def _manual_compact_idempotency_key(
    *, user_id: str, conv_id: str, raw_key: str
) -> str:
    digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    return f"context:manual_compact:idemp:{user_id}:{conv_id}:{digest}"


def _manual_compact_job_id(
    *,
    user_id: str,
    conv_id: str,
    boundary_id: str,
    extra_instruction: str | None,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
) -> str:
    raw = json.dumps(
        {
            "user_id": user_id,
            "conv_id": conv_id,
            "boundary_id": boundary_id,
            "extra_instruction": extra_instruction or "",
            "target_tokens": target_tokens,
            "input_budget": input_budget,
            "summary_timeout_s": summary_timeout_s,
            "model": model,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _manual_compact_job_key(
    *, user_id: str, conv_id: str, job_id: str
) -> str:
    return f"context:manual_compact:job:{user_id}:{conv_id}:{job_id}"


def _manual_compact_active_key(*, user_id: str, conv_id: str) -> str:
    return f"context:manual_compact:active:{user_id}:{conv_id}"


async def _redis_get_json(redis: Any, key: str) -> dict[str, Any] | None:
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


async def _redis_set_json(redis: Any, key: str, value: dict[str, Any], ttl: int) -> None:
    raw = json.dumps(value, separators=(",", ":"), default=str)
    setter = getattr(redis, "set", None)
    if setter is not None:
        await setter(key, raw, ex=ttl)
        return
    await redis.setex(key, ttl, raw)


def _compact_pending_payload(
    *,
    job_id: str,
    status: str = "pending",
    retry_after_seconds: int = MANUAL_COMPACT_RETRY_AFTER_SECONDS,
) -> dict[str, Any]:
    return {
        "status": status,
        "compacted": False,
        "reason": "pending",
        "job_id": job_id,
        "retry_after_seconds": retry_after_seconds,
    }


def _compact_payload_from_job(
    job: dict[str, Any] | None,
    *,
    job_id: str,
) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    status = str(job.get("status") or "")
    if status == "succeeded":
        response = job.get("response")
        if isinstance(response, dict):
            return response
        return None
    if status in {"queued", "running"}:
        return _compact_pending_payload(job_id=job_id, status="pending")
    if status == "failed":
        return {
            "status": "failed",
            "compacted": False,
            "reason": job.get("reason") or "upstream_error",
            "job_id": job_id,
        }
    return None


async def _redis_set_nx_json(
    redis: Any,
    key: str,
    value: dict[str, Any],
    ttl: int,
) -> bool:
    raw = json.dumps(value, separators=(",", ":"), default=str)
    setter = getattr(redis, "set", None)
    if setter is None:
        return False
    return bool(await setter(key, raw, ex=ttl, nx=True))


def _validate_compact_body(body: ConversationCompactIn) -> None:
    if body.extra_instruction is not None:
        if len(body.extra_instruction) > MANUAL_COMPACT_EXTRA_INSTRUCTION_MAX_CHARS:
            raise _bad_request(
                "invalid_extra_instruction",
                "extra_instruction exceeds 1000 characters",
            )
    if body.target_tokens is not None and not (
        MANUAL_COMPACT_MIN_TARGET_TOKENS
        <= body.target_tokens
        <= MANUAL_COMPACT_MAX_TARGET_TOKENS
    ):
        raise _bad_request(
            "invalid_target_tokens",
            "target_tokens must be between 300 and 4000",
        )


class ManualCompactIn(BaseModel):
    extra_instruction: str | None = None
    # force=False（默认）会先做 token 预算判断：未超即返回 {"compacted": false}，
    # 不调用上游也不写库；force=True 则跳过判断，直接走客户端层 compact 主流程。
    # 历史调用方（前端"立即压缩"按钮）应显式传 force=True 保留旧行为。
    force: bool = False
    # 客户端层 compact 的 safety_margin：留给即将追加的用户输入与 reasoning。
    # None 时回退到 would_exceed_budget 的默认 4096，避免硬编码到路由层。
    safety_margin: int | None = None
    # background=True 让前端按钮走 worker 后台任务，避免浏览器长连接超时、
    # 用户刷新页面或 React 重渲染导致 API 请求 499 后误判为失败。
    background: bool = False


def _classify_compact_failure(result: dict[str, Any] | None) -> str:
    """Map ensure_context_summary outcomes to a compact-API failure reason."""
    if not isinstance(result, dict):
        return "lock_busy"
    status = str(result.get("status") or "")
    if status in {"circuit_open", "circuit_breaker"}:
        return "circuit_open"
    if status in {"failed", "summary_failed", "cas_failed", "upstream_error"}:
        return "upstream_error"
    if status in {"lock_busy", "lock_wait_timeout"}:
        return "lock_busy"
    return "upstream_error"


def _build_compact_summary_payload(
    *,
    result: dict[str, Any],
    conv: Conversation,
) -> dict[str, Any]:
    """Translate ensure_context_summary metadata into the public compact payload.

    Pulls compressed_at from the persisted summary because ensure_context_summary
    only returns it indirectly (through summary_jsonb) for the freshly created or
    cached entries.
    """
    summary_jsonb = (
        conv.summary_jsonb if isinstance(getattr(conv, "summary_jsonb", None), dict) else {}
    )
    compressed_at = summary_jsonb.get("compressed_at") if isinstance(summary_jsonb, dict) else None
    summary_tokens = int(result.get("summary_tokens") or 0)
    source_token_estimate = int(result.get("source_token_estimate") or 0)
    tokens_freed = int(
        result.get("tokens_freed")
        if result.get("tokens_freed") is not None
        else max(0, source_token_estimate - summary_tokens)
    )
    return {
        "summary_created": bool(result.get("summary_created")),
        "summary_used": bool(result.get("summary_used", True)),
        "summary_up_to_message_id": result.get("summary_up_to_message_id"),
        "summary_up_to_created_at": result.get("summary_up_to_created_at"),
        "tokens": summary_tokens,
        "source_message_count": int(result.get("source_message_count") or 0),
        "source_token_estimate": source_token_estimate,
        "image_caption_count": int(result.get("image_caption_count") or 0),
        "tokens_freed": tokens_freed,
        "fallback_reason": result.get("fallback_reason"),
        "compressed_at": compressed_at,
        "status": result.get("status"),
    }


async def _enqueue_manual_compact_job(
    *,
    user_id: str,
    conv_id: str,
    boundary_id: str,
    extra_instruction: str | None,
    target_tokens: int,
    input_budget: int,
    summary_timeout_s: float,
    model: str,
    redis: Any,
    cooldown_seconds: int,
) -> dict[str, Any]:
    job_id = _manual_compact_job_id(
        user_id=user_id,
        conv_id=conv_id,
        boundary_id=boundary_id,
        extra_instruction=extra_instruction,
        target_tokens=target_tokens,
        input_budget=input_budget,
        summary_timeout_s=summary_timeout_s,
        model=model,
    )
    job_key = _manual_compact_job_key(
        user_id=user_id,
        conv_id=conv_id,
        job_id=job_id,
    )
    active_key = _manual_compact_active_key(user_id=user_id, conv_id=conv_id)
    cooldown_key = _manual_compact_cooldown_key(user_id=user_id, conv_id=conv_id)

    try:
        existing = await _redis_get_json(redis, job_key)
    except Exception:
        logger.warning("manual compact job status read failed", exc_info=True)
        existing = None
    payload = _compact_payload_from_job(existing, job_id=job_id)
    if payload is not None:
        return payload

    try:
        active = await _redis_get_json(redis, active_key)
    except Exception:
        active = None
    active_job_id = active.get("job_id") if isinstance(active, dict) else None
    if isinstance(active_job_id, str) and active_job_id:
        return _compact_pending_payload(job_id=active_job_id, status="pending")

    active_payload = {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        locked = await _redis_set_nx_json(
            redis,
            active_key,
            active_payload,
            MANUAL_COMPACT_ACTIVE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.exception("manual compact active lock failed")
        raise _service_unavailable("upstream_error") from exc
    if not locked:
        try:
            active = await _redis_get_json(redis, active_key)
        except Exception:
            active = None
        active_job_id = (
            active.get("job_id") if isinstance(active, dict) else None
        )
        if isinstance(active_job_id, str) and active_job_id:
            return _compact_pending_payload(job_id=active_job_id, status="pending")
        return _compact_pending_payload(job_id=job_id, status="pending")

    try:
        await _check_manual_compact_cooldown(
            redis,
            user_id=user_id,
            conv_id=conv_id,
            cooldown_seconds=cooldown_seconds,
        )
    except HTTPException:
        try:
            await redis.delete(active_key)
        except Exception:
            logger.debug("manual compact active cleanup after cooldown failed", exc_info=True)
        raise

    now = datetime.now(timezone.utc).isoformat()
    job_payload = {
        "status": "queued",
        "job_id": job_id,
        "user_id": user_id,
        "conv_id": conv_id,
        "boundary_id": boundary_id,
        "created_at": now,
        "updated_at": now,
    }
    try:
        await _redis_set_json(redis, job_key, job_payload, MANUAL_COMPACT_JOB_TTL_SECONDS)
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "manual_compact_conversation",
            user_id,
            conv_id,
            boundary_id,
            job_id,
            extra_instruction,
            target_tokens,
            input_budget,
            summary_timeout_s,
            model,
        )
    except Exception as exc:
        logger.exception("manual compact enqueue failed")
        try:
            await redis.delete(active_key)
            await redis.delete(job_key)
            await redis.delete(cooldown_key)
        except Exception:
            logger.debug("manual compact enqueue cleanup failed", exc_info=True)
        raise _service_unavailable("upstream_error") from exc

    return _compact_pending_payload(job_id=job_id, status="pending")


def _import_ensure_context_summary() -> Any | None:
    module = _import_worker_context_summary()
    if module is None:
        return None
    return getattr(module, "ensure_context_summary", None)


@router.post(
    "/{conv_id}/compact",
    dependencies=[Depends(verify_csrf)],
)
async def compact_conversation(
    conv_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ManualCompactIn | None = None,
) -> dict[str, Any]:
    """Manually compact a conversation's history.

    Why: gives the user an escape hatch when auto-compaction has not fired yet
    but the context window is already feeling full. Boundary is the latest live
    message; ensure_context_summary owns the cooldown / lock / circuit logic.
    """
    body = body or ManualCompactIn()
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if conv is None:
        raise _not_found()

    boundary = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == conv.id,
                *_message_alive_filters(),
                Message.role.in_(("user", "assistant")),
            )
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    if boundary is None:
        raise HTTPException(status_code=409, detail="no messages to compact")

    # ---- 客户端层 compact 的预算门槛 ----
    # force=False 时先估算历史 token：没超预算就直接返回 compacted=false，不打上游。
    # 这样避免短对话也跑一次昂贵的 /v1/responses 摘要调用。force=True 跳过判断。
    if not body.force:
        all_msgs = await _load_messages_for_compaction(db, conv.id)
        # system_prompt 拼接遵循 _estimate_context_window 的口径，确保门槛一致。
        conversation_prompt = await _load_prompt_content(
            db, user_id=user.id, prompt_id=conv.default_system_prompt_id
        )
        global_prompt = await _load_prompt_content(
            db, user_id=user.id, prompt_id=user.default_system_prompt_id
        )
        system_prompt = _simple_structured_system_prompt(
            global_prompt=global_prompt,
            conversation_prompt=conversation_prompt,
            legacy_conversation_prompt=conv.default_system,
        ) or ""
        safety_margin = (
            body.safety_margin if body.safety_margin is not None else 4096
        )
        used_tokens = messages_token_count(all_msgs, system_prompt=system_prompt)
        if not would_exceed_budget(
            all_msgs,
            system_prompt=system_prompt,
            budget=CONTEXT_INPUT_TOKEN_BUDGET,
            safety_margin=safety_margin,
        ):
            return {
                "status": "ok",
                "compacted": False,
                "reason": "below_budget",
                "estimated_input_tokens": used_tokens,
                "input_budget_tokens": CONTEXT_INPUT_TOKEN_BUDGET,
                "safety_margin": safety_margin,
            }

    redis = get_redis()
    target_tokens = await _setting_int(
        db, "context.summary_target_tokens", SUMMARY_TARGET_DEFAULT_TOKENS
    )
    input_budget = await _setting_int(db, "context.summary_input_budget", 80_000)
    summary_timeout_s = await _setting_float(db, "context.summary_http_timeout_s", 120.0)
    model = await _setting_str(db, "context.summary_model", SUMMARY_MODEL_DEFAULT)
    manual_cooldown_seconds = await _setting_int(
        db,
        "context.manual_compact_cooldown_seconds",
        MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS,
    )

    circuit_retry_after = await _circuit_breaker_retry_after(redis)
    if circuit_retry_after is not None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "compression_unavailable",
                    "message": "compression unavailable",
                    "reason": "circuit_open",
                    "details": {"reason": "circuit_open"},
                }
            },
            headers={"Retry-After": str(max(1, circuit_retry_after))},
        )

    if body.background:
        return await _enqueue_manual_compact_job(
            user_id=user.id,
            conv_id=conv.id,
            boundary_id=boundary.id,
            extra_instruction=body.extra_instruction,
            target_tokens=target_tokens,
            input_budget=input_budget,
            summary_timeout_s=summary_timeout_s,
            model=model,
            redis=redis,
            cooldown_seconds=manual_cooldown_seconds,
        )

    await _check_manual_compact_cooldown(
        redis,
        user_id=user.id,
        conv_id=conv.id,
        cooldown_seconds=manual_cooldown_seconds,
    )

    ensure = _import_ensure_context_summary()
    if ensure is None:
        logger.error("ensure_context_summary unavailable for manual compact")
        raise _service_unavailable("upstream_error")

    runtime_settings: dict[str, Any] = {
        "context.summary_target_tokens": target_tokens,
        "context.summary_input_budget": input_budget,
        "context.summary_http_timeout_s": summary_timeout_s,
        "context.summary_model": model,
        "redis": redis,
    }

    try:
        result = await ensure(
            db,
            conv,
            boundary,
            runtime_settings,
            force=True,
            extra_instruction=body.extra_instruction,
            trigger="manual",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surfaced as 503
        logger.exception("manual context compaction failed")
        raise _service_unavailable("upstream_error") from exc

    if result is None or not isinstance(result, dict) or "failed" in str(result.get("status") or ""):
        reason = _classify_compact_failure(result)
        raise _service_unavailable(reason)

    summary_payload = _build_compact_summary_payload(result=result, conv=conv)
    return {"status": "ok", "compacted": True, "summary": summary_payload}


@router.get("/{conv_id}/compact/status")
async def get_compact_conversation_status(
    conv_id: str,
    job_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    await _get_owned_conv(db, conv_id, user.id)
    redis = get_redis()
    job_key = _manual_compact_job_key(
        user_id=user.id,
        conv_id=conv_id,
        job_id=job_id,
    )
    try:
        job = await _redis_get_json(redis, job_key)
    except Exception as exc:
        logger.warning("manual compact status unavailable", exc_info=True)
        raise _service_unavailable("upstream_error") from exc
    payload = _compact_payload_from_job(job, job_id=job_id)
    if payload is None:
        raise _not_found()
    if payload.get("status") == "failed":
        reason = str(payload.get("reason") or "upstream_error")
        raise _service_unavailable(reason)
    return payload
