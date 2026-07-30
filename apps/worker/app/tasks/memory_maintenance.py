"""Periodic cleanup and last-used maintenance for account memory."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update

from lumen_core.model_entities import (
    UserMemory,
    UserMemoryStaging,
)


async def cleanup_memory(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], Any],
    cleanup_expired_undo: Callable[[datetime], Awaitable[int]],
) -> None:
    _ = ctx
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    async with session_factory() as session:
        pending = (
            (
                await session.execute(
                    select(UserMemoryStaging).where(
                        UserMemoryStaging.decision == "pending",
                        UserMemoryStaging.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in pending:
            row.decision = "rejected"
            row.decided_at = now
        old_deleted = (
            (
                await session.execute(
                    select(UserMemory).where(
                        UserMemory.disabled.is_(True),
                        UserMemory.deleted_at.is_not(None),
                        UserMemory.deleted_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for memory in old_deleted:
            await session.delete(memory)
        await session.commit()
    await cleanup_expired_undo(now)


async def flush_memory_last_used(
    ctx: dict[str, Any],
    *,
    session_factory: Callable[[], Any],
    last_used_pending_key: str,
    logger: logging.Logger,
) -> None:
    """Drain memory:last_used_pending ZSET into one batched UPDATE per timestamp.

    Why ZSET: assemble_user_memory_prompt fan-outs N writes per chat turn; per
    DESIGN §7.3 step 8 we batch them into one cron tick instead of hammering
    user_memories with row-by-row UPDATEs. Race: a second producer may bump a
    member's score between our zrange and zrem; we lose at most one timestamp
    update — non-fatal because last_used_at only feeds decay scoring.
    """
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        members = await redis.zrange(
            last_used_pending_key,
            0,
            -1,
            withscores=True,
        )
    except Exception:
        return
    if not members:
        return
    by_score: dict[float, list[str]] = defaultdict(list)
    member_names: list[str] = []
    for member, score in members:
        if isinstance(member, bytes):
            member = member.decode("utf-8", errors="ignore")
        if not isinstance(member, str):
            continue
        member_names.append(member)
        by_score[float(score)].append(member)
    if not by_score:
        return
    async with session_factory() as session:
        for score, ids in by_score.items():
            ts = datetime.fromtimestamp(score, tz=timezone.utc)
            await session.execute(
                update(UserMemory)
                .where(UserMemory.id.in_(ids))
                .values(last_used_at=ts)
                .execution_options(synchronize_session=False)
            )
        await session.commit()
    try:
        await redis.zrem(last_used_pending_key, *member_names)
    except Exception:
        logger.warning(
            "flush_memory_last_used zrem failed members=%d",
            len(member_names),
        )
