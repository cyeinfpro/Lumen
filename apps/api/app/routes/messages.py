"""Messages 路由（DESIGN §5.4 — 核心写入接口）。

POST /conversations/{conv_id}/messages
1. 鉴权 + rate limit
2. 意图路由（auto → chat / vision_qa / text_to_image / image_to_image）
3. 出图参数校验 + 尺寸解析（lumen_core.sizing.resolve_size）
4. 幂等：(user, idempotency_key) 命中 → 直接返回既有三件套
5. 单事务：INSERT messages(user) + messages(assistant, pending) + 子任务 + outbox_events
6. 事务提交后尽力 XADD queue + PUBLISH task.queued + XADD events:user:{uid}
7. 返回 PostMessageOut
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL_FAST,
    EV_COMP_QUEUED,
    EV_CONV_MSG_APPENDED,
    EV_GEN_QUEUED,
    CompletionStage,
    CompletionStatus,
    GenerationAction,
    GenerationStage,
    GenerationStatus,
    IMAGE_MULTI_GEN_STAGGER_CAP_S,
    IMAGE_MULTI_GEN_STAGGER_S,
    MAX_MESSAGE_ATTACHMENTS,
    Intent,
    MAX_PROMPT_CHARS,
    MessageStatus,
    Role,
    conv_channel,
    task_channel,
)
from lumen_core.memory import (
    canonical_memory_text,
    extract_memories,
)
from lumen_core.models import (
    ApiSupplierTemplate,
    Completion,
    Conversation,
    Generation,
    Image,
    MemoryAudit,
    Message,
    OutboxEvent,
    SystemPrompt,
    User,
    UserApiCredential,
    UserMemory,
    UserMemoryScope,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    MessageOut,
    PostMessageIn,
    PostMessageOut,
)
from lumen_core.sizing import ResolvedSize, resolve_size
from lumen_core import billing as billing_core

from ..arq_pool import get_arq_pool
from ..audit import hash_email, write_audit
# Why: read_byok_settings is re-exported here so existing tests that
# monkeypatch `messages.read_byok_settings` keep working. Production code on
# this path uses read_byok_settings_cached (TTL ~30 s) — see review #20.
from ..byok_service import read_byok_settings, read_byok_settings_cached  # noqa: F401
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..intent import resolve_intent
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..runtime_settings import embedding_provider_available, get_setting
from ..sse_publish import publish_sse_event, publish_sse_events


router = APIRouter()

logger = logging.getLogger(__name__)


async def _lock_idempotency_key(
    db: AsyncSession,
    user_id: str,
    idempotency_key: str,
) -> None:
    connection = getattr(db, "connection", None)
    if connection is None:
        return
    bind = await connection()
    if bind.dialect.name != "postgresql":
        return
    lock_key = f"{user_id}:{idempotency_key}"
    # Why hashtext is OK here: pg_advisory_xact_lock takes a 64-bit signed int
    # and hashtext returns a 32-bit signed int — i.e. there is collision risk
    # across distinct (user_id, idempotency_key) pairs. Collisions only cost
    # serialization (one extra waiter blocks until the txn commits) and never
    # corrupt; the race we actually guard against is duplicate inserts of the
    # same key, which still hash identically. So 32-bit hash collisions only
    # slow, never corrupt.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))

# Why: align with the global ``MAX_PROMPT_CHARS`` so server-side truncation
# matches the validation cap exposed to clients. Previously this was a local
# 4096 constant while ``MAX_PROMPT_CHARS`` was 4000 elsewhere, allowing
# inconsistent truncation between layers.
SYSTEM_PROMPT_SOURCE_LIMIT = MAX_PROMPT_CHARS
ALLOWED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_VECTOR_STORE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_IMAGE_RENDER_QUALITY_VALUES = {"low", "medium", "high"}
_IMAGE_OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
_DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"
_GENERATION_FAST_DEFAULT_KEY = "generation.fast_default"
_IMAGE_BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
_IMAGE_MODERATION_VALUES = {"auto", "low"}
_IMAGE_BILLING_TIER_VALUES = {"1k", "2k", "4k"}
_POST_COMMIT_PUBLISH_TIMEOUT_S = 2.0
_CONFIRM_REPLY_YES_RE = re.compile(
    r"^\s*(对|是|嗯|可以|继续|好|yes|yep|yeah|ok|okay)\b|按.*来",
    re.IGNORECASE,
)
# Why anchor: 中文 \b 不准, 不锚定开头会让"我打算继续按这个不变"里的"不"被匹中.
# 正确含义是用户答复以否定开头.
_CONFIRM_REPLY_NO_RE = re.compile(
    r"^\s*(不是|不要|不用|不按|换一?[下个]?|别|no|nope|don'?t|do not)",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_RE = re.compile(
    r"透明(?:底|背景|底色)|去背|抠图|免抠|无背景|"
    r"transparent\s+(?:background|bg)|background\s+transparent|"
    r"(?:no|without)\s+(?:a\s+)?background|cutout|isolated\s+subject",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_NEGATIVE_RE = re.compile(
    r"不(?:要|需要|用)?透明(?:底|背景|底色)?|非透明|opaque\s+background|"
    r"no\s+transparent\s+(?:background|bg)",
    re.IGNORECASE,
)
# 去除 C0 控制字符（\x00-\x1f）+ DEL（\x7f），但保留 \t (9) / \n (10) / \r (13)
# 以允许多行 prompt 的正常换行。prompt-injection 防御的目标是阻止像 \x1b 这种
# 终端转义、\x00 空字节注入，而不是把用户合法的换行也搞丢。
_SYSTEM_PROMPT_CONTROL_TRANSLATION = {
    i: " " for i in range(32) if i not in (9, 10, 13)
}
_SYSTEM_PROMPT_CONTROL_TRANSLATION[127] = " "


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


async def _billing_setting_raw(db: AsyncSession, key: str) -> str | None:
    spec = get_spec(key)
    if spec is None:
        return None
    try:
        return await get_setting(db, spec)
    except (AssertionError, IndexError):
        if key.startswith("billing."):
            return None
        raise


async def _billing_enabled(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _billing_setting_raw(db, "billing.enabled"),
        False,
    )


async def _audit_billing_gap(
    db: AsyncSession,
    *,
    event_type: str,
    user: User,
    details: dict[str, Any],
) -> None:
    await write_audit(
        db,
        event_type=event_type,
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        details=details,
        autocommit=False,
    )


async def _billing_allow_negative(db: AsyncSession) -> bool:
    return billing_core.parse_bool_setting(
        await _billing_setting_raw(db, "billing.allow_negative_balance"),
        False,
    )


async def _billing_image_thresholds(db: AsyncSession) -> dict[str, int]:
    return billing_core.parse_thresholds(
        await _billing_setting_raw(db, "billing.image_size_thresholds")
    )


def _billing_http_error(exc: billing_core.BillingError) -> HTTPException:
    return _http(exc.code, exc.message, exc.status_code)


def _requested_image_billing_tier(image_params: ImageParamsIn) -> str | None:
    return (
        image_params.quality
        if image_params.quality in _IMAGE_BILLING_TIER_VALUES
        else None
    )


def _resolve_image_render_quality(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
) -> str:
    _ = resolved_size
    if image_params.render_quality in _IMAGE_RENDER_QUALITY_VALUES:
        return image_params.render_quality
    return "medium"


def _default_output_compression(
    *,
    render_quality: str,
    fast: bool,
) -> int:
    return 0


async def _resolve_fast_default(db: AsyncSession) -> bool:
    spec = get_spec(_GENERATION_FAST_DEFAULT_KEY)
    if spec is None:
        return True
    raw = await get_setting(db, spec)
    if raw in {"0", "1"}:
        return raw == "1"
    return True


def _image_params_with_fast_default(
    image_params: ImageParamsIn,
    fast_default: bool,
) -> ImageParamsIn:
    if image_params.fast is not None:
        return image_params
    return image_params.model_copy(update={"fast": fast_default})


def _chat_params_with_fast_default(
    chat_params: ChatParamsIn,
    fast_default: bool,
) -> ChatParamsIn:
    if chat_params.fast is not None:
        return chat_params
    return chat_params.model_copy(update={"fast": fast_default})


def _wants_transparent_background(prompt: str | None) -> bool:
    if not prompt:
        return False
    return bool(_TRANSPARENT_BACKGROUND_RE.search(prompt)) and not bool(
        _TRANSPARENT_BACKGROUND_NEGATIVE_RE.search(prompt)
    )


def _resolve_image_background(image_params: ImageParamsIn, prompt: str | None) -> str:
    background = (
        image_params.background
        if image_params.background in _IMAGE_BACKGROUND_VALUES
        else "auto"
    )
    if background == "auto" and _wants_transparent_background(prompt):
        return "transparent"
    return background


def _transparent_background_prompt_suffix() -> str:
    return (
        "\n\nRender the subject as a clean cutout on a true transparent alpha "
        "background. Do not paint a white, gray, checkerboard, wall, floor, or "
        "studio backdrop."
    )


def _image_upstream_request(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
    *,
    prompt: str | None = None,
    default_output_format: str = _DEFAULT_IMAGE_OUTPUT_FORMAT,
) -> dict[str, Any]:
    render_quality = _resolve_image_render_quality(image_params, resolved_size)
    background = _resolve_image_background(image_params, prompt)
    output_format_is_explicit = image_params.output_format in _IMAGE_OUTPUT_FORMAT_VALUES
    output_format = (
        image_params.output_format
        if output_format_is_explicit
        else default_output_format
        if default_output_format in _IMAGE_OUTPUT_FORMAT_VALUES
        else _DEFAULT_IMAGE_OUTPUT_FORMAT
    )
    output_format_source = "request" if output_format_is_explicit else "system_default"
    if background == "transparent":
        output_format = "png"
        output_format_source = "transparent_background"
    upstream_request: dict[str, Any] = {
        "fast": bool(image_params.fast),
        "responses_model": (
            DEFAULT_IMAGE_RESPONSES_MODEL_FAST
            if image_params.fast
            else DEFAULT_IMAGE_RESPONSES_MODEL
        ),
        "render_quality": render_quality,
        "output_format": output_format,
        "output_format_source": output_format_source,
        "background": background,
        "moderation": (
            image_params.moderation
            if image_params.moderation in _IMAGE_MODERATION_VALUES
            else "low"
        ),
    }
    billing_tier = _requested_image_billing_tier(image_params)
    if billing_tier is not None:
        upstream_request["billing_tier"] = billing_tier
        upstream_request["billing_tier_source"] = "request_quality"
    if output_format in {"jpeg", "webp"}:
        upstream_request["output_compression"] = (
            _default_output_compression(
                render_quality=render_quality,
                fast=bool(image_params.fast),
            )
            if image_params.output_compression is None
            else image_params.output_compression
        )
    return upstream_request


def _chat_upstream_request(chat_params: ChatParamsIn) -> dict[str, Any] | None:
    req: dict[str, Any] = {}
    if chat_params.web_search:
        req["web_search"] = True
    if chat_params.file_search:
        vector_store_ids: list[str] = []
        seen: set[str] = set()
        for raw in chat_params.vector_store_ids:
            value = raw.strip()
            if not value:
                continue
            if not _VECTOR_STORE_ID_RE.fullmatch(value):
                raise _http(
                    "invalid_vector_store_id",
                    "invalid vector_store_ids entry",
                    422,
                )
            if value not in seen:
                seen.add(value)
                vector_store_ids.append(value)
        req["file_search"] = True
        if vector_store_ids:
            req["vector_store_ids"] = vector_store_ids
    if chat_params.code_interpreter:
        req["code_interpreter"] = True
    if chat_params.image_generation:
        req["image_generation"] = True
    return req or None


def _non_blank(text: str | None) -> str | None:
    if text is None:
        return None
    return text if text.strip() else None


def _sanitize_system_prompt_source(text: str | None) -> str | None:
    """NFKC normalize + 去除控制字符 + 长度截断到 SYSTEM_PROMPT_SOURCE_LIMIT。

    选 NFKC 而非 NFC 是有意的：NFKC 会把全角数字/同形异码字符统一成 ASCII 规范
    形式，更能对抗 prompt-injection 里故意用 Unicode 混淆分隔符的场景（例如用
    U+FF3B 『[』 伪造 [SYSTEM_GLOBAL] 标签）。对 system prompt 这类受控文本的
    轻微语义改写是可接受代价。
    """
    prompt = _non_blank(text)
    if prompt is None:
        return None
    normalized = unicodedata.normalize("NFKC", prompt)
    cleaned = normalized.translate(_SYSTEM_PROMPT_CONTROL_TRANSLATION).strip()
    if not cleaned:
        return None
    if len(cleaned) > SYSTEM_PROMPT_SOURCE_LIMIT:
        # 截断是 prompt-injection 防御的一部分：上限太高上游也会拒，放任不管
        # 会让"超长覆盖全局规则"成为攻击面。log 出原长度方便事后审计。
        logger.warning(
            "system prompt source truncated: original_len=%d limit=%d",
            len(cleaned),
            SYSTEM_PROMPT_SOURCE_LIMIT,
        )
        cleaned = cleaned[:SYSTEM_PROMPT_SOURCE_LIMIT]
    return cleaned


def choose_system_prompt(
    *,
    explicit_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
    global_prompt: str | None,
) -> str | None:
    for candidate in (
        explicit_prompt,
        conversation_prompt,
        legacy_conversation_prompt,
        global_prompt,
    ):
        prompt = _sanitize_system_prompt_source(candidate)
        if prompt is not None:
            return prompt
    return None


def build_structured_system_prompt(
    *,
    explicit_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
    global_prompt: str | None,
) -> str | None:
    sections: list[str] = []
    for tag, candidate in (
        ("SYSTEM_GLOBAL", global_prompt),
        ("SYSTEM_CONVERSATION_LEGACY", legacy_conversation_prompt),
        ("SYSTEM_CONVERSATION", conversation_prompt),
        ("SYSTEM_EXPLICIT", explicit_prompt),
    ):
        prompt = _sanitize_system_prompt_source(candidate)
        if prompt is not None:
            sections.append(f"[{tag}]\n{prompt}\n[/{tag}]")
    if not sections:
        return None
    return "\n".join(("[SYSTEM_PROMPTS]", *sections, "[/SYSTEM_PROMPTS]"))


def _message_alive_filters() -> tuple[Any, ...]:
    deleted_at = getattr(Message, "deleted_at", None)
    if deleted_at is None:
        return ()
    return (deleted_at.is_(None),)


async def _load_owned_prompt_content(
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


async def resolve_system_prompt_for_message(
    db: AsyncSession,
    *,
    user_id: str,
    default_system_prompt_id: str | None,
    conv: Conversation,
    explicit_prompt: str | None,
) -> str | None:
    conversation_prompt = await _load_owned_prompt_content(
        db, user_id=user_id, prompt_id=conv.default_system_prompt_id
    )
    global_prompt = await _load_owned_prompt_content(
        db, user_id=user_id, prompt_id=default_system_prompt_id
    )
    return build_structured_system_prompt(
        explicit_prompt=explicit_prompt,
        conversation_prompt=conversation_prompt,
        legacy_conversation_prompt=conv.default_system,
        global_prompt=global_prompt,
    )


async def _default_memory_scope(db: AsyncSession, user_id: str) -> UserMemoryScope:
    scope = (
        await db.execute(
            select(UserMemoryScope).where(
                UserMemoryScope.user_id == user_id,
                UserMemoryScope.is_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if scope is not None:
        return scope
    scope = UserMemoryScope(user_id=user_id, name="default", is_default=True)
    db.add(scope)
    await db.flush()
    return scope


async def _enqueue_memory_reembed(target: str, row_id: str) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("memory_reembed", target, row_id)
    except Exception:
        logger.warning(
            "memory_reembed enqueue failed target=%s id=%s",
            target,
            row_id,
            exc_info=True,
        )


async def _memory_undo_token(payload: dict[str, Any]) -> str | None:
    token = secrets.token_urlsafe(24)
    try:
        await get_redis().setex(
            f"memory:undo:{token}",
            300,
            json.dumps(payload, separators=(",", ":")),
        )
        return token
    except Exception:
        return None


async def _disable_memory_for_conversation(conversation_id: str, memory_id: str) -> None:
    try:
        key = f"memory:conversation:{conversation_id}:disabled"
        pipe = get_redis().pipeline(transaction=False)
        pipe.sadd(key, memory_id)
        pipe.expire(key, 30 * 24 * 60 * 60)
        await pipe.execute()
    except Exception:
        return


def _confirmation_reply_decision(text: str) -> Literal["yes", "no", "skip"] | None:
    value = " ".join((text or "").split()).strip()
    if not value:
        return None
    if _CONFIRM_REPLY_NO_RE.search(value):
        return "no"
    if _CONFIRM_REPLY_YES_RE.search(value):
        return "yes"
    return "skip"


async def _apply_pending_confirmation_reply(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    user_msg: Message,
    text: str,
) -> None:
    decision = _confirmation_reply_decision(text)
    if decision is None:
        return
    prompt = (
        await db.execute(
            select(MemoryAudit)
            .join(Message, MemoryAudit.source_message_id == Message.id)
            .where(
                MemoryAudit.user_id == user.id,
                MemoryAudit.event_type == "confirm_prompted",
                Message.conversation_id == conv.id,
            )
            .order_by(desc(MemoryAudit.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if prompt is None or not prompt.memory_id:
        return
    already_answered = (
        await db.execute(
            select(func.count(MemoryAudit.id)).where(
                MemoryAudit.user_id == user.id,
                MemoryAudit.memory_id == prompt.memory_id,
                MemoryAudit.event_type.in_(
                    (
                        "confirm_yes",
                        "confirm_no",
                        "confirm_skip",
                        "confirm_auto_yes",
                        "confirm_auto_no",
                        "confirm_auto_skip",
                    )
                ),
                MemoryAudit.created_at > prompt.created_at,
            )
        )
    ).scalar_one()
    if int(already_answered or 0) > 0:
        return
    memory = await db.get(UserMemory, prompt.memory_id)
    if memory is None or memory.user_id != user.id:
        return
    now = datetime.now(timezone.utc)
    if decision == "yes":
        memory.positive_signal += 1
    elif decision == "no":
        memory.negative_signal += 2
        await _disable_memory_for_conversation(conv.id, memory.id)
    memory.last_confirmed_at = now
    db.add(
        MemoryAudit(
            user_id=user.id,
            memory_id=memory.id,
            event_type=f"confirm_auto_{decision}",
            new_content=memory.content,
            source_message_id=user_msg.id,
            details={"conversation_id": conv.id, "prompt_audit_id": prompt.id},
        )
    )


async def _apply_explicit_memory_write(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    user_msg: Message,
    assistant_msg: Message,
    text: str,
    reembed_ids: list[str] | None = None,
) -> None:
    """Synchronous "remember X" path so the next turn can use it."""
    if (
        bool(getattr(user, "memory_disabled", False))
        or bool(getattr(user, "memory_paused", False))
        or bool(getattr(conv, "memory_disabled", False))
    ):
        return
    # 没 embedding provider 时整条记忆链路都没法跑(检索阶段算 cosine 全 ≈ 0).
    # 直接 short-circuit, 不写库不发 inline 提示, 让用户在 settings 里看到
    # "需要 embedding provider" 的统一提示.
    if not await embedding_provider_available(db):
        return
    write_now = datetime.now(timezone.utc)
    candidates, rejected_pii = extract_memories(text, explicit_only=True)
    explicit_reembed_ids: list[str] = reembed_ids if reembed_ids is not None else []
    writes: list[dict[str, Any]] = []
    if rejected_pii:
        writes.append(
            {
                "id": None,
                "kind": "rejected_pii",
                "type": None,
                "content": "",
                "source_excerpt": " ".join((text or "").split())[:160],
                "undo_token": None,
                "scope_id": None,
                "recommended_scope_id": None,
            }
        )
    if not candidates:
        if writes:
            assistant_msg.content = {**(assistant_msg.content or {}), "memory_writes": writes}
        return

    default_scope = await _default_memory_scope(db, user.id)
    scope = default_scope
    if conv.active_scope_id:
        active_scope = (
            await db.execute(
                select(UserMemoryScope).where(
                    UserMemoryScope.id == conv.active_scope_id,
                    UserMemoryScope.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if active_scope is not None:
            scope = active_scope
    for candidate in candidates:
        existing = (
            await db.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user.id,
                    UserMemory.type == candidate.type,
                    UserMemory.disabled.is_(False),
                    UserMemory.superseded_by.is_(None),
                )
            )
        ).scalars().all()
        duplicate = next(
            (
                m
                for m in existing
                if canonical_memory_text(m.content)
                == canonical_memory_text(candidate.content)
            ),
            None,
        )
        if duplicate is not None:
            duplicate.positive_signal += 1
            duplicate.updated_at = write_now
            db.add(
                MemoryAudit(
                    user_id=user.id,
                    memory_id=duplicate.id,
                    event_type="merged",
                    old_content=duplicate.content,
                    new_content=duplicate.content,
                    source_message_id=user_msg.id,
                    details={"source": "explicit"},
                )
            )
            # Why preserve full candidate: undo "merged" must split it back
            # into an independent entry per design §5.4 ("撤销保留独立"),
            # which means we need every field needed to reconstruct a row.
            token = await _memory_undo_token(
                {
                    "user_id": user.id,
                    "action": "merged",
                    "memory_id": duplicate.id,
                    "candidate": {
                        "type": candidate.type,
                        "content": candidate.content,
                        "source_excerpt": candidate.source_excerpt,
                        "source_message_id": user_msg.id,
                        "scope_id": scope.id,
                        "source": "explicit",
                        "confidence": 1.0,
                    },
                }
            )
            writes.append(
                {
                    "id": duplicate.id,
                    "kind": "merged",
                    "type": duplicate.type,
                    "content": duplicate.content,
                    "source_excerpt": candidate.source_excerpt,
                    "undo_token": token,
                    "scope_id": duplicate.scope_id,
                    "recommended_scope_id": scope.id,
                }
            )
            continue
        memory = UserMemory(
            user_id=user.id,
            type=candidate.type,
            content=candidate.content,
            source_message_id=user_msg.id,
            source_excerpt=candidate.source_excerpt,
            source="explicit",
            embedding=None,
            confidence=1.0,
            scope_id=scope.id,
            last_used_at=write_now,
        )
        db.add(memory)
        await db.flush()
        explicit_reembed_ids.append(memory.id)
        db.add(
            MemoryAudit(
                user_id=user.id,
                memory_id=memory.id,
                event_type="added",
                new_content=memory.content,
                source_message_id=user_msg.id,
                details={"source": "explicit"},
            )
        )
        token = await _memory_undo_token(
            {"user_id": user.id, "action": "added", "memory_id": memory.id}
        )
        writes.append(
            {
                "id": memory.id,
                "kind": "added",
                "type": memory.type,
                "content": memory.content,
                "source_excerpt": memory.source_excerpt,
                "undo_token": token,
                "scope_id": memory.scope_id,
                "recommended_scope_id": scope.id,
            }
        )
    if writes:
        assistant_msg.content = {**(assistant_msg.content or {}), "memory_writes": writes}


# ---------------------------------------------------------------------------
# Shared helper: build assistant message + completion/generations + outbox.
#
# Used by:
#   - POST /conversations/{conv_id}/messages         (this file)
#   - POST /conversations/{cid}/messages/{mid}/regenerate (regenerate.py)
#
# The helper assumes the caller has already created (and flushed) the *user*
# message. It returns the assistant message + ids created. The caller commits.
# ---------------------------------------------------------------------------


@dataclass
class AssistantTaskResult:
    assistant_msg: Message
    completion_id: str | None
    generation_ids: list[str]
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[OutboxEvent]


@dataclass(frozen=True)
class _TaskCredentialPin:
    credential_id: str
    supplier_id: str
    default_chat_model: str
    fast_chat_model: str | None
    # Why: image tasks must NOT reuse default_chat_model. If supplier exposes
    # a dedicated image model field (Group B migration), we pin the
    # generation row to that; otherwise fall back to default_chat_model
    # so existing suppliers keep working.
    default_image_model: str | None


async def _resolve_task_credential_pin(
    db: AsyncSession,
    user_id: str,
    required_purpose: str,
    account_mode: str,
) -> _TaskCredentialPin | None:
    if account_mode != "byok":
        return None

    active_row = (
        await db.execute(
            select(UserApiCredential, ApiSupplierTemplate)
            .join(ApiSupplierTemplate, ApiSupplierTemplate.id == UserApiCredential.supplier_id)
            .where(
                UserApiCredential.user_id == user_id,
                UserApiCredential.status == "active",
                UserApiCredential.deleted_at.is_(None),
                ApiSupplierTemplate.deleted_at.is_(None),
                ApiSupplierTemplate.enabled.is_(True),
            )
            .order_by(UserApiCredential.created_at.desc())
            .limit(1)
            )
    ).first()
    if active_row is not None:
        active, supplier = active_row
        rate_limited_until = getattr(active, "rate_limited_until", None)
        if rate_limited_until is not None:
            if rate_limited_until.tzinfo is None:
                rate_limited_until = rate_limited_until.replace(tzinfo=timezone.utc)
            if rate_limited_until > datetime.now(timezone.utc):
                raise _http(
                    "NO_ACTIVE_API_KEY",
                    "your API key is currently rate limited",
                    412,
                )
        if required_purpose not in set(supplier.purposes or []):
            raise _http(
                "NO_ACTIVE_API_KEY",
                "your current API Key does not support this task type",
                412,
            )
        return _TaskCredentialPin(
            credential_id=active.id,
            supplier_id=active.supplier_id,
            default_chat_model=supplier.default_chat_model or DEFAULT_CHAT_MODEL,
            fast_chat_model=supplier.fast_chat_model,
            # default_image_model is added by Group B migration. getattr
            # tolerates the pre-migration shape where the field is absent.
            default_image_model=getattr(supplier, "default_image_model", None),
        )

    raise _http(
        "NO_ACTIVE_API_KEY",
        "please upload an active API key before starting new tasks",
        412,
    )


async def _create_assistant_task(
    *,
    db: AsyncSession,
    user_id: str,
    account_mode: str,
    conv: Conversation,
    user_msg: Message,
    intent: Intent,
    idempotency_key: str,
    image_params: ImageParamsIn,
    chat_params: ChatParamsIn,
    system_prompt: str | None,
    attachment_ids: list[str],
    text: str,
    default_image_output_format: str = _DEFAULT_IMAGE_OUTPUT_FORMAT,
    mask_image_id: str | None = None,
) -> AssistantTaskResult:
    """Build assistant message + sub-task(s) + outbox in the open transaction.

    Caller is responsible for db.commit() and post-commit publish/enqueue.
    """
    produces_image = intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE)
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )

    # ---- size resolve (image intents only) ----
    resolved_size = None
    prompt_suffix = ""
    if produces_image:
        try:
            resolved_size = resolve_size(
                aspect=image_params.aspect_ratio,
                mode=image_params.size_mode,
                fixed=image_params.fixed_size,
            )
            prompt_suffix = resolved_size.prompt_suffix
        except Exception as e:  # noqa: BLE001
            raise _http("invalid_size", f"size resolve failed: {e}", 422)

    assistant_msg = Message(
        conversation_id=conv.id,
        role=Role.ASSISTANT.value,
        content={},
        parent_message_id=user_msg.id,
        intent=intent.value,
        status=MessageStatus.PENDING.value,
    )
    db.add(assistant_msg)
    await db.flush()

    completion_id: str | None = None
    generation_ids: list[str] = []
    outbox_payloads: list[dict[str, Any]] = []
    credential_pin = await _resolve_task_credential_pin(
        db,
        user_id,
        "image" if produces_image else "chat",
        account_mode,
    )

    if intent in (Intent.CHAT, Intent.VISION_QA):
        task_chat_model = (
            credential_pin.fast_chat_model
            if credential_pin and chat_params.fast and credential_pin.fast_chat_model
            else credential_pin.default_chat_model
            if credential_pin
            else DEFAULT_CHAT_MODEL
        )
        comp = Completion(
            message_id=assistant_msg.id,
            user_id=user_id,
            model=task_chat_model,
            input_image_ids=attachment_ids if intent == Intent.VISION_QA else [],
            system_prompt=system_prompt,
            text="",
            status=CompletionStatus.QUEUED.value,
            progress_stage=CompletionStage.QUEUED.value,
            attempt=0,
            idempotency_key=idempotency_key,
            upstream_request=_chat_upstream_request(chat_params),
            user_api_credential_id=(
                credential_pin.credential_id if credential_pin else None
            ),
            upstream_supplier_id=credential_pin.supplier_id if credential_pin else None,
        )
        db.add(comp)
        await db.flush()
        completion_id = comp.id
        outbox_payloads.append(
            {
                "task_id": comp.id,
                "user_id": user_id,
                "kind": "completion",
            }
        )
    else:
        # text_to_image / image_to_image
        count = max(1, min(16, image_params.count))
        action = (
            GenerationAction.EDIT.value
            if intent == Intent.IMAGE_TO_IMAGE
            else GenerationAction.GENERATE.value
        )
        primary = attachment_ids[0] if attachment_ids else None
        prompt_full = (text or "") + prompt_suffix
        assert resolved_size is not None  # guarded by produces_image branch
        upstream_request = _image_upstream_request(
            image_params,
            resolved_size,
            prompt=prompt_full,
            default_output_format=default_image_output_format,
        )
        billing_enabled = account_mode == "wallet" and await _billing_enabled(db)
        billing_thresholds = (
            await _billing_image_thresholds(db) if billing_enabled else {}
        )
        size_px = (
            (resolved_size.width or 0) * (resolved_size.height or 0)
            if resolved_size.width and resolved_size.height
            else billing_core.DEFAULT_IMAGE_SIZE_THRESHOLDS["1k"]
        )
        billing_tier = _requested_image_billing_tier(image_params)
        if not billing_enabled:
            estimated_micro, estimated_tier = (0, "free")
        elif billing_tier is not None:
            estimated_micro, estimated_tier = await billing_core.estimate_image_cost_for_tier(
                db,
                tier=billing_tier,
                n=1,
            )
        else:
            estimated_micro, estimated_tier = await billing_core.estimate_image_cost(
                db,
                size_px=size_px,
                n=1,
                thresholds=billing_thresholds or None,
            )
        if credential_pin:
            # Why: image tasks must use the supplier's image model when
            # available — chat models (e.g. gpt-5.4) cannot generate images
            # and would produce upstream 400s. fast_chat_model is reserved
            # for chat-only fast tier and is intentionally NOT used here.
            upstream_request["responses_model"] = (
                credential_pin.default_image_model
                or credential_pin.default_chat_model
            )
        if upstream_request.get("background") == "transparent":
            prompt_full += _transparent_background_prompt_suffix()

        for i in range(count):
            idem = idempotency_key if i == 0 else f"{idempotency_key}:{i}"
            gen = Generation(
                message_id=assistant_msg.id,
                user_id=user_id,
                action=action,
                prompt=prompt_full,
                size_requested=resolved_size.size,
                aspect_ratio=image_params.aspect_ratio,
                input_image_ids=attachment_ids,
                primary_input_image_id=primary,
                # mask 仅在 image_to_image 有意义；text_to_image 强制 None，
                # 保险起见在主流程入口做了 422，这里再兜一层避免误传。
                mask_image_id=(
                    mask_image_id if intent == Intent.IMAGE_TO_IMAGE else None
                ),
                status=GenerationStatus.QUEUED.value,
                progress_stage=GenerationStage.QUEUED.value,
                attempt=0,
                idempotency_key=idem,
                upstream_request=dict(upstream_request),
                user_api_credential_id=(
                    credential_pin.credential_id if credential_pin else None
                ),
                upstream_supplier_id=(
                    credential_pin.supplier_id if credential_pin else None
                ),
            )
            db.add(gen)
            await db.flush()
            if billing_enabled and estimated_micro > 0:
                try:
                    tx = await billing_core.hold(
                        db,
                        user_id,
                        estimated_micro,
                        ref_type="generation",
                        ref_id=gen.id,
                        idempotency_key=f"hold:{gen.id}",
                        allow_negative=await _billing_allow_negative(db),
                        meta={
                            "tier": estimated_tier,
                            "size_requested": resolved_size.size,
                            "pixels_estimated": size_px,
                        },
                    )
                except billing_core.BillingError as exc:
                    raise _billing_http_error(exc)
                if tx is not None:
                    await write_audit(
                        db,
                        event_type="wallet.hold.image",
                        user_id=user_id,
                        details={
                            "generation_id": gen.id,
                            "amount_micro": estimated_micro,
                            "tier": estimated_tier,
                            "balance_after": tx.balance_after,
                            "hold_after": tx.hold_after,
                        },
                        autocommit=False,
                    )
            generation_ids.append(gen.id)
            # Stagger 多张图入队：i=0 立即跑，i>=1 延迟 i*STAGGER 秒（cap CAP）。
            # 实测同 prompt 同账号同时打 ChatGPT codex 会触发 OpenAI 内部 race（一败一成稳定模式）；
            # 错开几秒让第二条到达时第一条已分配好 image_generation slot，避免碰撞。
            defer_s = (
                min(i * IMAGE_MULTI_GEN_STAGGER_S, IMAGE_MULTI_GEN_STAGGER_CAP_S)
                if i > 0
                else 0
            )
            payload: dict[str, Any] = {
                "task_id": gen.id,
                "user_id": user_id,
                "kind": "generation",
            }
            if defer_s > 0:
                payload["defer_s"] = defer_s
            outbox_payloads.append(payload)

    # outbox rows (same transaction)
    outbox_rows: list[OutboxEvent] = []
    for p in outbox_payloads:
        ev = OutboxEvent(kind=p["kind"], payload=p, published_at=None)
        db.add(ev)
        outbox_rows.append(ev)
    if outbox_rows:
        await db.flush()
        for payload, row in zip(outbox_payloads, outbox_rows, strict=False):
            payload["outbox_id"] = str(row.id)
            row.payload = dict(payload)

    return AssistantTaskResult(
        assistant_msg=assistant_msg,
        completion_id=completion_id,
        generation_ids=generation_ids,
        outbox_payloads=outbox_payloads,
        outbox_rows=outbox_rows,
    )


async def _publish_message_appended(
    *,
    redis: Any,
    user_id: str,
    conv_id: str,
    message_ids: list[str],
) -> None:
    """Best-effort publish for cross-device message list synchronization."""
    if not message_ids:
        return
    try:
        if len(message_ids) == 1:
            message_id = message_ids[0]
            await publish_sse_event(
                redis,
                user_id=user_id,
                channel=conv_channel(conv_id),
                event_name=EV_CONV_MSG_APPENDED,
                data={
                    "conversation_id": conv_id,
                    "message_id": message_id,
                },
            )
        else:
            await publish_sse_events(
                redis,
                [
                    {
                        "user_id": user_id,
                        "channel": conv_channel(conv_id),
                        "event_name": EV_CONV_MSG_APPENDED,
                        "data": {
                            "conversation_id": conv_id,
                            "message_id": message_id,
                        },
                    }
                    for message_id in message_ids
                ],
            )
    except Exception:
        logger.warning(
            "publish_message_appended failed user=%s conv=%s messages=%s",
            user_id,
            conv_id,
            message_ids,
            exc_info=True,
        )


async def _publish_assistant_task(
    *,
    db: AsyncSession,
    redis: Any,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str,
    outbox_payloads: list[dict[str, Any]],
    outbox_rows: list[OutboxEvent],
) -> None:
    """Best-effort: enqueue arq + publish/XADD for each outbox payload.

    Failures are logged; the outbox publisher will catch up. Caller must
    commit the transaction *before* invoking this.
    """
    _ = outbox_rows
    try:
        pool = await get_arq_pool()
        for p in outbox_payloads:
            fn_name = (
                "run_completion" if p["kind"] == "completion" else "run_generation"
            )
            # 多图 stagger：i>=1 的 generation row 在 payload 里带 defer_s，让 arq 延迟入队执行。
            # 主路径（直接 enqueue）和降级路径（outbox publisher）都需要透传这个字段——
            # 之前漏了主路径，导致即使 payload 含 defer_s=5，task 也立刻被 worker 拉起。
            enqueue_kwargs: dict[str, Any] = {}
            defer_s = p.get("defer_s")
            if isinstance(defer_s, (int, float)) and defer_s > 0:
                enqueue_kwargs["_defer_by"] = float(defer_s)
            enqueue_kwargs["_job_id"] = arq_job_id(
                p["kind"], p["task_id"], p.get("outbox_id")
            )
            await pool.enqueue_job(fn_name, p["task_id"], **enqueue_kwargs)

            ev_name = EV_COMP_QUEUED if p["kind"] == "completion" else EV_GEN_QUEUED
            id_field = "completion_id" if p["kind"] == "completion" else "generation_id"
            await publish_sse_event(
                redis,
                user_id=user_id,
                channel=task_channel(p["task_id"]),
                event_name=ev_name,
                data={
                    id_field: p["task_id"],
                    "message_id": assistant_msg_id,
                    "conversation_id": conv_id,
                    "kind": p["kind"],
                },
            )
    except Exception:
        # Why: Outbox publisher will catch up later; surface in logs so silent
        # publish failures are observable without rolling back committed work.
        logger.warning(
            "publish_assistant_task failed user=%s conv=%s msg=%s",
            user_id,
            conv_id,
            assistant_msg_id,
            exc_info=True,
        )
        return

    # Why: only mark outbox rows published after the redis pipe succeeded.
    if outbox_rows:
        try:
            now2 = datetime.now(timezone.utc)
            for row in outbox_rows:
                row.published_at = now2
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                logger.warning(
                    "outbox row rollback failed user=%s msg=%s",
                    user_id,
                    assistant_msg_id,
                    exc_info=True,
                )
            logger.warning(
                "outbox row mark-published failed user=%s msg=%s",
                user_id,
                assistant_msg_id,
                exc_info=True,
            )


async def _await_post_commit_publish(
    label: str,
    awaitable: Awaitable[Any],
    *,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str | None = None,
) -> None:
    """Bound best-effort publishing so POST /messages can return promptly.

    The message, task, and outbox rows are already committed before this runs.
    If Redis or ARQ stalls here, the outbox publisher is still the durable source
    of truth and will enqueue the task shortly after.
    """
    try:
        await asyncio.wait_for(awaitable, timeout=_POST_COMMIT_PUBLISH_TIMEOUT_S)
    except TimeoutError:
        logger.warning(
            "post_commit_publish timeout label=%s user=%s conv=%s msg=%s timeout_s=%.1f",
            label,
            user_id,
            conv_id,
            assistant_msg_id,
            _POST_COMMIT_PUBLISH_TIMEOUT_S,
        )
    except Exception:
        logger.warning(
            "post_commit_publish failed label=%s user=%s conv=%s msg=%s",
            label,
            user_id,
            conv_id,
            assistant_msg_id,
            exc_info=True,
        )


async def _lookup_idempotent_post(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> PostMessageOut | None:
    """Return prior PostMessageOut if (user, idempotency_key) already exists.

    Used for both the pre-check fast path and the IntegrityError fallback so the
    response shape stays bit-identical between concurrent and sequential cases.
    """
    alive_filters = _message_alive_filters()
    comp_hit = (
        await db.execute(
            select(Completion)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user_id,
                Completion.idempotency_key == idempotency_key,
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    gen_anchor = (
        await db.execute(
            select(Generation)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user_id,
                Generation.idempotency_key == idempotency_key,
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if not comp_hit and not gen_anchor:
        return None

    anchor_msg_id = comp_hit.message_id if comp_hit else gen_anchor.message_id
    assistant_msg = (
        await db.execute(
            select(Message).where(
                Message.id == anchor_msg_id,
                Message.conversation_id == conv_id,
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if assistant_msg is None:
        return None
    gen_hits: list[Generation] = []
    if gen_anchor is not None:
        gen_hits = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id == anchor_msg_id,
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
    user_msg = None
    if assistant_msg.parent_message_id:
        user_msg = (
            await db.execute(
                select(Message).where(
                    Message.id == assistant_msg.parent_message_id,
                    Message.conversation_id == conv_id,
                    *alive_filters,
                )
            )
        ).scalar_one_or_none()
    if user_msg is None:
        user_msg = (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.role == Role.USER.value,
                    *alive_filters,
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if user_msg is None:
        return None
    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(assistant_msg),
        completion_id=comp_hit.id if comp_hit else None,
        generation_ids=[g.id for g in gen_hits],
    )


@router.post(
    "/conversations/{conv_id}/messages",
    response_model=PostMessageOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_message(
    conv_id: str,
    body: PostMessageIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostMessageOut:
    return await submit_user_message(conv_id, body, user, db)


async def submit_user_message(
    conv_id: str,
    body: PostMessageIn,
    user: User,
    db: AsyncSession,
) -> PostMessageOut:
    """Post a user message + spawn assistant task. Used by the public
    `/conversations/{cid}/messages` route AND by the Telegram bot route
    (which authenticates via X-Bot-Token instead of session cookie). The
    function body is the original `post_message` logic verbatim — only
    the entry signature changed so callers can supply `user` directly.
    """
    redis = get_redis()
    await MESSAGES_LIMITER.check(redis, f"rl:msg:{user.id}")

    # ---- ownership check ----
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise _http("not_found", "conversation not found", 404)

    # ---- idempotency short-circuit (best-effort: skips an INSERT round trip) ---
    prior = await _lookup_idempotent_post(
        db, user.id, conv_id, body.idempotency_key
    )
    if prior is not None:
        return prior

    # Empty SELECT ... FOR UPDATE locks no rows, so it does not serialize the
    # first concurrent INSERT for an idempotency key. PostgreSQL advisory locks
    # give the intended per-user/per-key critical section; other dialects keep
    # relying on the unique constraint + IntegrityError fallback below.
    await _lock_idempotency_key(db, user.id, body.idempotency_key)

    # ---- validate attachments belong to user (and are alive) ----
    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        rows = (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(attachment_ids),
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if len(rows) != len(attachment_ids):
            raise _http("invalid_attachment", "one or more attachment images are not owned or were deleted", 400)

    # ---- mask 字段归一（intent 解析前只做 strip，规约移到 intent 后） ----
    mask_image_id = (body.mask_image_id or "").strip() or None

    # ---- intent routing ----
    intent = resolve_intent(
        explicit=body.intent,
        text=body.text or "",
        has_attachment=bool(attachment_ids),
    )
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )

    # ---- validate mask (local inpaint, image_to_image 专用) ----
    # 设计契约（DESIGN.md §7.6 + apps/web mask UI 已对齐）：
    #   - mask 仅在 intent=image_to_image 时有意义；其他 intent 带 mask → 422
    #   - mask 必须正好对应 1 张 reference（OpenAI /v1/images/edits 协议：mask
    #     只对 image[] 的第 0 张生效，多张参考图 + mask 是不符合上游契约的请求）
    #   - mask 必须属于当前用户 + 未软删 → 404
    #   - 尺寸不一致不在 API 层挡，留给 worker 用 PIL resize（API 简单）
    if mask_image_id is not None:
        if intent != Intent.IMAGE_TO_IMAGE:
            raise _http(
                "mask_requires_image_to_image",
                f"mask requires intent=image_to_image (got intent={intent.value})",
                422,
            )
        if len(attachment_ids) != 1:
            raise _http(
                "mask_requires_single_reference_image",
                f"mask requires exactly one reference image (got {len(attachment_ids)})",
                422,
            )
        mask_row = (
            await db.execute(
                select(Image.id).where(
                    Image.id == mask_image_id,
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if mask_row is None:
            raise _http("mask_not_found", "mask image not found", 404)

    # ---- single transaction ----
    now = datetime.now(timezone.utc)

    user_content: dict[str, Any] = {
        "text": body.text or "",
        "attachments": [{"image_id": i} for i in attachment_ids],
    }
    fast_default = await _resolve_fast_default(db)
    image_params = _image_params_with_fast_default(body.image_params, fast_default)
    chat_params = _chat_params_with_fast_default(body.chat_params, fast_default)

    # 推理强度仅对文本/视觉问答有意义；非空才写入，保持 content 干净。
    if intent in (Intent.CHAT, Intent.VISION_QA) and chat_params.reasoning_effort:
        if chat_params.reasoning_effort not in ALLOWED_REASONING_EFFORTS:
            raise _http("invalid_reasoning_effort", "invalid reasoning_effort", 422)
        user_content["reasoning_effort"] = chat_params.reasoning_effort
    # Fast 模式：chat 侧写进 user content；image 侧写进 Generation.upstream_request。
    # worker 读这些字段选择 priority / smaller rendering profiles。
    if intent in (Intent.CHAT, Intent.VISION_QA) and chat_params.fast:
        user_content["fast"] = True
    if intent in (Intent.CHAT, Intent.VISION_QA) and chat_params.web_search:
        user_content["web_search"] = True
    if intent in (Intent.CHAT, Intent.VISION_QA) and chat_params.file_search:
        user_content["file_search"] = True
        if chat_params.vector_store_ids:
            user_content["vector_store_ids"] = [
                v.strip()
                for v in chat_params.vector_store_ids
                if isinstance(v, str) and v.strip()
            ]
    if intent in (Intent.CHAT, Intent.VISION_QA) and chat_params.code_interpreter:
        user_content["code_interpreter"] = True
    if intent in (Intent.CHAT, Intent.VISION_QA) and chat_params.image_generation:
        user_content["image_generation"] = True

    system_prompt = None
    if intent in (Intent.CHAT, Intent.VISION_QA):
        system_prompt = await resolve_system_prompt_for_message(
            db,
            user_id=user.id,
            default_system_prompt_id=user.default_system_prompt_id,
            conv=conv,
            explicit_prompt=chat_params.system_prompt,
        )
        if getattr(user, "account_mode", "wallet") == "wallet" and await _billing_enabled(db):
            # Why: lock=True so concurrent chat submissions from the same user
            # serialize on the wallet row. Without the lock, N parallel chats
            # all see the same pre-charge balance and overdraw together; design
            # §6.3.2 tolerates a single transient overdraw but not N.
            wallet = await billing_core.get_wallet(db, user.id, lock=True)
            if wallet.balance_micro < 10_000:
                raise _http(
                    "INSUFFICIENT_BALANCE",
                    "insufficient wallet balance",
                    402,
                )
            cost_preview = await billing_core.estimate_completion_cost(
                db,
                model=DEFAULT_CHAT_MODEL,
                tokens_in=1,
                tokens_out=1,
            )
            if cost_preview <= 0:
                await _audit_billing_gap(
                    db,
                    event_type="pricing.not_configured",
                    user=user,
                    details={
                        "scope": "chat_model",
                        "model": DEFAULT_CHAT_MODEL,
                        "route": "messages.preflight",
                    },
                )
    default_image_output_format = _DEFAULT_IMAGE_OUTPUT_FORMAT
    if intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE):
        spec = get_spec("image.output_format")
        if spec is not None:
            raw_default_format = await get_setting(db, spec)
            if raw_default_format in _IMAGE_OUTPUT_FORMAT_VALUES:
                default_image_output_format = raw_default_format

    user_msg = Message(
        conversation_id=conv_id,
        role=Role.USER.value,
        content=user_content,
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()  # need user_msg.id for parent_message_id
    if intent in (Intent.CHAT, Intent.VISION_QA):
        await _apply_pending_confirmation_reply(
            db=db,
            user=user,
            conv=conv,
            user_msg=user_msg,
            text=body.text or "",
        )

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=body.idempotency_key,
        image_params=image_params,
        chat_params=chat_params,
        system_prompt=system_prompt,
        attachment_ids=attachment_ids,
        text=body.text or "",
        default_image_output_format=default_image_output_format,
        mask_image_id=mask_image_id,
    )
    explicit_reembed_ids: list[str] = []
    if intent in (Intent.CHAT, Intent.VISION_QA):
        await _apply_explicit_memory_write(
            db=db,
            user=user,
            conv=conv,
            user_msg=user_msg,
            assistant_msg=result.assistant_msg,
            text=body.text or "",
            reembed_ids=explicit_reembed_ids,
        )

    # bump conversation last_activity_at
    conv.last_activity_at = now

    try:
        await db.commit()
    except IntegrityError:
        # Why: concurrent request with the same idempotency_key won the race;
        # rely on the (user_id, idempotency_key) unique constraint and return
        # the prior result instead of raising 500.
        await db.rollback()
        prior = await _lookup_idempotent_post(
            db, user.id, conv_id, body.idempotency_key
        )
        if prior is not None:
            return prior
        raise _http(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        )
    await db.refresh(user_msg)
    await db.refresh(result.assistant_msg)
    for memory_id in explicit_reembed_ids:
        await _enqueue_memory_reembed("memory", memory_id)

    # ---- best-effort publish ----
    await _await_post_commit_publish(
        "message_appended",
        _publish_message_appended(
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            message_ids=[user_msg.id, result.assistant_msg.id],
        ),
        user_id=user.id,
        conv_id=conv_id,
    )
    await _await_post_commit_publish(
        "assistant_task",
        _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            assistant_msg_id=result.assistant_msg.id,
            outbox_payloads=result.outbox_payloads,
            outbox_rows=result.outbox_rows,
        ),
        user_id=user.id,
        conv_id=conv_id,
        assistant_msg_id=result.assistant_msg.id,
    )

    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(result.assistant_msg),
        completion_id=result.completion_id,
        generation_ids=result.generation_ids,
    )


# ---------------------------------------------------------------------------
# Silent generation: 仅创建 assistant + generation，不创建用户消息。
# 用于重画（reroll）和放大（upscale）场景。
# ---------------------------------------------------------------------------

from pydantic import BaseModel, Field as PydanticField  # noqa: E402


class SilentGenerationIn(BaseModel):
    idempotency_key: str = PydanticField(min_length=1, max_length=64)
    parent_message_id: str
    intent: Literal["text_to_image", "image_to_image"] = "text_to_image"
    image_params: ImageParamsIn = PydanticField(default_factory=ImageParamsIn)
    prompt: str = PydanticField(default="", max_length=MAX_PROMPT_CHARS)
    attachment_image_ids: list[str] = PydanticField(
        default_factory=list,
        max_length=MAX_MESSAGE_ATTACHMENTS,
    )


class SilentGenerationOut(BaseModel):
    assistant_message: MessageOut
    generation_ids: list[str] = PydanticField(default_factory=list)


@router.post(
    "/conversations/{conv_id}/generations",
    response_model=SilentGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_silent_generation(
    conv_id: str,
    body: SilentGenerationIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SilentGenerationOut:
    """Create a generation without a user message (for reroll / upscale)."""
    redis = get_redis()

    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise _http("not_found", "conversation not found", 404)

    parent_msg = (
        await db.execute(
            select(Message).where(
                Message.id == body.parent_message_id,
                Message.conversation_id == conv_id,
                *_message_alive_filters(),
            )
        )
    ).scalar_one_or_none()
    if not parent_msg:
        raise _http("not_found", "parent message not found", 404)

    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        rows = (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(attachment_ids),
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if len(rows) != len(attachment_ids):
            raise _http("invalid_attachment", "attachment not owned or deleted", 400)

    intent = Intent(body.intent)
    text = body.prompt
    default_image_output_format = _DEFAULT_IMAGE_OUTPUT_FORMAT
    spec = get_spec("image.output_format")
    if spec is not None:
        raw_default_format = await get_setting(db, spec)
        if raw_default_format in _IMAGE_OUTPUT_FORMAT_VALUES:
            default_image_output_format = raw_default_format
    fast_default = await _resolve_fast_default(db)
    image_params = _image_params_with_fast_default(body.image_params, fast_default)

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=parent_msg,
        intent=intent,
        idempotency_key=body.idempotency_key,
        image_params=image_params,
        chat_params=ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=text,
        default_image_output_format=default_image_output_format,
    )

    now = datetime.now(timezone.utc)
    conv.last_activity_at = now

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _http("idempotency_conflict", "idempotency_key conflict", 409)

    await db.refresh(result.assistant_msg)

    await _await_post_commit_publish(
        "message_appended",
        _publish_message_appended(
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            message_ids=[result.assistant_msg.id],
        ),
        user_id=user.id,
        conv_id=conv_id,
    )
    await _await_post_commit_publish(
        "assistant_task",
        _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            assistant_msg_id=result.assistant_msg.id,
            outbox_payloads=result.outbox_payloads,
            outbox_rows=result.outbox_rows,
        ),
        user_id=user.id,
        conv_id=conv_id,
        assistant_msg_id=result.assistant_msg.id,
    )

    return SilentGenerationOut(
        assistant_message=MessageOut.model_validate(result.assistant_msg),
        generation_ids=result.generation_ids,
    )


@router.get(
    "/conversations/{conv_id}/messages/{message_id}",
    response_model=MessageOut,
)
async def get_message(
    conv_id: str,
    message_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageOut:
    msg = (
        await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
                *_message_alive_filters(),
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise _http("not_found", "message not found", 404)
    return MessageOut.model_validate(msg)
