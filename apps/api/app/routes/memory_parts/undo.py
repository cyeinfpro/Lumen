"""Undo token coordination and memory rollback implementation."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    MemoryAudit,
    User,
    UserMemory,
    UserMemoryStaging,
)

from .contracts import MemoryUndoIn


Operation = Callable[..., Any]
AsyncOperation = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class UndoDependencies:
    get_redis: Operation
    undo_payload: AsyncOperation
    undo_token_consumed: AsyncOperation
    cleanup_undo_token: AsyncOperation
    claim_undo_token: AsyncOperation
    undo_memory_action: AsyncOperation
    audit: Operation
    release_undo_token_claim: AsyncOperation
    enqueue_memory_reembed: AsyncOperation


def undo_token_claim_key(undo_token: str) -> str:
    return f"memory:undo:claim:{undo_token}"


async def undo_token_consumed(
    db: AsyncSession,
    *,
    user_id: str,
    undo_token: str,
) -> bool:
    row = (
        await db.execute(
            select(MemoryAudit.id)
            .where(
                MemoryAudit.user_id == user_id,
                MemoryAudit.event_type == "undo_token_consumed",
                MemoryAudit.details["undo_token"].as_string() == undo_token,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def claim_undo_token(
    redis: Any,
    undo_token: str,
    *,
    owner: str,
    claim_ttl_seconds: int,
) -> bool:
    claim_key = undo_token_claim_key(undo_token)
    set_fn = getattr(redis, "set", None)
    if callable(set_fn):
        claimed = await set_fn(
            claim_key,
            owner,
            ex=claim_ttl_seconds,
            nx=True,
        )
        return not (claimed is False or claimed == 0)
    raw = await redis.get(claim_key)
    return raw is None


async def release_undo_token_claim(
    redis: Any,
    undo_token: str,
    *,
    owner: str,
    logger: logging.Logger,
) -> None:
    claim_key = undo_token_claim_key(undo_token)
    try:
        raw = await redis.get(claim_key)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if raw == owner:
            await redis.delete(claim_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "memory undo claim cleanup failed token=%s err=%s",
            undo_token,
            exc,
        )


async def undo_payload(
    redis: Any,
    *,
    undo_token: str,
    user_id: str,
    http: Operation,
) -> dict[str, Any]:
    raw = await redis.get(f"memory:undo:{undo_token}")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw:
        raise http("undo_expired", "undo token expired", 410)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise http("undo_expired", "undo token expired", 410) from exc
    if payload.get("user_id") != user_id:
        raise http("forbidden", "undo token does not belong to this user", 403)
    return payload


async def cleanup_undo_token(
    redis: Any,
    *,
    undo_token: str,
    user_id: str,
    log_message: str,
    release_claim: AsyncOperation,
    logger: logging.Logger,
) -> None:
    try:
        await redis.delete(f"memory:undo:{undo_token}")
        await release_claim(
            redis,
            undo_token,
            owner=user_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(log_message, undo_token, exc)


async def restore_merged_candidate(
    db: AsyncSession,
    *,
    user_id: str,
    duplicate: UserMemory,
    candidate: object,
    owned_scope: AsyncOperation,
    audit: Operation,
) -> str | None:
    if not isinstance(candidate, dict):
        return None
    candidate_type = candidate.get("type")
    candidate_content = candidate.get("content")
    if (
        candidate_type not in {"profile", "preference", "avoid", "project"}
        or not isinstance(candidate_content, str)
        or not candidate_content.strip()
    ):
        return None
    scope_id = candidate.get("scope_id") or duplicate.scope_id
    scope = await owned_scope(db, user_id, scope_id)
    source = candidate.get("source")
    if source not in {"explicit", "auto", "manual"}:
        source = "auto"
    independent = UserMemory(
        user_id=user_id,
        type=candidate_type,
        content=candidate_content.strip(),
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
        source=source,
        embedding=None,
        confidence=float(candidate.get("confidence") or 0.80),
        scope_id=scope.id,
        last_used_at=datetime.now(timezone.utc),
    )
    db.add(independent)
    await db.flush()
    db.add(
        audit(
            user_id=user_id,
            event_type="undo_merged",
            memory_id=independent.id,
            new_content=independent.content,
            details={"merged_into": duplicate.id},
        )
    )
    return independent.id


async def undo_memory_action(
    db: AsyncSession,
    *,
    user_id: str,
    payload: dict[str, Any],
    owned_memory: AsyncOperation,
    restore_merged_candidate_fn: AsyncOperation,
    audit: Operation,
) -> str | None:
    action = payload.get("action")
    memory_id = payload.get("memory_id")
    if action in {"added", "updated"} and isinstance(memory_id, str):
        memory = await owned_memory(db, user_id, memory_id)
        memory.disabled = True
        db.add(audit(user_id=user_id, event_type="undo", memory_id=memory.id))
        return None
    if action == "merged" and isinstance(memory_id, str):
        duplicate = await owned_memory(db, user_id, memory_id)
        duplicate.positive_signal = max(0, duplicate.positive_signal - 1)
        reembed_id = await restore_merged_candidate_fn(
            db,
            user_id=user_id,
            duplicate=duplicate,
            candidate=payload.get("candidate"),
        )
        db.add(
            audit(
                user_id=user_id,
                event_type="undo",
                memory_id=duplicate.id,
                details={"action": "merged"},
            )
        )
        return reembed_id
    if action == "superseded" and isinstance(memory_id, str):
        memory = await owned_memory(db, user_id, memory_id)
        memory.disabled = True
        old_id = payload.get("old_memory_id")
        if isinstance(old_id, str):
            old = await owned_memory(db, user_id, old_id)
            old.superseded_by = None
        db.add(audit(user_id=user_id, event_type="undo", memory_id=memory.id))
        return None
    if action == "staged":
        staging_id = payload.get("staging_id")
        if isinstance(staging_id, str):
            row = await db.get(UserMemoryStaging, staging_id)
            if row is not None and row.user_id == user_id:
                row.decision = "rejected"
                row.decided_at = datetime.now(timezone.utc)
    return None


async def undo_memory_write_impl(
    body: MemoryUndoIn,
    user: User,
    db: AsyncSession,
    *,
    deps: UndoDependencies,
) -> dict[str, bool]:
    redis = deps.get_redis()
    payload = await deps.undo_payload(
        redis,
        undo_token=body.undo_token,
        user_id=user.id,
    )
    if await deps.undo_token_consumed(db, user_id=user.id, undo_token=body.undo_token):
        await deps.cleanup_undo_token(
            redis,
            undo_token=body.undo_token,
            user_id=user.id,
            log_message=("memory undo consumed token cleanup failed token=%s err=%s"),
        )
        return {"ok": True}
    if not await deps.claim_undo_token(redis, body.undo_token, owner=user.id):
        return {"ok": False}
    action = payload.get("action")
    memory_id = payload.get("memory_id")
    reembed_id = await deps.undo_memory_action(
        db,
        user_id=user.id,
        payload=payload,
    )
    db.add(
        deps.audit(
            user_id=user.id,
            event_type="undo_token_consumed",
            memory_id=memory_id if isinstance(memory_id, str) else None,
            details={
                "undo_token": body.undo_token,
                "action": action,
            },
        )
    )
    try:
        await db.commit()
    except Exception:
        await deps.release_undo_token_claim(redis, body.undo_token, owner=user.id)
        raise
    await deps.cleanup_undo_token(
        redis,
        undo_token=body.undo_token,
        user_id=user.id,
        log_message="memory undo token cleanup failed token=%s err=%s",
    )
    if reembed_id:
        await deps.enqueue_memory_reembed("memory", reembed_id)
    return {"ok": True}
