"""Messages 路由（DESIGN §5.4 — 核心写入接口）。

POST /conversations/{conv_id}/messages
1. 鉴权 + rate limit
2. 意图路由（auto → chat / vision_qa / text_to_image / image_to_image）
3. 出图参数校验 + 尺寸解析（lumen_core.sizing.resolve_size）
4. 幂等：(user, conversation, idempotency_key) 命中 → 直接返回既有三件套
5. 单事务：INSERT messages(user) + messages(assistant, pending) + 子任务 + outbox_events
6. 事务提交后尽力 XADD queue + PUBLISH task.queued + XADD events:user:{uid}
7. 返回 PostMessageOut
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    IMAGE_MULTI_GEN_STAGGER_CAP_S,  # noqa: F401 - compatibility facade
    IMAGE_MULTI_GEN_STAGGER_S,  # noqa: F401 - compatibility facade
    MAX_MESSAGE_ATTACHMENTS,
    Intent,
    MAX_PROMPT_CHARS,
    MessageStatus,  # noqa: F401 - compatibility facade for route tests
    Role,
)
from lumen_core.memory import (
    canonical_memory_text,
    extract_memories,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    MemoryAudit,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
)
from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    is_user_visible as byok_retention_is_user_visible,
    user_visible_filter as byok_retention_user_visible_filter,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    MessageOut,
    PostMessageIn,
    PostMessageOut,
)
from lumen_core import billing as billing_core  # noqa: F401 - compatibility facade

from ..arq_pool import get_arq_pool
from ..audit import write_audit

# Why: read_byok_settings is re-exported here so existing tests that
# monkeypatch `messages.read_byok_settings` keep working. Production code on
# this path uses read_byok_settings_cached (TTL ~30 s) — see review #20.
from ..byok_service import (  # noqa: F401
    read_byok_settings,
    read_byok_settings_cached,
    retention_policy_from_settings,
)
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..intent import resolve_intent
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..runtime_settings import embedding_provider_available, get_setting
from ..services import message_submission as _message_submission
from ..services.message_request import (
    AssistantContextRuntime,
    MessageTransactionRuntime,
    build_user_content,
    is_chat_intent,
    persist_message_request,
    resolve_assistant_context,
    validate_attachment_ids,
    validate_mask_image,
)
from ..sse_publish import publish_sse_event, publish_sse_events
from ..task_billing import (
    ChatWalletPreflight as _ChatWalletPreflight,
    apply_rate_multiplier_micro as _apply_rate_multiplier_micro,
    requested_image_billing_tier as _requested_image_billing_tier,
    user_rate_multiplier_x10000 as _user_rate_multiplier_x10000,
)


router = APIRouter()

logger = logging.getLogger(__name__)


async def _lock_idempotency_key(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> bool:
    connection = getattr(db, "connection", None)
    if connection is None:
        return False
    bind = await connection()
    if bind.dialect.name != "postgresql":
        return False
    lock_key = _idempotency_lock_key(user_id, conv_id, idempotency_key)
    # Why hashtext is OK here: pg_advisory_xact_lock takes a 64-bit signed int
    # and hashtext returns a 32-bit signed int — i.e. there is collision risk
    # across distinct (user_id, conv_id, idempotency_key) triples. Collisions only cost
    # serialization (one extra waiter blocks until the txn commits) and never
    # corrupt; the race we actually guard against is duplicate inserts of the
    # same key, which still hash identically. So 32-bit hash collisions only
    # slow, never corrupt.
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))
    return True


AssistantTaskResult = _message_submission.AssistantTaskResult
_TaskCredentialPin = _message_submission.TaskCredentialPin
_IMAGE_OUTPUT_FORMAT_VALUES = _message_submission.IMAGE_OUTPUT_FORMAT_VALUES
_DEFAULT_IMAGE_OUTPUT_FORMAT = _message_submission.DEFAULT_IMAGE_OUTPUT_FORMAT
_idempotency_lock_key = _message_submission.idempotency_lock_key
_stored_idempotency_key = _message_submission.stored_idempotency_key
_generation_child_idempotency_key = _message_submission.generation_child_idempotency_key
_image_multi_generation_defer_s = _message_submission.image_multi_generation_defer_s
_idempotency_lookup_keys = _message_submission.idempotency_lookup_keys
_image_params_with_fast_default = _message_submission.image_params_with_fast_default
_chat_params_with_fast_default = _message_submission.chat_params_with_fast_default
_wants_transparent_background = _message_submission.wants_transparent_background
_image_upstream_request = _message_submission.image_upstream_request
_message_request_metadata = _message_submission.message_request_metadata
build_structured_system_prompt = _message_submission.build_structured_system_prompt
resolve_system_prompt_for_message = (
    _message_submission.resolve_system_prompt_for_message
)

ALLOWED_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_SILENT_GENERATION_REQUEST_HASH_KEY = "request_hash"
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


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


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
        prompt = _message_submission._sanitize_system_prompt_source(candidate)
        if prompt is not None:
            return prompt
    return None


_billing_setting_raw = _message_submission.billing_setting_raw
_billing_enabled = _message_submission.billing_enabled
_billing_allow_negative = _message_submission.billing_allow_negative
_billing_image_thresholds = _message_submission.billing_image_thresholds
_chat_tool_budget_setting_micro = _message_submission.chat_tool_budget_setting_micro
_chat_max_tool_invocations = _message_submission.chat_max_tool_invocations


async def _ensure_chat_wallet_preflight(
    db: AsyncSession,
    *,
    user_id: str,
    user_email: str | None,
    account_mode: str,
    model: str,
    chat_params: ChatParamsIn | None = None,
) -> _ChatWalletPreflight | None:
    return await _message_submission.ensure_chat_wallet_preflight(
        db,
        user_id=user_id,
        user_email=user_email,
        account_mode=account_mode,
        model=model,
        chat_params=chat_params,
        billing_enabled_fn=_billing_enabled,
        billing_allow_negative_fn=_billing_allow_negative,
        user_rate_multiplier_fn=_user_rate_multiplier_x10000,
        chat_tool_budget_setting_fn=_chat_tool_budget_setting_micro,
        chat_max_tool_invocations_fn=_chat_max_tool_invocations,
    )


async def _resolve_fast_default(db: AsyncSession) -> bool:
    return await _message_submission.resolve_fast_default(
        db,
        get_spec_fn=get_spec,
        get_setting_fn=get_setting,
    )


async def _ensure_file_search_configured(
    db: AsyncSession,
    chat_params: ChatParamsIn,
) -> None:
    await _message_submission.ensure_file_search_configured(
        db,
        chat_params,
        get_spec_fn=get_spec,
        get_setting_fn=get_setting,
    )


def _message_alive_filters() -> tuple[Any, ...]:
    deleted_at = getattr(Message, "deleted_at", None)
    if deleted_at is None:
        return ()
    return (deleted_at.is_(None),)


async def _byok_retention_policy_for_user(db: AsyncSession, user: User):
    if not byok_retention_applies_to_user(user):
        return None
    return retention_policy_from_settings(await read_byok_settings_cached(db))


def _message_user_visible_filters(
    user: User,
    *,
    retention_policy: Any | None,
) -> tuple[Any, ...]:
    filters = list(_message_alive_filters())
    if retention_policy is not None:
        retention_filter = byok_retention_user_visible_filter(
            user,
            Message.created_at,
            policy=retention_policy,
        )
        if retention_filter is not None:
            filters.append(retention_filter)
    return tuple(filters)


async def _ensure_conversation_visible_to_user(
    db: AsyncSession,
    conv: Conversation,
    user: User,
) -> None:
    policy = await _byok_retention_policy_for_user(db, user)
    if policy is None:
        return
    if not byok_retention_is_user_visible(
        account_mode=user.account_mode,
        created_at=conv.last_activity_at,
        policy=policy,
    ):
        raise _http("not_found", "conversation not found", 404)


async def _byok_image_visible_filter(db: AsyncSession, user: User):
    policy = await _byok_retention_policy_for_user(db, user)
    if policy is None:
        return None
    return byok_retention_user_visible_filter(user, Image.created_at, policy=policy)


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


async def _disable_memory_for_conversation(
    conversation_id: str, memory_id: str
) -> None:
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
            assistant_msg.content = {
                **(assistant_msg.content or {}),
                "memory_writes": writes,
            }
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
            (
                await db.execute(
                    select(UserMemory).where(
                        UserMemory.user_id == user.id,
                        UserMemory.type == candidate.type,
                        UserMemory.disabled.is_(False),
                        UserMemory.superseded_by.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
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
        assistant_msg.content = {
            **(assistant_msg.content or {}),
            "memory_writes": writes,
        }


async def _resolve_task_credential_pin(
    db: AsyncSession,
    user_id: str,
    required_purpose: str,
    account_mode: str,
) -> _TaskCredentialPin | None:
    return await _message_submission.resolve_task_credential_pin(
        db,
        user_id,
        required_purpose,
        account_mode,
        read_byok_settings_cached_fn=read_byok_settings_cached,
    )


_select_chat_task_model = _message_submission._select_chat_task_model


async def _create_assistant_task(**kwargs: Any) -> AssistantTaskResult:
    return await _message_submission.create_assistant_task(
        **kwargs,
        resolve_task_credential_pin_fn=_resolve_task_credential_pin,
        ensure_chat_wallet_preflight_fn=_ensure_chat_wallet_preflight,
        billing_enabled_fn=_billing_enabled,
        billing_allow_negative_fn=_billing_allow_negative,
        billing_image_thresholds_fn=_billing_image_thresholds,
        user_rate_multiplier_fn=_user_rate_multiplier_x10000,
        apply_rate_multiplier_fn=_apply_rate_multiplier_micro,
        requested_image_billing_tier_fn=_requested_image_billing_tier,
        write_audit_fn=write_audit,
    )


async def _publish_message_appended(**kwargs: Any) -> None:
    await _message_submission.publish_message_appended(
        **kwargs,
        publish_sse_event_fn=publish_sse_event,
        publish_sse_events_fn=publish_sse_events,
        log=logger,
    )


async def _publish_assistant_task(**kwargs: Any) -> None:
    await _message_submission.publish_assistant_task(
        **kwargs,
        get_arq_pool_fn=get_arq_pool,
        publish_sse_event_fn=publish_sse_event,
        log=logger,
    )


async def _await_post_commit_publish(
    label: str,
    awaitable: Awaitable[Any],
    *,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str | None = None,
) -> None:
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
    """Return prior PostMessageOut if (user, conversation, idempotency_key) exists.

    Used for both the pre-check fast path and the IntegrityError fallback so the
    response shape stays bit-identical between concurrent and sequential cases.
    """
    alive_filters = _message_alive_filters()
    lookup_keys = _idempotency_lookup_keys(conv_id, idempotency_key)
    comp_hit = (
        await db.execute(
            select(Completion)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user_id,
                Completion.idempotency_key.in_(lookup_keys),
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
                Generation.idempotency_key.in_(lookup_keys),
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if comp_hit is not None:
        anchor_msg_id = comp_hit.message_id
    elif gen_anchor is not None:
        anchor_msg_id = gen_anchor.message_id
    else:
        return None
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
        gen_hits = list(
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == user_id,
                        Generation.message_id == anchor_msg_id,
                    )
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                )
            )
            .scalars()
            .all()
        )
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


def _assistant_context_runtime() -> AssistantContextRuntime:
    return AssistantContextRuntime(
        resolve_system_prompt=resolve_system_prompt_for_message,
        resolve_credential_pin=_resolve_task_credential_pin,
        get_setting=get_setting,
        default_image_output_format=_DEFAULT_IMAGE_OUTPUT_FORMAT,
        image_output_format_values=_IMAGE_OUTPUT_FORMAT_VALUES,
    )


def _message_transaction_runtime() -> MessageTransactionRuntime:
    return MessageTransactionRuntime(
        apply_pending_confirmation_reply=_apply_pending_confirmation_reply,
        create_assistant_task=_create_assistant_task,
        apply_explicit_memory_write=_apply_explicit_memory_write,
        lookup_idempotent_post=_lookup_idempotent_post,
        http_error=_http,
    )


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
    await _ensure_conversation_visible_to_user(db, conv, user)

    # ---- idempotency short-circuit (best-effort: skips an INSERT round trip) ---
    prior = await _lookup_idempotent_post(db, user.id, conv_id, body.idempotency_key)
    if prior is not None:
        return prior

    # Empty SELECT ... FOR UPDATE locks no rows, so it does not serialize the
    # first concurrent INSERT for an idempotency key. PostgreSQL advisory locks
    # give the intended per-user/per-key critical section; other dialects keep
    # relying on the unique constraint + IntegrityError fallback below.
    if await _lock_idempotency_key(db, user.id, conv_id, body.idempotency_key):
        prior = await _lookup_idempotent_post(
            db,
            user.id,
            conv_id,
            body.idempotency_key,
        )
        if prior is not None:
            return prior

    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        image_retention_filter = await _byok_image_visible_filter(db, user)
        await validate_attachment_ids(
            db,
            user_id=user.id,
            attachment_ids=attachment_ids,
            visibility_filter=image_retention_filter,
            http_error=_http,
        )

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

    if mask_image_id is not None:
        image_retention_filter = await _byok_image_visible_filter(db, user)
        await validate_mask_image(
            db,
            user_id=user.id,
            intent=intent,
            attachment_ids=attachment_ids,
            mask_image_id=mask_image_id,
            visibility_filter=image_retention_filter,
            http_error=_http,
        )

    # ---- single transaction ----
    now = datetime.now(timezone.utc)
    request_metadata = _message_request_metadata(
        body,
        attachment_ids=attachment_ids,
        mask_image_id=mask_image_id,
        intent=intent,
    )
    fast_default = await _resolve_fast_default(db)
    image_params = _image_params_with_fast_default(body.image_params, fast_default)
    chat_params = _chat_params_with_fast_default(body.chat_params, fast_default)
    if is_chat_intent(intent):
        await _ensure_file_search_configured(db, chat_params)
    user_content = build_user_content(
        body,
        request_metadata=request_metadata,
        attachment_ids=attachment_ids,
        chat_params=chat_params,
        intent=intent,
        allowed_reasoning_efforts=ALLOWED_REASONING_EFFORTS,
        http_error=_http,
    )
    account_mode = getattr(user, "account_mode", "wallet")
    assistant_context = await resolve_assistant_context(
        db,
        _assistant_context_runtime(),
        user=user,
        conversation=conv,
        intent=intent,
        chat_params=chat_params,
        account_mode=account_mode,
    )
    transaction = await persist_message_request(
        db,
        _message_transaction_runtime(),
        user=user,
        conversation=conv,
        conv_id=conv_id,
        body=body,
        intent=intent,
        user_content=user_content,
        image_params=image_params,
        chat_params=chat_params,
        assistant_context=assistant_context,
        attachment_ids=attachment_ids,
        mask_image_id=mask_image_id,
        request_metadata=request_metadata,
        account_mode=account_mode,
        now=now,
    )
    if transaction.idempotent_response is not None:
        return transaction.idempotent_response
    user_msg = transaction.user_message
    result = transaction.assistant_task
    assert user_msg is not None and result is not None
    await db.refresh(user_msg)
    await db.refresh(result.assistant_msg)
    for memory_id in transaction.reembed_ids:
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


def _silent_generation_request_hash(body: SilentGenerationIn) -> str:
    payload = {
        "parent_message_id": body.parent_message_id,
        "intent": body.intent,
        "prompt": body.prompt,
        "attachment_image_ids": list(body.attachment_image_ids),
        "image_params": body.image_params.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stored_silent_generation_request_hash(generation: Generation) -> Any:
    request = getattr(generation, "upstream_request", None)
    if not isinstance(request, dict):
        return None
    return request.get(_SILENT_GENERATION_REQUEST_HASH_KEY)


async def _lookup_silent_generation(
    db: AsyncSession,
    *,
    user: User,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
    parent_message_id: str,
    request_hash: str,
    retention_policy: Any | None,
) -> SilentGenerationOut | None:
    lookup_keys = _idempotency_lookup_keys(conv_id, idempotency_key)
    anchor = (
        await db.execute(
            select(Generation)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user_id,
                Generation.idempotency_key.in_(lookup_keys),
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .order_by(Generation.created_at.asc(), Generation.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        return None
    assistant_msg = (
        await db.execute(
            select(Message).where(
                Message.id == anchor.message_id,
                Message.conversation_id == conv_id,
                Message.role == Role.ASSISTANT.value,
                *_message_user_visible_filters(
                    user,
                    retention_policy=retention_policy,
                ),
            )
        )
    ).scalar_one_or_none()
    if assistant_msg is None:
        raise _http("not_found", "assistant message not found", 404)
    stored_parent_message_id = assistant_msg.parent_message_id
    if not isinstance(stored_parent_message_id, str) or not stored_parent_message_id:
        raise _http("idempotency_conflict", "idempotency_key conflict", 409)
    stored_parent = (
        await db.execute(
            select(Message).where(
                Message.id == stored_parent_message_id,
                Message.conversation_id == conv_id,
                *_message_user_visible_filters(
                    user,
                    retention_policy=retention_policy,
                ),
            )
        )
    ).scalar_one_or_none()
    if stored_parent is None:
        raise _http("not_found", "parent message not found", 404)
    generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id == anchor.message_id,
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not generations:
        generations = [anchor]

    if stored_parent_message_id != parent_message_id:
        raise _http("idempotency_conflict", "idempotency_key conflict", 409)

    stored_hashes = [
        _stored_silent_generation_request_hash(generation) for generation in generations
    ]
    present_hashes = [value for value in stored_hashes if value is not None]
    if present_hashes and (
        len(present_hashes) != len(stored_hashes)
        or any(
            not isinstance(value, str) or value != request_hash
            for value in present_hashes
        )
    ):
        raise _http("idempotency_conflict", "idempotency_key conflict", 409)

    return SilentGenerationOut(
        assistant_message=MessageOut.model_validate(assistant_msg),
        generation_ids=[generation.id for generation in generations],
    )


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
    await _ensure_conversation_visible_to_user(db, conv, user)

    request_hash = _silent_generation_request_hash(body)
    retention_policy = await _byok_retention_policy_for_user(db, user)
    prior = await _lookup_silent_generation(
        db,
        user=user,
        user_id=user.id,
        conv_id=conv_id,
        idempotency_key=body.idempotency_key,
        parent_message_id=body.parent_message_id,
        request_hash=request_hash,
        retention_policy=retention_policy,
    )
    if prior is not None:
        return prior
    if await _lock_idempotency_key(db, user.id, conv_id, body.idempotency_key):
        prior = await _lookup_silent_generation(
            db,
            user=user,
            user_id=user.id,
            conv_id=conv_id,
            idempotency_key=body.idempotency_key,
            parent_message_id=body.parent_message_id,
            request_hash=request_hash,
            retention_policy=retention_policy,
        )
        if prior is not None:
            return prior

    parent_msg = (
        await db.execute(
            select(Message).where(
                Message.id == body.parent_message_id,
                Message.conversation_id == conv_id,
                *_message_user_visible_filters(
                    user,
                    retention_policy=retention_policy,
                ),
            )
        )
    ).scalar_one_or_none()
    if not parent_msg:
        raise _http("not_found", "parent message not found", 404)

    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        image_retention_filter = await _byok_image_visible_filter(db, user)
        rows = (
            (
                await db.execute(
                    select(Image.id).where(
                        Image.id.in_(attachment_ids),
                        Image.user_id == user.id,
                        Image.deleted_at.is_(None),
                        *(
                            (image_retention_filter,)
                            if image_retention_filter is not None
                            else ()
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
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
        request_metadata={
            _SILENT_GENERATION_REQUEST_HASH_KEY: request_hash,
        },
    )

    now = datetime.now(timezone.utc)
    conv.last_activity_at = now

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await _lookup_silent_generation(
            db,
            user=user,
            user_id=user.id,
            conv_id=conv_id,
            idempotency_key=body.idempotency_key,
            parent_message_id=body.parent_message_id,
            request_hash=request_hash,
            retention_policy=retention_policy,
        )
        if prior is not None:
            return prior
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
