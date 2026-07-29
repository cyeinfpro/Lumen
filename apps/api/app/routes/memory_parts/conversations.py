"""Conversation-specific memory route implementations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Completion, Conversation, Message, User

from .contracts import (
    ConversationActiveScopeIn,
    ConversationMemoryDisabledIn,
    UsedMemoriesOut,
)


Operation = Callable[..., Any]
AsyncOperation = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ConversationMemoryDependencies:
    http: Operation
    owned_scope: AsyncOperation
    publish_conversation_memory_updated: AsyncOperation
    publish_account_settings_updated: AsyncOperation
    filter_owned_used_memory_payload: AsyncOperation
    get_redis: Operation


async def patch_conversation_memory_disabled_impl(
    conv_id: str,
    body: ConversationMemoryDisabledIn,
    user: User,
    db: AsyncSession,
    *,
    deps: ConversationMemoryDependencies,
) -> dict[str, bool]:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise deps.http("not_found", "conversation not found", 404)
    conv.memory_disabled = body.disabled
    await db.commit()
    redis = deps.get_redis()
    await deps.publish_conversation_memory_updated(
        redis,
        user_id=user.id,
        conversation_id=conv_id,
        payload={
            "conversation_id": conv_id,
            "memory_disabled": conv.memory_disabled,
            "active_scope_id": conv.active_scope_id,
        },
    )
    await deps.publish_account_settings_updated(redis, user.id)
    return {"disabled": conv.memory_disabled}


async def patch_conversation_active_scope_impl(
    conv_id: str,
    body: ConversationActiveScopeIn,
    user: User,
    db: AsyncSession,
    *,
    deps: ConversationMemoryDependencies,
) -> dict[str, str | None]:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise deps.http("not_found", "conversation not found", 404)
    if body.scope_id is None:
        conv.active_scope_id = None
    else:
        conv.active_scope_id = (await deps.owned_scope(db, user.id, body.scope_id)).id
    await db.commit()
    redis = deps.get_redis()
    await deps.publish_conversation_memory_updated(
        redis,
        user_id=user.id,
        conversation_id=conv_id,
        payload={
            "conversation_id": conv_id,
            "memory_disabled": conv.memory_disabled,
            "active_scope_id": conv.active_scope_id,
        },
    )
    await deps.publish_account_settings_updated(redis, user.id)
    return {"scope_id": conv.active_scope_id}


async def get_conversation_used_memories_impl(
    conv_id: str,
    user: User,
    db: AsyncSession,
    *,
    deps: ConversationMemoryDependencies,
) -> UsedMemoriesOut:
    conv = (
        await db.execute(
            select(Conversation.id).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise deps.http("not_found", "conversation not found", 404)
    row = (
        await db.execute(
            select(Completion.upstream_request)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user.id,
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Message.deleted_at.is_(None),
            )
            .order_by(desc(Completion.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    memory = row.get("memory") if isinstance(row, dict) else None
    if not isinstance(memory, dict):
        return UsedMemoriesOut()
    return await deps.filter_owned_used_memory_payload(
        db,
        user_id=user.id,
        ids=memory.get("used_memory_ids"),
        summary=memory.get("used_memory_summary"),
    )
