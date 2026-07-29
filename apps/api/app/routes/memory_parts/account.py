"""Account memory CRUD, staging, settings, and timeline implementations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import MemoryAudit, User, UserMemory, UserMemoryStaging

from .contracts import (
    MemoryAuditOut,
    MemoryCreateIn,
    MemoryListOut,
    MemoryOut,
    MemoryPatchIn,
    MemorySettingsOut,
    MemorySettingsPatchIn,
    MemoryStagingListOut,
    MemoryStagingOut,
    MemoryStagingPatchIn,
    MemoryTimelineOut,
    MemoryType,
    OnboardingSeenPatchIn,
)


Operation = Callable[..., Any]
AsyncOperation = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class AccountMemoryDependencies:
    http: Operation
    dml_rowcount: Operation
    memory_to_out: Operation
    staging_to_out: Operation
    owned_scope: AsyncOperation
    owned_memory: AsyncOperation
    owned_staging: AsyncOperation
    enqueue_memory_reembed: AsyncOperation
    audit: Operation
    build_memory_settings: AsyncOperation
    embedding_provider_available: AsyncOperation
    publish_account_settings_updated: AsyncOperation
    get_redis: Operation


async def get_memory_settings_impl(
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemorySettingsOut:
    return await deps.build_memory_settings(user, db)


async def patch_memory_settings_impl(
    body: MemorySettingsPatchIn,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemorySettingsOut:
    if body.disabled is False and bool(user.memory_disabled):
        if not await deps.embedding_provider_available(db):
            raise deps.http(
                "embedding_provider_required",
                "需要先在管理员后台为某个 provider 勾选 embedding 用途, 才能启用记忆功能.",
                422,
            )
    if body.paused is not None:
        user.memory_paused = body.paused
    if body.disabled is not None:
        user.memory_disabled = body.disabled
    if body.confirmation_enabled is not None:
        user.confirmation_enabled = body.confirmation_enabled
    await db.commit()
    await deps.publish_account_settings_updated(deps.get_redis(), user.id)
    return await deps.build_memory_settings(user, db)


async def patch_onboarding_seen_impl(
    body: OnboardingSeenPatchIn,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemorySettingsOut:
    user.onboarding_seen = int(user.onboarding_seen or 0) | (1 << body.flag)
    await db.commit()
    return await deps.build_memory_settings(user, db)


async def list_memories_impl(
    user: User,
    db: AsyncSession,
    *,
    type: MemoryType | None,
    pinned: bool | None,
    disabled: bool | None,
    scope_id: str | None,
    deps: AccountMemoryDependencies,
) -> MemoryListOut:
    stmt = select(UserMemory).where(UserMemory.user_id == user.id)
    if type is not None:
        stmt = stmt.where(UserMemory.type == type)
    if pinned is not None:
        stmt = stmt.where(UserMemory.pinned.is_(pinned))
    if disabled is not None:
        stmt = stmt.where(UserMemory.disabled.is_(disabled))
    if scope_id is not None:
        stmt = stmt.where(UserMemory.scope_id == scope_id)
    rows = (
        (
            await db.execute(
                stmt.order_by(desc(UserMemory.pinned), desc(UserMemory.updated_at))
            )
        )
        .scalars()
        .all()
    )
    return MemoryListOut(items=[deps.memory_to_out(memory) for memory in rows])


async def create_memory_impl(
    body: MemoryCreateIn,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemoryOut:
    scope = await deps.owned_scope(db, user.id, body.scope_id)
    memory = UserMemory(
        user_id=user.id,
        type=body.type,
        content=body.content.strip(),
        source_message_id=None,
        source_excerpt=body.source_excerpt,
        source="manual",
        embedding=None,
        confidence=1.0,
        pinned=body.pinned,
        scope_id=scope.id,
        last_used_at=datetime.now(timezone.utc),
    )
    db.add(memory)
    await db.flush()
    db.add(
        deps.audit(
            user_id=user.id,
            event_type="added",
            memory_id=memory.id,
            new_content=memory.content,
            details={"source": "manual"},
        )
    )
    await db.commit()
    await db.refresh(memory)
    await deps.enqueue_memory_reembed("memory", memory.id)
    return deps.memory_to_out(memory)


async def patch_memory_impl(
    memory_id: str,
    body: MemoryPatchIn,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemoryOut:
    memory = await deps.owned_memory(db, user.id, memory_id)
    old_content = memory.content
    content_changed = False
    if body.type is not None:
        memory.type = body.type
    if body.content is not None:
        new_content = body.content.strip()
        if new_content != memory.content:
            memory.content = new_content
            memory.embedding = None
            memory.positive_signal += 2
            content_changed = True
    if body.pinned is not None and body.pinned != memory.pinned:
        memory.pinned = body.pinned
        if body.pinned:
            memory.positive_signal += 1
    if body.disabled is not None and body.disabled != memory.disabled:
        memory.disabled = body.disabled
        if body.disabled:
            memory.negative_signal += 1
    if body.scope_id is not None:
        scope = await deps.owned_scope(db, user.id, body.scope_id)
        memory.scope_id = scope.id
    event = "updated" if old_content != memory.content else "settings_updated"
    db.add(
        deps.audit(
            user_id=user.id,
            event_type=event,
            memory_id=memory.id,
            old_content=old_content if old_content != memory.content else None,
            new_content=memory.content,
        )
    )
    await db.commit()
    await db.refresh(memory)
    if content_changed:
        await deps.enqueue_memory_reembed("memory", memory.id)
    return deps.memory_to_out(memory)


async def forget_memory_impl(
    memory_id: str,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> dict[str, bool]:
    memory = await deps.owned_memory(db, user.id, memory_id)
    memory.disabled = True
    memory.deleted_at = datetime.now(timezone.utc)
    memory.negative_signal += 2
    user.extraction_threshold = min(
        0.95, float(user.extraction_threshold or 0.80) + 0.02
    )
    db.add(
        deps.audit(
            user_id=user.id,
            event_type="forget",
            memory_id=memory.id,
            old_content=memory.content,
        )
    )
    await db.commit()
    return {"ok": True}


async def clear_memories_impl(
    user: User,
    db: AsyncSession,
    *,
    confirmation: str | None,
    deps: AccountMemoryDependencies,
) -> dict[str, int]:
    if (confirmation or "").strip().lower() != "yes":
        raise deps.http(
            "confirmation_required",
            "X-Confirm-Clear-Memory must be 'yes'",
            428,
        )
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(UserMemory)
        .where(UserMemory.user_id == user.id, UserMemory.disabled.is_(False))
        .values(disabled=True, deleted_at=now)
        .execution_options(synchronize_session=False)
    )
    deleted_count = int(deps.dml_rowcount(result) or 0)
    db.add(
        deps.audit(
            user_id=user.id,
            event_type="clear",
            details={"count": deleted_count},
        )
    )
    await db.commit()
    return {"deleted": deleted_count}


async def export_memories_impl(user: User, db: AsyncSession) -> dict[str, Any]:
    rows = (
        (
            await db.execute(
                select(UserMemory)
                .where(UserMemory.user_id == user.id)
                .order_by(UserMemory.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "type": memory.type,
                "content": memory.content,
                "source_excerpt": memory.source_excerpt,
                "created_at": memory.created_at.isoformat(),
            }
            for memory in rows
        ]
    }


async def list_memory_staging_impl(
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemoryStagingListOut:
    now = datetime.now(timezone.utc)
    rows = (
        (
            await db.execute(
                select(UserMemoryStaging)
                .where(
                    UserMemoryStaging.user_id == user.id,
                    UserMemoryStaging.decision == "pending",
                    UserMemoryStaging.expires_at > now,
                )
                .order_by(desc(UserMemoryStaging.created_at))
            )
        )
        .scalars()
        .all()
    )
    return MemoryStagingListOut(
        items=[deps.staging_to_out(staging) for staging in rows]
    )


async def patch_memory_staging_impl(
    staging_id: str,
    body: MemoryStagingPatchIn,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemoryStagingOut:
    row = await deps.owned_staging(db, user.id, staging_id)
    content_changed = False
    if body.type is not None:
        row.type = body.type
    if body.content is not None:
        new_content = body.content.strip()
        if new_content != row.content:
            row.content = new_content
            row.embedding = None
            content_changed = True
    if body.scope_id is not None:
        row.scope_id = (await deps.owned_scope(db, user.id, body.scope_id)).id
    await db.commit()
    await db.refresh(row)
    if content_changed:
        await deps.enqueue_memory_reembed("staging", row.id)
    return deps.staging_to_out(row)


async def accept_memory_staging_impl(
    staging_id: str,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> MemoryOut:
    row = await deps.owned_staging(db, user.id, staging_id)
    if row.decision != "pending":
        raise deps.http("already_decided", "staging memory already decided", 409)
    memory = UserMemory(
        user_id=user.id,
        type=row.type,
        content=row.content,
        source_message_id=row.source_message_id,
        source_excerpt=row.source_excerpt,
        source="auto",
        embedding=row.embedding,
        confidence=max(row.confidence, 0.85),
        scope_id=row.scope_id,
        last_used_at=datetime.now(timezone.utc),
    )
    db.add(memory)
    row.decision = "accepted"
    row.decided_at = datetime.now(timezone.utc)
    await db.flush()
    needs_reembed = memory.embedding is None
    db.add(
        deps.audit(
            user_id=user.id,
            event_type="added",
            memory_id=memory.id,
            staging_id=row.id,
            new_content=memory.content,
            source_message_id=row.source_message_id,
        )
    )
    await db.commit()
    await db.refresh(memory)
    if needs_reembed:
        await deps.enqueue_memory_reembed("memory", memory.id)
    return deps.memory_to_out(memory)


async def reject_memory_staging_impl(
    staging_id: str,
    user: User,
    db: AsyncSession,
    *,
    deps: AccountMemoryDependencies,
) -> dict[str, bool]:
    row = await deps.owned_staging(db, user.id, staging_id)
    row.decision = "rejected"
    row.decided_at = datetime.now(timezone.utc)
    db.add(deps.audit(user_id=user.id, event_type="reject", staging_id=row.id))
    await db.commit()
    return {"ok": True}


async def memory_timeline_impl(
    user: User,
    db: AsyncSession,
    *,
    cursor: str | None,
    limit: int,
) -> MemoryTimelineOut:
    stmt = select(MemoryAudit).where(MemoryAudit.user_id == user.id)
    if cursor:
        try:
            cur_dt = datetime.fromisoformat(cursor)
            stmt = stmt.where(MemoryAudit.created_at < cur_dt)
        except ValueError:
            pass
    rows = (
        (await db.execute(stmt.order_by(desc(MemoryAudit.created_at)).limit(limit + 1)))
        .scalars()
        .all()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return MemoryTimelineOut(
        items=[
            MemoryAuditOut(
                id=audit.id,
                event_type=audit.event_type,
                memory_id=audit.memory_id,
                staging_id=audit.staging_id,
                old_content=audit.old_content,
                new_content=audit.new_content,
                source_message_id=audit.source_message_id,
                details=audit.details,
                created_at=audit.created_at,
            )
            for audit in rows
        ],
        next_cursor=rows[-1].created_at.isoformat() if has_more and rows else None,
    )


async def cleanup_expired_staging_impl(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    rows = (
        (
            await db.execute(
                select(UserMemoryStaging).where(
                    UserMemoryStaging.decision == "pending",
                    UserMemoryStaging.expires_at < now,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.decision = "rejected"
        row.decided_at = now
    await db.commit()
    return len(rows)
