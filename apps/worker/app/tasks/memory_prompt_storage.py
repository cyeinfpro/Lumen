"""Database reads used while assembling account-memory prompts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from lumen_core.models import Conversation, UserMemory, UserMemoryScope

from .memory_extraction_values import conversation_disabled_memory_ids


async def _default_scope(session: Any, user_id: str) -> UserMemoryScope:
    scope = (
        await session.execute(
            select(UserMemoryScope).where(
                UserMemoryScope.user_id == user_id,
                UserMemoryScope.is_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if scope is not None:
        return scope
    scope = UserMemoryScope(user_id=user_id, name="default", is_default=True)
    session.add(scope)
    await session.flush()
    return scope


async def _memory_scope_context(
    session: Any,
    *,
    user_id: str,
    conversation: Conversation,
) -> tuple[set[str], UserMemoryScope | None]:
    default_scope = await _default_scope(session, user_id)
    scope_ids = {default_scope.id}
    active_scope = None
    if conversation.active_scope_id:
        scope_ids.add(conversation.active_scope_id)
        active_scope = await session.get(
            UserMemoryScope,
            conversation.active_scope_id,
        )
    return scope_ids, active_scope


async def _prompt_memory_rows(
    session: Any,
    *,
    user_id: str,
    conversation_id: str,
    scope_ids: set[str],
    redis: Any | None,
) -> list[UserMemory]:
    rows = list(
        (
            await session.execute(
                select(UserMemory).where(
                    UserMemory.user_id == user_id,
                    UserMemory.disabled.is_(False),
                    UserMemory.superseded_by.is_(None),
                    UserMemory.scope_id.in_(scope_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    disabled_ids = await conversation_disabled_memory_ids(redis, conversation_id)
    if not disabled_ids:
        return rows
    return [memory for memory in rows if memory.id not in disabled_ids]
