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

MEMORY_MAINTENANCE_BATCH_SIZE = 500


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
                    .limit(MEMORY_MAINTENANCE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
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
                    .limit(MEMORY_MAINTENANCE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
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
    """Drain one bounded memory:last_used_pending batch per cron tick.

    Why ZSET: assemble_user_memory_prompt fan-outs N writes per chat turn; per
    DESIGN §7.3 step 8 we batch them into one cron tick instead of hammering
    user_memories with row-by-row UPDATEs. Removal compares the observed score
    atomically so a producer bump that races this flush remains queued.
    """
    redis = ctx.get("redis")
    if redis is None:
        return
    try:
        members = await redis.zrange(
            last_used_pending_key,
            0,
            MEMORY_MAINTENANCE_BATCH_SIZE - 1,
            withscores=True,
        )
    except Exception:
        return
    if not members:
        return
    by_score: dict[float, list[str]] = defaultdict(list)
    member_scores: list[tuple[str, float]] = []
    for member, score in members:
        if isinstance(member, bytes):
            member = member.decode("utf-8", errors="ignore")
        if not isinstance(member, str):
            continue
        parsed_score = float(score)
        member_scores.append((member, parsed_score))
        by_score[parsed_score].append(member)
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
        argv = [
            value
            for member, score in member_scores
            for value in (member, repr(score))
        ]
        await redis.eval(
            """
            local removed = 0
            for i = 1, #ARGV, 2 do
              local current = redis.call('ZSCORE', KEYS[1], ARGV[i])
              if current and tonumber(current) == tonumber(ARGV[i + 1]) then
                removed = removed + redis.call('ZREM', KEYS[1], ARGV[i])
              end
            end
            return removed
            """,
            1,
            last_used_pending_key,
            *argv,
        )
    except Exception:
        logger.warning(
            "flush_memory_last_used conditional zrem failed members=%d",
            len(member_scores),
        )
