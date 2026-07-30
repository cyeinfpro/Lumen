"""Shared memory route queries, serialization, and event helpers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import conv_channel, user_channel
from lumen_core.model_entities import (
    MemoryAudit,
    User,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)

from .contracts import MemoryOut, MemorySettingsOut, MemoryStagingOut, UsedMemoriesOut


def http_error(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


async def filter_owned_used_memory_payload(
    db: AsyncSession,
    *,
    user_id: str,
    ids: object,
    summary: object,
) -> UsedMemoriesOut:
    if not isinstance(ids, list):
        return UsedMemoriesOut()
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for value in ids:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        ordered_ids.append(value)
    if not ordered_ids:
        return UsedMemoriesOut()
    owned = set(
        (
            await db.execute(
                select(UserMemory.id).where(
                    UserMemory.id.in_(ordered_ids),
                    UserMemory.user_id == user_id,
                    or_(UserMemory.disabled.is_(False), UserMemory.disabled.is_(None)),
                    UserMemory.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    filtered_ids = [memory_id for memory_id in ordered_ids if memory_id in owned]
    if not filtered_ids:
        return UsedMemoriesOut()
    filtered_id_set = set(filtered_ids)
    filtered_summary: list[dict[str, str]] = []
    if isinstance(summary, list):
        for item in summary:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if not isinstance(item_id, str) or item_id not in filtered_id_set:
                continue
            filtered_summary.append(
                {k: str(v) for k, v in item.items() if k in {"id", "type", "content"}}
            )
    return UsedMemoriesOut(
        used_memory_ids=filtered_ids,
        used_memory_summary=filtered_summary,
    )


def memory_to_out(memory: UserMemory) -> MemoryOut:
    return MemoryOut(
        id=memory.id,
        type=memory.type,  # type: ignore[arg-type]
        content=memory.content,
        source_message_id=memory.source_message_id,
        source_excerpt=memory.source_excerpt,
        source=memory.source,  # type: ignore[arg-type]
        confidence=memory.confidence,
        pinned=memory.pinned,
        disabled=memory.disabled,
        positive_signal=memory.positive_signal,
        negative_signal=memory.negative_signal,
        superseded_by=memory.superseded_by,
        last_used_at=memory.last_used_at,
        scope_id=memory.scope_id,
        last_confirmed_at=memory.last_confirmed_at,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


def staging_to_out(staging: UserMemoryStaging) -> MemoryStagingOut:
    return MemoryStagingOut(
        id=staging.id,
        type=staging.type,  # type: ignore[arg-type]
        content=staging.content,
        source_message_id=staging.source_message_id,
        source_excerpt=staging.source_excerpt,
        confidence=staging.confidence,
        scope_id=staging.scope_id,
        recommended_scope_id=staging.recommended_scope_id,
        decision=staging.decision,  # type: ignore[arg-type]
        expires_at=staging.expires_at,
        created_at=staging.created_at,
    )


async def default_scope(db: AsyncSession, user_id: str) -> UserMemoryScope:
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


async def owned_scope(
    db: AsyncSession,
    user_id: str,
    scope_id: str | None,
    *,
    default_scope_fn: Callable[..., Awaitable[UserMemoryScope]],
    http: Callable[..., HTTPException],
) -> UserMemoryScope:
    if not scope_id:
        return await default_scope_fn(db, user_id)
    scope = (
        await db.execute(
            select(UserMemoryScope).where(
                UserMemoryScope.id == scope_id,
                UserMemoryScope.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if scope is None:
        raise http("scope_not_found", "memory scope not found", 404)
    return scope


async def owned_memory(
    db: AsyncSession,
    user_id: str,
    memory_id: str,
    *,
    http: Callable[..., HTTPException],
) -> UserMemory:
    memory = (
        await db.execute(
            select(UserMemory).where(
                UserMemory.id == memory_id, UserMemory.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if memory is None:
        raise http("not_found", "memory not found", 404)
    return memory


async def owned_staging(
    db: AsyncSession,
    user_id: str,
    staging_id: str,
    *,
    http: Callable[..., HTTPException],
) -> UserMemoryStaging:
    row = (
        await db.execute(
            select(UserMemoryStaging).where(
                UserMemoryStaging.id == staging_id,
                UserMemoryStaging.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise http("not_found", "staging memory not found", 404)
    return row


def audit(
    *,
    user_id: str,
    event_type: str,
    memory_id: str | None = None,
    staging_id: str | None = None,
    old_content: str | None = None,
    new_content: str | None = None,
    source_message_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> MemoryAudit:
    return MemoryAudit(
        user_id=user_id,
        memory_id=memory_id,
        staging_id=staging_id,
        event_type=event_type,
        old_content=old_content,
        new_content=new_content,
        source_message_id=source_message_id,
        details=details or {},
    )


async def publish_account_settings_updated(
    redis: Any,
    user_id: str,
    *,
    publish_event: Callable[..., Awaitable[Any]],
) -> None:
    try:
        await publish_event(
            redis,
            user_id=user_id,
            channel=user_channel(user_id),
            event_name="account_settings_updated",
            data={"user_id": user_id},
        )
    except Exception:
        return


async def publish_conversation_memory_updated(
    redis: Any,
    *,
    user_id: str,
    conversation_id: str,
    payload: dict[str, Any],
    publish_event: Callable[..., Awaitable[Any]],
) -> None:
    try:
        await publish_event(
            redis,
            user_id=user_id,
            channel=conv_channel(conversation_id),
            event_name="conversation.memory.updated",
            data=payload,
        )
    except Exception:
        return


async def disable_memory_for_conversation(
    redis: Any, conversation_id: str, memory_id: str
) -> None:
    try:
        key = f"memory:conversation:{conversation_id}:disabled"
        pipe = redis.pipeline(transaction=False)
        pipe.sadd(key, memory_id)
        pipe.expire(key, 30 * 24 * 60 * 60)
        await pipe.execute()
    except Exception:
        return


async def build_memory_settings(
    user: User,
    db: AsyncSession,
    *,
    embedding_available: Callable[..., Awaitable[bool]],
) -> MemorySettingsOut:
    available = await embedding_available(db)
    return MemorySettingsOut(
        paused=bool(user.memory_paused),
        disabled=bool(user.memory_disabled),
        extraction_threshold=float(user.extraction_threshold),
        onboarding_seen=int(user.onboarding_seen),
        confirmation_enabled=bool(user.confirmation_enabled),
        embedding_available=available,
    )
