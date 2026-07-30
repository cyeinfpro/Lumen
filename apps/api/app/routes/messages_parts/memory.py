"""Conversation memory side effects owned by the messages route."""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.memory import canonical_memory_text, extract_memories
from lumen_core.models import (
    Conversation,
    MemoryAudit,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
)


CONFIRM_REPLY_YES_RE = re.compile(
    r"^\s*(对|是|嗯|可以|继续|好|yes|yep|yeah|ok|okay)\b|按.*来",
    re.IGNORECASE,
)
# Chinese word boundaries are unreliable, so negative replies must be anchored.
CONFIRM_REPLY_NO_RE = re.compile(
    r"^\s*(不是|不要|不用|不按|换一?[下个]?|别|no|nope|don'?t|do not)",
    re.IGNORECASE,
)


async def default_memory_scope(
    db: AsyncSession,
    user_id: str,
) -> UserMemoryScope:
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


async def enqueue_memory_reembed(
    target: str,
    row_id: str,
    *,
    get_arq_pool_fn: Callable[[], Awaitable[Any]],
    log: logging.Logger,
) -> None:
    try:
        pool = await get_arq_pool_fn()
        await pool.enqueue_job("memory_reembed", target, row_id)
    except Exception:
        log.warning(
            "memory_reembed enqueue failed target=%s id=%s",
            target,
            row_id,
            exc_info=True,
        )


async def memory_undo_token(
    payload: dict[str, Any],
    *,
    get_redis_fn: Callable[[], Any],
) -> str | None:
    token = secrets.token_urlsafe(24)
    try:
        await get_redis_fn().setex(
            f"memory:undo:{token}",
            300,
            json.dumps(payload, separators=(",", ":")),
        )
        return token
    except Exception:
        return None


async def disable_memory_for_conversation(
    conversation_id: str,
    memory_id: str,
    *,
    get_redis_fn: Callable[[], Any],
) -> None:
    try:
        key = f"memory:conversation:{conversation_id}:disabled"
        pipe = get_redis_fn().pipeline(transaction=False)
        pipe.sadd(key, memory_id)
        pipe.expire(key, 30 * 24 * 60 * 60)
        await pipe.execute()
    except Exception:
        return


def confirmation_reply_decision(
    text: str,
) -> Literal["yes", "no", "skip"] | None:
    value = " ".join((text or "").split()).strip()
    if not value:
        return None
    if CONFIRM_REPLY_NO_RE.search(value):
        return "no"
    if CONFIRM_REPLY_YES_RE.search(value):
        return "yes"
    return "skip"


async def apply_pending_confirmation_reply(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    user_msg: Message,
    text: str,
    decision_fn: Callable[[str], Literal["yes", "no", "skip"] | None],
    disable_memory_fn: Callable[[str, str], Awaitable[None]],
) -> None:
    decision = decision_fn(text)
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
        await disable_memory_fn(conv.id, memory.id)
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


async def apply_explicit_memory_write(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    user_msg: Message,
    assistant_msg: Message,
    text: str,
    reembed_ids: list[str] | None,
    embedding_provider_available_fn: Callable[[AsyncSession], Awaitable[bool]],
    default_memory_scope_fn: Callable[[AsyncSession, str], Awaitable[UserMemoryScope]],
    memory_undo_token_fn: Callable[[dict[str, Any]], Awaitable[str | None]],
) -> None:
    """Synchronously persist explicit memories for use on the next turn."""
    if (
        bool(getattr(user, "memory_disabled", False))
        or bool(getattr(user, "memory_paused", False))
        or bool(getattr(conv, "memory_disabled", False))
    ):
        return
    if not await embedding_provider_available_fn(db):
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

    default_scope = await default_memory_scope_fn(db, user.id)
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
                memory
                for memory in existing
                if canonical_memory_text(memory.content)
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
            token = await memory_undo_token_fn(
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
        token = await memory_undo_token_fn(
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
