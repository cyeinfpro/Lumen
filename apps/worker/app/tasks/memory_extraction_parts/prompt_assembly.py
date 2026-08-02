"""Prompt-memory selection and usage-accounting helpers."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from lumen_core.memory import (
    cosine_similarity,
    deterministic_embedding,
    parse_embedding_literal,
)
from lumen_core.model_entities import MemoryAudit, Message, User, UserMemory


EmbeddingVector = Callable[[dict[str, Any] | None, str], Awaitable[list[float]]]
AdvisoryXactLock = Callable[[Any, str], Awaitable[None]]
MemoryDecay = Callable[[UserMemory, datetime], float]
MemoryLineClipper = Callable[..., list[UserMemory]]


async def ranked_prompt_memories(
    rows: list[UserMemory],
    *,
    user_text: str,
    now: datetime,
    embedding_vector: EmbeddingVector,
    decay: MemoryDecay,
) -> tuple[list[UserMemory], list[UserMemory], list[UserMemory], list[float] | None]:
    profiles = [memory for memory in rows if memory.type == "profile"]
    avoids = [memory for memory in rows if memory.type == "avoid"]
    pinned = [memory for memory in rows if memory.pinned]
    candidates = [
        memory
        for memory in rows
        if memory.type in {"preference", "project"} and not memory.pinned
    ]

    query_vec = None
    ranked: list[tuple[float, UserMemory]] = []
    if len((user_text or "").strip()) >= 5:
        query_vec = await embedding_vector(None, user_text)
        for memory in candidates:
            memory_vec = parse_embedding_literal(
                memory.embedding
            ) or deterministic_embedding(memory.content)
            score = (
                cosine_similarity(query_vec, memory_vec)
                * (1 + 0.1 * memory.positive_signal - 0.15 * memory.negative_signal)
                * decay(memory, now)
            )
            ranked.append((score, memory))
    ranked.sort(key=lambda item: item[0], reverse=True)

    context_memories: list[UserMemory] = []
    seen: set[str] = set()
    for memory in [*pinned, *[memory for _, memory in ranked[:8]]]:
        if memory.id in seen or memory.type in {"profile", "avoid"}:
            continue
        seen.add(memory.id)
        context_memories.append(memory)
    return profiles, avoids, context_memories, query_vec


def clipped_prompt_memories(
    profiles: list[UserMemory],
    avoids: list[UserMemory],
    context_memories: list[UserMemory],
    *,
    clip_lines: MemoryLineClipper,
) -> tuple[list[UserMemory], list[UserMemory], list[UserMemory]]:
    profiles = clip_lines(
        sorted(profiles, key=lambda memory: (not memory.pinned, -memory.confidence)),
        max_chars=400,
    )
    avoids = clip_lines(
        sorted(avoids, key=lambda memory: (not memory.pinned, -memory.confidence)),
        max_chars=400,
    )
    return profiles, avoids, clip_lines(context_memories, max_chars=600)


async def record_used_memories(
    session: Any,
    *,
    redis: Any | None,
    used_ids: list[str],
    now: datetime,
    last_used_pending_key: str,
) -> None:
    if not used_ids:
        return
    flushed = False
    if redis is not None:
        try:
            pipe = redis.pipeline(transaction=False)
            score = now.timestamp()
            for memory_id in used_ids:
                pipe.zadd(last_used_pending_key, {memory_id: score})
            await pipe.execute()
            flushed = True
        except Exception:
            flushed = False
    if flushed:
        return
    await session.execute(
        update(UserMemory)
        .where(UserMemory.id.in_(used_ids))
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )


def confirmation_instruction(memory: UserMemory | None) -> str | None:
    if memory is None:
        return None
    return (
        f"如果用户问题与用户偏好「{memory.content}」高度相关,"
        "请在回答开头用一句话简短确认:「按你之前提到的这个偏好来吗?」再继续回答。"
        "不要解释为什么记得。"
    )


async def pick_confirmation_candidate(
    session: Any,
    memories: list[UserMemory],
    *,
    user: User,
    user_text: str,
    now: datetime,
    conversation_id: str,
    parent_user_message_id: str | None,
    try_advisory_xact_lock: AdvisoryXactLock,
    confirm_weekly_limit: int,
    query_vec: list[float] | None = None,
) -> UserMemory | None:
    if not user.confirmation_enabled:
        return None
    if re.search(
        r"(记住|remember|以后|不要|never|always)", user_text or "", re.IGNORECASE
    ):
        return None
    if parent_user_message_id:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        await try_advisory_xact_lock(
            session,
            f"{user.id}:confirm_prompt:{conversation_id}:{int(day_start.timestamp())}",
        )
        week_cutoff = now - timedelta(days=7)
        weekly_count = (
            await session.execute(
                select(func.count(MemoryAudit.id)).where(
                    MemoryAudit.user_id == user.id,
                    MemoryAudit.event_type == "confirm_prompted",
                    MemoryAudit.created_at >= week_cutoff,
                )
            )
        ).scalar_one()
        if int(weekly_count or 0) >= confirm_weekly_limit:
            return None

        daily_count = (
            await session.execute(
                select(func.count(MemoryAudit.id))
                .select_from(MemoryAudit)
                .join(Message, MemoryAudit.source_message_id == Message.id)
                .where(
                    MemoryAudit.user_id == user.id,
                    MemoryAudit.event_type == "confirm_prompted",
                    MemoryAudit.created_at >= day_start,
                    Message.conversation_id == conversation_id,
                )
            )
        ).scalar_one()
        if int(daily_count or 0) > 0:
            return None
    for memory in sorted(memories, key=lambda item: item.positive_signal, reverse=True):
        if memory.type not in {"preference", "avoid"}:
            continue
        if memory.positive_signal < 3:
            continue
        if memory.last_confirmed_at and (now - memory.last_confirmed_at).days < 14:
            continue
        if parent_user_message_id:
            prompted_count = (
                await session.execute(
                    select(func.count(MemoryAudit.id))
                    .select_from(MemoryAudit)
                    .join(Message, MemoryAudit.source_message_id == Message.id)
                    .where(
                        MemoryAudit.user_id == user.id,
                        MemoryAudit.memory_id == memory.id,
                        MemoryAudit.event_type == "confirm_prompted",
                        Message.conversation_id == conversation_id,
                    )
                )
            ).scalar_one()
            if int(prompted_count or 0) > 0:
                continue
        score = cosine_similarity(
            query_vec or deterministic_embedding(user_text),
            parse_embedding_literal(memory.embedding)
            or deterministic_embedding(memory.content),
        )
        if score >= 0.92:
            if parent_user_message_id:
                session.add(
                    MemoryAudit(
                        user_id=user.id,
                        memory_id=memory.id,
                        event_type="confirm_prompted",
                        source_message_id=parent_user_message_id,
                        details={
                            "conversation_id": conversation_id,
                            "weekly_limit": confirm_weekly_limit,
                        },
                    )
                )
                await session.flush()
            return memory
    return None
