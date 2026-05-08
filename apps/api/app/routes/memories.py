"""User-owned account memory APIs."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import EVENTS_STREAM_PREFIX, conv_channel
from lumen_core.memory import MEMORY_TYPES
from lumen_core.models import (
    Completion,
    Conversation,
    MemoryAudit,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..runtime_settings import embedding_provider_available


router = APIRouter(tags=["memories"])
logger = logging.getLogger(__name__)
_UNDO_TTL_SECONDS = 300
_STAGING_TTL_DAYS = 7


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


MemoryType = Literal["profile", "preference", "avoid", "project"]


class MemoryScopeOut(BaseModel):
    id: str
    name: str
    emoji: str | None = None
    is_default: bool
    count: int = 0
    created_at: datetime


class MemoryOut(BaseModel):
    id: str
    type: MemoryType
    content: str
    source_message_id: str | None = None
    source_excerpt: str | None = None
    source: Literal["explicit", "auto", "manual"]
    confidence: float
    pinned: bool
    disabled: bool
    positive_signal: int
    negative_signal: int
    superseded_by: str | None = None
    last_used_at: datetime | None = None
    scope_id: str
    last_confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MemoryListOut(BaseModel):
    items: list[MemoryOut]


class MemoryCreateIn(BaseModel):
    type: MemoryType
    content: str = Field(min_length=1, max_length=200)
    source_excerpt: str | None = Field(default=None, max_length=160)
    pinned: bool = False
    scope_id: str | None = None


class MemoryPatchIn(BaseModel):
    type: MemoryType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    disabled: bool | None = None
    scope_id: str | None = None


class MemorySettingsOut(BaseModel):
    paused: bool
    disabled: bool
    extraction_threshold: float
    onboarding_seen: int
    confirmation_enabled: bool
    embedding_available: bool


class MemorySettingsPatchIn(BaseModel):
    paused: bool | None = None
    disabled: bool | None = None
    confirmation_enabled: bool | None = None


class OnboardingSeenPatchIn(BaseModel):
    flag: int = Field(ge=0, le=30)


class MemoryStagingOut(BaseModel):
    id: str
    type: MemoryType
    content: str
    source_message_id: str | None = None
    source_excerpt: str | None = None
    confidence: float
    scope_id: str
    recommended_scope_id: str | None = None
    decision: Literal["pending", "accepted", "rejected"]
    expires_at: datetime
    created_at: datetime


class MemoryStagingListOut(BaseModel):
    items: list[MemoryStagingOut]


class MemoryStagingPatchIn(BaseModel):
    type: MemoryType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=200)
    scope_id: str | None = None


class MemoryUndoIn(BaseModel):
    undo_token: str


class MemoryAuditOut(BaseModel):
    id: str
    event_type: str
    memory_id: str | None = None
    staging_id: str | None = None
    old_content: str | None = None
    new_content: str | None = None
    source_message_id: str | None = None
    details: dict[str, Any]
    created_at: datetime


class MemoryTimelineOut(BaseModel):
    items: list[MemoryAuditOut]
    next_cursor: str | None = None


class MemoryScopeCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)


class MemoryScopePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)


class MemoryConfirmIn(BaseModel):
    decision: Literal["yes", "no", "skip"]
    conversation_id: str | None = None


class ConversationMemoryDisabledIn(BaseModel):
    disabled: bool


class ConversationActiveScopeIn(BaseModel):
    scope_id: str | None = None


class UsedMemoriesOut(BaseModel):
    used_memory_ids: list[str] = []
    used_memory_summary: list[dict[str, str]] = []


def _memory_to_out(m: UserMemory) -> MemoryOut:
    return MemoryOut(
        id=m.id,
        type=m.type,  # type: ignore[arg-type]
        content=m.content,
        source_message_id=m.source_message_id,
        source_excerpt=m.source_excerpt,
        source=m.source,  # type: ignore[arg-type]
        confidence=m.confidence,
        pinned=m.pinned,
        disabled=m.disabled,
        positive_signal=m.positive_signal,
        negative_signal=m.negative_signal,
        superseded_by=m.superseded_by,
        last_used_at=m.last_used_at,
        scope_id=m.scope_id,
        last_confirmed_at=m.last_confirmed_at,
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _staging_to_out(s: UserMemoryStaging) -> MemoryStagingOut:
    return MemoryStagingOut(
        id=s.id,
        type=s.type,  # type: ignore[arg-type]
        content=s.content,
        source_message_id=s.source_message_id,
        source_excerpt=s.source_excerpt,
        confidence=s.confidence,
        scope_id=s.scope_id,
        recommended_scope_id=s.recommended_scope_id,
        decision=s.decision,  # type: ignore[arg-type]
        expires_at=s.expires_at,
        created_at=s.created_at,
    )


async def _default_scope(db: AsyncSession, user_id: str) -> UserMemoryScope:
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


async def _owned_scope(
    db: AsyncSession, user_id: str, scope_id: str | None
) -> UserMemoryScope:
    if not scope_id:
        return await _default_scope(db, user_id)
    scope = (
        await db.execute(
            select(UserMemoryScope).where(
                UserMemoryScope.id == scope_id,
                UserMemoryScope.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if scope is None:
        raise _http("scope_not_found", "memory scope not found", 404)
    return scope


async def _owned_memory(db: AsyncSession, user_id: str, memory_id: str) -> UserMemory:
    memory = (
        await db.execute(
            select(UserMemory).where(UserMemory.id == memory_id, UserMemory.user_id == user_id)
        )
    ).scalar_one_or_none()
    if memory is None:
        raise _http("not_found", "memory not found", 404)
    return memory


async def _enqueue_memory_reembed(target: str, row_id: str) -> None:
    """Schedule a worker job to compute the real LLM embedding.

    API CRUD writes embedding=NULL and lets the worker fill it in via the
    embedding-purpose provider pool. Keeps API path latency bounded and avoids
    the deterministic-vs-real mismatch that previously made manual entries
    invisible to retrieval.
    """
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


def _audit(
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


async def _publish_account_settings_updated(redis: Any, user_id: str) -> None:
    data = json.dumps(
        {"event": "account_settings_updated", "data": {"user_id": user_id}},
        separators=(",", ":"),
    )
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.publish(f"user:{user_id}", data)
        pipe.xadd(
            f"{EVENTS_STREAM_PREFIX}{user_id}",
            {"event": "account_settings_updated", "data": data},
            maxlen=10000,
            approximate=True,
        )
        await pipe.execute()
    except Exception:
        return


async def _publish_conversation_memory_updated(
    redis: Any,
    *,
    user_id: str,
    conversation_id: str,
    payload: dict[str, Any],
) -> None:
    data = json.dumps(
        {"event": "conversation.memory.updated", "data": payload},
        separators=(",", ":"),
    )
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.publish(conv_channel(conversation_id), data)
        pipe.xadd(
            f"{EVENTS_STREAM_PREFIX}{user_id}",
            {"event": "conversation.memory.updated", "data": data},
            maxlen=10000,
            approximate=True,
        )
        await pipe.execute()
    except Exception:
        return


async def _disable_memory_for_conversation(
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


async def _make_undo_token(redis: Any, payload: dict[str, Any]) -> str | None:
    token = secrets.token_urlsafe(24)
    try:
        await redis.setex(
            f"memory:undo:{token}",
            _UNDO_TTL_SECONDS,
            json.dumps(payload, separators=(",", ":")),
        )
        return token
    except Exception:
        return None


async def _build_memory_settings(
    user: User, db: AsyncSession
) -> MemorySettingsOut:
    available = await embedding_provider_available(db)
    return MemorySettingsOut(
        paused=bool(user.memory_paused),
        disabled=bool(user.memory_disabled),
        extraction_threshold=float(user.extraction_threshold),
        onboarding_seen=int(user.onboarding_seen),
        confirmation_enabled=bool(user.confirmation_enabled),
        embedding_available=available,
    )


@router.get("/me/memory-settings", response_model=MemorySettingsOut)
async def get_memory_settings(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemorySettingsOut:
    return await _build_memory_settings(user, db)


@router.patch(
    "/me/memory-settings",
    response_model=MemorySettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_settings(
    body: MemorySettingsPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemorySettingsOut:
    # 如果当前没 embedding provider, 拒绝把 disabled 翻成 false (即"启用").
    # 已经 disabled=true 的允许继续 patch (例如改 paused/confirmation), 但
    # 不允许重开. 这避免了 UI 看到"开启"了却根本不能写入记忆的状态.
    if body.disabled is False and bool(user.memory_disabled):
        if not await embedding_provider_available(db):
            raise _http(
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
    await _publish_account_settings_updated(get_redis(), user.id)
    return await _build_memory_settings(user, db)


@router.patch(
    "/me/onboarding-seen",
    response_model=MemorySettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_onboarding_seen(
    body: OnboardingSeenPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemorySettingsOut:
    user.onboarding_seen = int(user.onboarding_seen or 0) | (1 << body.flag)
    await db.commit()
    return await _build_memory_settings(user, db)


@router.get("/me/memories", response_model=MemoryListOut)
async def list_memories(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: MemoryType | None = None,
    pinned: bool | None = None,
    disabled: bool | None = None,
    scope_id: str | None = None,
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
        await db.execute(stmt.order_by(desc(UserMemory.pinned), desc(UserMemory.updated_at)))
    ).scalars().all()
    return MemoryListOut(items=[_memory_to_out(m) for m in rows])


@router.post(
    "/me/memories",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_memory(
    body: MemoryCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    scope = await _owned_scope(db, user.id, body.scope_id)
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
        _audit(
            user_id=user.id,
            event_type="added",
            memory_id=memory.id,
            new_content=memory.content,
            details={"source": "manual"},
        )
    )
    await db.commit()
    await db.refresh(memory)
    await _enqueue_memory_reembed("memory", memory.id)
    return _memory_to_out(memory)


@router.patch(
    "/me/memories/{memory_id}",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory(
    memory_id: str,
    body: MemoryPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    memory = await _owned_memory(db, user.id, memory_id)
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
        scope = await _owned_scope(db, user.id, body.scope_id)
        memory.scope_id = scope.id
    event = "updated" if old_content != memory.content else "settings_updated"
    db.add(
        _audit(
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
        await _enqueue_memory_reembed("memory", memory.id)
    return _memory_to_out(memory)


@router.delete(
    "/me/memories/{memory_id}",
    dependencies=[Depends(verify_csrf)],
)
async def forget_memory(
    memory_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    memory = await _owned_memory(db, user.id, memory_id)
    memory.disabled = True
    memory.deleted_at = datetime.now(timezone.utc)
    memory.negative_signal += 2
    user.extraction_threshold = min(0.95, float(user.extraction_threshold or 0.80) + 0.02)
    db.add(
        _audit(
            user_id=user.id,
            event_type="forget",
            memory_id=memory.id,
            old_content=memory.content,
        )
    )
    await db.commit()
    return {"ok": True}


@router.delete(
    "/me/memories",
    dependencies=[Depends(verify_csrf)],
)
async def clear_memories(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    confirmation: Annotated[str | None, Header(alias="X-Confirm-Clear-Memory")] = None,
) -> dict[str, int]:
    if (confirmation or "").strip().lower() != "yes":
        raise _http(
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
    db.add(_audit(user_id=user.id, event_type="clear", details={"count": result.rowcount or 0}))
    await db.commit()
    return {"deleted": int(result.rowcount or 0)}


@router.get("/me/memories/export")
async def export_memories(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user.id)
            .order_by(UserMemory.created_at.asc())
        )
    ).scalars().all()
    return {
        "items": [
            {
                "type": m.type,
                "content": m.content,
                "source_excerpt": m.source_excerpt,
                "created_at": m.created_at.isoformat(),
            }
            for m in rows
        ]
    }


@router.post(
    "/me/memories/undo",
    dependencies=[Depends(verify_csrf)],
)
async def undo_memory_write(
    body: MemoryUndoIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    redis = get_redis()
    raw = await redis.get(f"memory:undo:{body.undo_token}")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        raise _http("undo_expired", "undo token expired", 410)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _http("undo_expired", "undo token expired", 410) from exc
    if payload.get("user_id") != user.id:
        raise _http("forbidden", "undo token does not belong to this user", 403)
    action = payload.get("action")
    memory_id = payload.get("memory_id")
    reembed_id: str | None = None
    if action in {"added", "updated"} and isinstance(memory_id, str):
        memory = await _owned_memory(db, user.id, memory_id)
        memory.disabled = True
        db.add(_audit(user_id=user.id, event_type="undo", memory_id=memory.id))
    elif action == "merged" and isinstance(memory_id, str):
        # 设计 §5.4: 撤销 merged 不是删 duplicate, 而是把这次合并掉的 candidate
        # 拆成独立条, 同时把 duplicate.positive_signal 减回去 (合并时 +=1).
        duplicate = await _owned_memory(db, user.id, memory_id)
        duplicate.positive_signal = max(0, duplicate.positive_signal - 1)
        candidate = payload.get("candidate")
        if isinstance(candidate, dict):
            cand_type = candidate.get("type")
            cand_content = candidate.get("content")
            if (
                cand_type in {"profile", "preference", "avoid", "project"}
                and isinstance(cand_content, str)
                and cand_content.strip()
            ):
                cand_scope_id = candidate.get("scope_id") or duplicate.scope_id
                scope = await _owned_scope(db, user.id, cand_scope_id)
                cand_source = candidate.get("source")
                if cand_source not in {"explicit", "auto", "manual"}:
                    cand_source = "auto"
                independent = UserMemory(
                    user_id=user.id,
                    type=cand_type,
                    content=cand_content.strip(),
                    source_message_id=(
                        candidate.get("source_message_id")
                        if isinstance(candidate.get("source_message_id"), str)
                        else None
                    ),
                    source_excerpt=(
                        candidate.get("source_excerpt")
                        if isinstance(candidate.get("source_excerpt"), str)
                        else None
                    ),
                    source=cand_source,
                    embedding=None,
                    confidence=float(candidate.get("confidence") or 0.80),
                    scope_id=scope.id,
                    last_used_at=datetime.now(timezone.utc),
                )
                db.add(independent)
                await db.flush()
                reembed_id = independent.id
                db.add(
                    _audit(
                        user_id=user.id,
                        event_type="undo_merged",
                        memory_id=independent.id,
                        new_content=independent.content,
                        details={"merged_into": duplicate.id},
                    )
                )
        db.add(
            _audit(
                user_id=user.id,
                event_type="undo",
                memory_id=duplicate.id,
                details={"action": "merged"},
            )
        )
    elif action == "superseded" and isinstance(memory_id, str):
        memory = await _owned_memory(db, user.id, memory_id)
        old_id = payload.get("old_memory_id")
        memory.disabled = True
        if isinstance(old_id, str):
            old = await _owned_memory(db, user.id, old_id)
            old.superseded_by = None
        db.add(_audit(user_id=user.id, event_type="undo", memory_id=memory.id))
    elif action == "staged":
        staging_id = payload.get("staging_id")
        if isinstance(staging_id, str):
            row = await db.get(UserMemoryStaging, staging_id)
            if row is not None and row.user_id == user.id:
                row.decision = "rejected"
                row.decided_at = datetime.now(timezone.utc)
    await redis.delete(f"memory:undo:{body.undo_token}")
    await db.commit()
    if reembed_id:
        await _enqueue_memory_reembed("memory", reembed_id)
    return {"ok": True}


@router.get("/me/memories/staging", response_model=MemoryStagingListOut)
async def list_memory_staging(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryStagingListOut:
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(UserMemoryStaging)
            .where(
                UserMemoryStaging.user_id == user.id,
                UserMemoryStaging.decision == "pending",
                UserMemoryStaging.expires_at > now,
            )
            .order_by(desc(UserMemoryStaging.created_at))
        )
    ).scalars().all()
    return MemoryStagingListOut(items=[_staging_to_out(s) for s in rows])


async def _owned_staging(db: AsyncSession, user_id: str, staging_id: str) -> UserMemoryStaging:
    row = (
        await db.execute(
            select(UserMemoryStaging).where(
                UserMemoryStaging.id == staging_id,
                UserMemoryStaging.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "staging memory not found", 404)
    return row


@router.patch(
    "/me/memories/staging/{staging_id}",
    response_model=MemoryStagingOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_staging(
    staging_id: str,
    body: MemoryStagingPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryStagingOut:
    row = await _owned_staging(db, user.id, staging_id)
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
        row.scope_id = (await _owned_scope(db, user.id, body.scope_id)).id
    await db.commit()
    await db.refresh(row)
    if content_changed:
        await _enqueue_memory_reembed("staging", row.id)
    return _staging_to_out(row)


@router.post(
    "/me/memories/staging/{staging_id}/accept",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def accept_memory_staging(
    staging_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    row = await _owned_staging(db, user.id, staging_id)
    if row.decision != "pending":
        raise _http("already_decided", "staging memory already decided", 409)
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
        _audit(
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
        await _enqueue_memory_reembed("memory", memory.id)
    return _memory_to_out(memory)


@router.post(
    "/me/memories/staging/{staging_id}/reject",
    dependencies=[Depends(verify_csrf)],
)
async def reject_memory_staging(
    staging_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    row = await _owned_staging(db, user.id, staging_id)
    row.decision = "rejected"
    row.decided_at = datetime.now(timezone.utc)
    db.add(_audit(user_id=user.id, event_type="reject", staging_id=row.id))
    await db.commit()
    return {"ok": True}


@router.get("/me/memories/timeline", response_model=MemoryTimelineOut)
async def memory_timeline(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> MemoryTimelineOut:
    stmt = select(MemoryAudit).where(MemoryAudit.user_id == user.id)
    if cursor:
        try:
            cur_dt = datetime.fromisoformat(cursor)
            stmt = stmt.where(MemoryAudit.created_at < cur_dt)
        except ValueError:
            pass
    rows = (
        await db.execute(stmt.order_by(desc(MemoryAudit.created_at)).limit(limit + 1))
    ).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return MemoryTimelineOut(
        items=[
            MemoryAuditOut(
                id=a.id,
                event_type=a.event_type,
                memory_id=a.memory_id,
                staging_id=a.staging_id,
                old_content=a.old_content,
                new_content=a.new_content,
                source_message_id=a.source_message_id,
                details=a.details,
                created_at=a.created_at,
            )
            for a in rows
        ],
        next_cursor=rows[-1].created_at.isoformat() if has_more and rows else None,
    )


@router.get("/me/memory-scopes", response_model=list[MemoryScopeOut])
async def list_memory_scopes(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MemoryScopeOut]:
    # GET 保持只读;default scope 由 migration + INSERT trigger 保证存在,
    # 极端 race 下空列表由前端重试覆盖。
    rows = (
        await db.execute(
            select(UserMemoryScope, func.count(UserMemory.id))
            .outerjoin(UserMemory, UserMemory.scope_id == UserMemoryScope.id)
            .where(UserMemoryScope.user_id == user.id)
            .group_by(UserMemoryScope.id)
            .order_by(desc(UserMemoryScope.is_default), UserMemoryScope.created_at.asc())
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


@router.post(
    "/me/memory-scopes",
    response_model=MemoryScopeOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_memory_scope(
    body: MemoryScopeCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
    await _publish_account_settings_updated(get_redis(), user.id)
    return MemoryScopeOut(
        id=scope.id,
        name=scope.name,
        emoji=scope.emoji,
        is_default=False,
        count=0,
        created_at=scope.created_at,
    )


@router.patch(
    "/me/memory-scopes/{scope_id}",
    response_model=MemoryScopeOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_scope(
    scope_id: str,
    body: MemoryScopePatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryScopeOut:
    scope = await _owned_scope(db, user.id, scope_id)
    if body.name is not None:
        scope.name = body.name.strip()
    if body.emoji is not None:
        scope.emoji = body.emoji.strip() or None
    await db.commit()
    await db.refresh(scope)
    await _publish_account_settings_updated(get_redis(), user.id)
    count = (
        await db.execute(select(func.count(UserMemory.id)).where(UserMemory.scope_id == scope.id))
    ).scalar_one()
    return MemoryScopeOut(
        id=scope.id,
        name=scope.name,
        emoji=scope.emoji,
        is_default=scope.is_default,
        count=int(count or 0),
        created_at=scope.created_at,
    )


@router.delete(
    "/me/memory-scopes/{scope_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_memory_scope(
    scope_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    scope = await _owned_scope(db, user.id, scope_id)
    if scope.is_default:
        raise _http("cannot_delete_default", "default memory scope cannot be deleted", 422)
    default = await _default_scope(db, user.id)
    result = await db.execute(
        update(UserMemory)
        .where(UserMemory.scope_id == scope.id)
        .values(scope_id=default.id)
        .execution_options(synchronize_session=False)
    )
    await db.delete(scope)
    await db.commit()
    await _publish_account_settings_updated(get_redis(), user.id)
    return {"moved": int(result.rowcount or 0)}


@router.patch(
    "/me/memories/{memory_id}/scope",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_scope_assignment(
    memory_id: str,
    body: ConversationActiveScopeIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    memory = await _owned_memory(db, user.id, memory_id)
    memory.scope_id = (await _owned_scope(db, user.id, body.scope_id)).id
    await db.commit()
    await db.refresh(memory)
    return _memory_to_out(memory)


@router.post(
    "/me/memories/{memory_id}/confirm",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def confirm_memory(
    memory_id: str,
    body: MemoryConfirmIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    memory = await _owned_memory(db, user.id, memory_id)
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
            raise _http("conversation_not_found", "conversation not found", 404)
        conversation_id = conversation
    if body.decision == "yes":
        memory.positive_signal += 1
        memory.last_confirmed_at = datetime.now(timezone.utc)
    elif body.decision == "no":
        memory.negative_signal += 2
        memory.last_confirmed_at = datetime.now(timezone.utc)
        if conversation_id:
            await _disable_memory_for_conversation(get_redis(), conversation_id, memory.id)
    else:
        memory.last_confirmed_at = datetime.now(timezone.utc)
    db.add(
        _audit(
            user_id=user.id,
            event_type=f"confirm_{body.decision}",
            memory_id=memory.id,
            new_content=memory.content,
            details={"conversation_id": conversation_id} if conversation_id else None,
        )
    )
    await db.commit()
    await db.refresh(memory)
    return _memory_to_out(memory)


@router.patch(
    "/conversations/{conv_id}/memory-disabled",
    dependencies=[Depends(verify_csrf)],
)
async def patch_conversation_memory_disabled(
    conv_id: str,
    body: ConversationMemoryDisabledIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
        raise _http("not_found", "conversation not found", 404)
    conv.memory_disabled = body.disabled
    await db.commit()
    redis = get_redis()
    await _publish_conversation_memory_updated(
        redis,
        user_id=user.id,
        conversation_id=conv_id,
        payload={
            "conversation_id": conv_id,
            "memory_disabled": conv.memory_disabled,
            "active_scope_id": conv.active_scope_id,
        },
    )
    await _publish_account_settings_updated(redis, user.id)
    return {"disabled": conv.memory_disabled}


@router.patch(
    "/conversations/{conv_id}/active-scope",
    dependencies=[Depends(verify_csrf)],
)
async def patch_conversation_active_scope(
    conv_id: str,
    body: ConversationActiveScopeIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
        raise _http("not_found", "conversation not found", 404)
    if body.scope_id is None:
        conv.active_scope_id = None
    else:
        conv.active_scope_id = (await _owned_scope(db, user.id, body.scope_id)).id
    await db.commit()
    redis = get_redis()
    await _publish_conversation_memory_updated(
        redis,
        user_id=user.id,
        conversation_id=conv_id,
        payload={
            "conversation_id": conv_id,
            "memory_disabled": conv.memory_disabled,
            "active_scope_id": conv.active_scope_id,
        },
    )
    await _publish_account_settings_updated(redis, user.id)
    return {"scope_id": conv.active_scope_id}


@router.get("/conversations/{conv_id}/used-memories", response_model=UsedMemoriesOut)
async def get_conversation_used_memories(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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
        raise _http("not_found", "conversation not found", 404)
    row = (
        await db.execute(
            select(Completion.upstream_request)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user.id,
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
            )
            .order_by(desc(Completion.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    memory = row.get("memory") if isinstance(row, dict) else None
    if not isinstance(memory, dict):
        return UsedMemoriesOut()
    ids = memory.get("used_memory_ids")
    summary = memory.get("used_memory_summary")
    return UsedMemoriesOut(
        used_memory_ids=[v for v in ids if isinstance(v, str)] if isinstance(ids, list) else [],
        used_memory_summary=[
            {k: str(v) for k, v in item.items() if k in {"id", "type", "content"}}
            for item in summary
            if isinstance(item, dict)
        ]
        if isinstance(summary, list)
        else [],
    )

async def cleanup_expired_staging(db: AsyncSession) -> int:
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(UserMemoryStaging).where(
                UserMemoryStaging.decision == "pending",
                UserMemoryStaging.expires_at < now,
            )
        )
    ).scalars().all()
    for row in rows:
        row.decision = "rejected"
        row.decided_at = now
    await db.commit()
    return len(rows)
