"""Memory scope and confirmation route implementations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Conversation, User, UserMemory, UserMemoryScope

from .contracts import (
    ConversationActiveScopeIn,
    MemoryConfirmIn,
    MemoryOut,
    MemoryScopeCreateIn,
    MemoryScopeOut,
    MemoryScopePatchIn,
)


Operation = Callable[..., Any]
AsyncOperation = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class MemoryScopeDependencies:
    http: Operation
    dml_rowcount: Operation
    default_scope: AsyncOperation
    owned_scope: AsyncOperation
    owned_memory: AsyncOperation
    memory_to_out: Operation
    audit: Operation
    disable_memory_for_conversation: AsyncOperation
    publish_account_settings_updated: AsyncOperation
    get_redis: Operation


async def list_memory_scopes_impl(
    user: User,
    db: AsyncSession,
) -> list[MemoryScopeOut]:
    rows = (
        await db.execute(
            select(UserMemoryScope, func.count(UserMemory.id))
            .outerjoin(UserMemory, UserMemory.scope_id == UserMemoryScope.id)
            .where(UserMemoryScope.user_id == user.id)
            .group_by(UserMemoryScope.id)
            .order_by(
                desc(UserMemoryScope.is_default), UserMemoryScope.created_at.asc()
            )
        )
    ).all()
    return [
        MemoryScopeOut(
            id=scope.id,
            name=scope.name,
            emoji=scope.emoji,
            is_default=scope.is_default,
            count=int(count or 0),
            created_at=scope.created_at,
        )
        for scope, count in rows
    ]


async def create_memory_scope_impl(
    body: MemoryScopeCreateIn,
    user: User,
    db: AsyncSession,
    *,
    deps: MemoryScopeDependencies,
) -> MemoryScopeOut:
    scope = UserMemoryScope(
        user_id=user.id,
        name=body.name.strip(),
        emoji=(body.emoji or "").strip() or None,
        is_default=False,
    )
    db.add(scope)
    await db.commit()
    await db.refresh(scope)
    await deps.publish_account_settings_updated(deps.get_redis(), user.id)
    return MemoryScopeOut(
        id=scope.id,
        name=scope.name,
        emoji=scope.emoji,
        is_default=False,
        count=0,
        created_at=scope.created_at,
    )


async def patch_memory_scope_impl(
    scope_id: str,
    body: MemoryScopePatchIn,
    user: User,
    db: AsyncSession,
    *,
    deps: MemoryScopeDependencies,
) -> MemoryScopeOut:
    scope = await deps.owned_scope(db, user.id, scope_id)
    if body.name is not None:
        scope.name = body.name.strip()
    if body.emoji is not None:
        scope.emoji = body.emoji.strip() or None
    await db.commit()
    await db.refresh(scope)
    await deps.publish_account_settings_updated(deps.get_redis(), user.id)
    count = (
        await db.execute(
            select(func.count(UserMemory.id)).where(UserMemory.scope_id == scope.id)
        )
    ).scalar_one()
    return MemoryScopeOut(
        id=scope.id,
        name=scope.name,
        emoji=scope.emoji,
        is_default=scope.is_default,
        count=int(count or 0),
        created_at=scope.created_at,
    )


async def delete_memory_scope_impl(
    scope_id: str,
    user: User,
    db: AsyncSession,
    *,
    deps: MemoryScopeDependencies,
) -> dict[str, int]:
    scope = await deps.owned_scope(db, user.id, scope_id)
    if scope.is_default:
        raise deps.http(
            "cannot_delete_default", "default memory scope cannot be deleted", 422
        )
    default = await deps.default_scope(db, user.id)
    result = await db.execute(
        update(UserMemory)
        .where(UserMemory.scope_id == scope.id)
        .values(scope_id=default.id)
        .execution_options(synchronize_session=False)
    )
    moved_count = int(deps.dml_rowcount(result) or 0)
    await db.delete(scope)
    await db.commit()
    await deps.publish_account_settings_updated(deps.get_redis(), user.id)
    return {"moved": moved_count}


async def patch_memory_scope_assignment_impl(
    memory_id: str,
    body: ConversationActiveScopeIn,
    user: User,
    db: AsyncSession,
    *,
    deps: MemoryScopeDependencies,
) -> MemoryOut:
    memory = await deps.owned_memory(db, user.id, memory_id)
    memory.scope_id = (await deps.owned_scope(db, user.id, body.scope_id)).id
    await db.commit()
    await db.refresh(memory)
    return deps.memory_to_out(memory)


async def confirm_memory_impl(
    memory_id: str,
    body: MemoryConfirmIn,
    user: User,
    db: AsyncSession,
    *,
    deps: MemoryScopeDependencies,
) -> MemoryOut:
    memory = await deps.owned_memory(db, user.id, memory_id)
    conversation_id: str | None = None
    if body.conversation_id:
        conversation = (
            await db.execute(
                select(Conversation.id).where(
                    Conversation.id == body.conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            raise deps.http("conversation_not_found", "conversation not found", 404)
        conversation_id = conversation
    if body.decision == "yes":
        memory.positive_signal += 1
        memory.last_confirmed_at = datetime.now(timezone.utc)
    elif body.decision == "no":
        memory.negative_signal += 2
        memory.last_confirmed_at = datetime.now(timezone.utc)
        if conversation_id:
            await deps.disable_memory_for_conversation(
                deps.get_redis(), conversation_id, memory.id
            )
    else:
        memory.last_confirmed_at = datetime.now(timezone.utc)
    db.add(
        deps.audit(
            user_id=user.id,
            event_type=f"confirm_{body.decision}",
            memory_id=memory.id,
            new_content=memory.content,
            details={"conversation_id": conversation_id} if conversation_id else None,
        )
    )
    await db.commit()
    await db.refresh(memory)
    return deps.memory_to_out(memory)
