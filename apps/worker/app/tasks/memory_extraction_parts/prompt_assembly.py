"""Prompt-memory selection and usage-accounting helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import math
import re
import struct
from typing import Any
import unicodedata

from sqlalchemy import func, select, update

from lumen_core.memory import (
    cosine_similarity,
    deterministic_embedding,
    embedding_literal,
    parse_embedding_literal,
)
from lumen_core.model_entities import (
    Conversation,
    MemoryAudit,
    Message,
    User,
    UserMemory,
)

from ..memory_extraction_values import AssembledMemoryPrompt

EmbeddingVector = Callable[[dict[str, Any] | None, str], Awaitable[list[float]]]
AdvisoryXactLock = Callable[[Any, str], Awaitable[None]]
MemoryDecay = Callable[[UserMemory, datetime], float]
MemoryLineClipper = Callable[..., list[UserMemory]]

QUERY_EMBEDDING_CACHE_TTL_SECONDS = 600
QUERY_EMBEDDING_SOFT_TIMEOUT_SECONDS = 0.75
QUERY_EMBEDDING_CACHE_KEY_PREFIX = "memory:query_emb:v1"
DEFAULT_EMBEDDING_CACHE_MODEL = "text-embedding-3-large"
PROMPT_CONTEXT_TOP_K = 8

_EPOCH_UTC = datetime.min.replace(tzinfo=timezone.utc)
_logger = logging.getLogger(__name__)


def normalize_query_embedding_text(user_text: str) -> str:
    return unicodedata.normalize("NFC", " ".join((user_text or "").strip().split()))


def query_embedding_cache_key(
    user_text: str,
    *,
    model: str = DEFAULT_EMBEDDING_CACHE_MODEL,
) -> str:
    normalized = normalize_query_embedding_text(user_text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{QUERY_EMBEDDING_CACHE_KEY_PREFIX}:{model}:{digest}"


def pack_float16_vector(values: list[float]) -> bytes:
    if not values:
        return b""
    return struct.pack(f"<{len(values)}e", *values)


def unpack_float16_vector(payload: bytes | bytearray | memoryview) -> list[float]:
    raw = bytes(payload)
    if len(raw) < 2 or len(raw) % 2 != 0:
        return []
    values = [float(item) for item in struct.unpack(f"<{len(raw) // 2}e", raw)]
    if not all(math.isfinite(item) for item in values):
        return []
    return values


async def _read_cached_query_embedding(
    redis: Any | None,
    cache_key: str,
) -> list[float] | None:
    if redis is None or not hasattr(redis, "get"):
        return None
    try:
        cached = await redis.get(cache_key)
    except Exception:
        return None
    if isinstance(cached, (bytes, bytearray, memoryview)):
        parsed = unpack_float16_vector(cached)
        return parsed or None
    return None


async def _write_cached_query_embedding(
    redis: Any | None,
    cache_key: str,
    vector: list[float],
    *,
    ttl_seconds: int,
) -> None:
    if redis is None or not vector or ttl_seconds <= 0:
        return
    try:
        payload = pack_float16_vector(vector)
        if not payload:
            return
        if hasattr(redis, "set"):
            await redis.set(cache_key, payload, ex=ttl_seconds)
        elif hasattr(redis, "setex"):
            await redis.setex(cache_key, ttl_seconds, payload)
    except Exception:
        return


async def resolve_query_embedding_for_prompt(
    user_text: str,
    *,
    embedding_vector: EmbeddingVector,
    redis: Any | None = None,
    soft_timeout_s: float = QUERY_EMBEDDING_SOFT_TIMEOUT_SECONDS,
    cache_ttl_s: int = QUERY_EMBEDDING_CACHE_TTL_SECONDS,
    model: str = DEFAULT_EMBEDDING_CACHE_MODEL,
) -> tuple[list[float] | None, bool]:
    normalized = normalize_query_embedding_text(user_text)
    if len(normalized) < 5:
        return None, False
    cache_key = query_embedding_cache_key(normalized, model=model)
    cached_vec = await _read_cached_query_embedding(redis, cache_key)
    if cached_vec is not None:
        return cached_vec, False
    try:
        if soft_timeout_s > 0:
            vector = await asyncio.wait_for(
                embedding_vector(None, user_text),
                timeout=soft_timeout_s,
            )
        else:
            vector = await embedding_vector(None, user_text)
    except TimeoutError:
        _logger.warning(
            "memory_prompt.query_embedding_soft_timeout timeout_s=%.3f fallback=recent_top8",
            soft_timeout_s,
        )
        return None, True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "memory_prompt.query_embedding_failed err=%s fallback=recent_top8",
            exc,
        )
        return None, True
    if not vector:
        return None, True
    await _write_cached_query_embedding(
        redis,
        cache_key,
        vector,
        ttl_seconds=cache_ttl_s,
    )
    return vector, False


def _timestamp_sort_key(value: datetime | None) -> datetime:
    if value is None:
        return _EPOCH_UTC
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def recent_fallback_memories(
    candidates: list[UserMemory],
    *,
    limit: int = PROMPT_CONTEXT_TOP_K,
) -> list[UserMemory]:
    ordered = sorted(
        candidates,
        key=lambda memory: (
            _timestamp_sort_key(
                getattr(memory, "last_used_at", None)
                or getattr(memory, "updated_at", None)
                or getattr(memory, "created_at", None)
            ),
            float(getattr(memory, "confidence", 0.0) or 0.0),
            str(getattr(memory, "id", "") or ""),
        ),
        reverse=True,
    )
    return ordered[: max(0, limit)]


def _score_candidate_memories(
    candidates: list[UserMemory],
    *,
    query_vec: list[float],
    now: datetime,
    decay: MemoryDecay,
    precomputed_similarities: Mapping[str, float] | None = None,
) -> list[UserMemory]:
    ranked: list[tuple[float, UserMemory]] = []
    for memory in candidates:
        if (
            precomputed_similarities is not None
            and memory.id in precomputed_similarities
        ):
            similarity = float(precomputed_similarities[memory.id])
        else:
            memory_vec = parse_embedding_literal(
                memory.embedding
            ) or deterministic_embedding(memory.content)
            similarity = cosine_similarity(query_vec, memory_vec)
        score = (
            similarity
            * (1.0 + 0.1 * memory.positive_signal - 0.15 * memory.negative_signal)
            * decay(memory, now)
        )
        ranked.append((score, memory))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [memory for _, memory in ranked[:PROMPT_CONTEXT_TOP_K]]


async def ranked_prompt_memories(
    rows: list[UserMemory],
    *,
    user_text: str,
    now: datetime,
    embedding_vector: EmbeddingVector,
    decay: MemoryDecay,
    redis: Any | None = None,
    soft_timeout_s: float = QUERY_EMBEDDING_SOFT_TIMEOUT_SECONDS,
    precomputed_similarities: Mapping[str, float] | None = None,
    query_vec_override: list[float] | None = None,
    embedding_degraded: bool = False,
) -> tuple[list[UserMemory], list[UserMemory], list[UserMemory], list[float] | None]:
    profiles = [memory for memory in rows if memory.type == "profile"]
    avoids = [memory for memory in rows if memory.type == "avoid"]
    pinned = [memory for memory in rows if memory.pinned]
    candidates = [
        memory
        for memory in rows
        if memory.type in {"preference", "project"} and not memory.pinned
    ]

    query_vec: list[float] | None = None
    selected_candidates: list[UserMemory] = []
    if candidates and len((user_text or "").strip()) >= 5:
        if embedding_degraded or query_vec_override is not None:
            query_vec = query_vec_override
            degraded = embedding_degraded or query_vec is None
        else:
            query_vec, degraded = await resolve_query_embedding_for_prompt(
                user_text,
                embedding_vector=embedding_vector,
                redis=redis,
                soft_timeout_s=soft_timeout_s,
            )
        if degraded or query_vec is None:
            selected_candidates = recent_fallback_memories(
                candidates,
                limit=PROMPT_CONTEXT_TOP_K,
            )
        else:
            selected_candidates = _score_candidate_memories(
                candidates,
                query_vec=query_vec,
                now=now,
                decay=decay,
                precomputed_similarities=precomputed_similarities,
            )

    context_memories: list[UserMemory] = []
    seen: set[str] = set()
    for memory in [*pinned, *selected_candidates]:
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


def _eligible_confirmation_memories(
    memories: list[UserMemory],
    *,
    now: datetime,
) -> list[UserMemory]:
    eligible: list[UserMemory] = []
    seen: set[str] = set()
    for memory in sorted(memories, key=lambda item: item.positive_signal, reverse=True):
        if memory.id and memory.id in seen:
            continue
        if memory.type not in {"preference", "avoid"}:
            continue
        if memory.positive_signal < 3:
            continue
        if memory.last_confirmed_at and (now - memory.last_confirmed_at).days < 14:
            continue
        if memory.id:
            seen.add(memory.id)
        eligible.append(memory)
    return eligible


async def _confirmation_window_allows_prompt(
    session: Any,
    *,
    user_id: str,
    conversation_id: str,
    now: datetime,
    try_advisory_xact_lock: AdvisoryXactLock,
    confirm_weekly_limit: int,
) -> bool:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await try_advisory_xact_lock(
        session,
        f"{user_id}:confirm_prompt:{conversation_id}:{int(day_start.timestamp())}",
    )
    week_cutoff = now - timedelta(days=7)
    weekly_count = (
        await session.execute(
            select(func.count(MemoryAudit.id)).where(
                MemoryAudit.user_id == user_id,
                MemoryAudit.event_type == "confirm_prompted",
                MemoryAudit.created_at >= week_cutoff,
            )
        )
    ).scalar_one()
    if int(weekly_count or 0) >= confirm_weekly_limit:
        return False

    daily_count = (
        await session.execute(
            select(func.count(MemoryAudit.id))
            .select_from(MemoryAudit)
            .join(Message, MemoryAudit.source_message_id == Message.id)
            .where(
                MemoryAudit.user_id == user_id,
                MemoryAudit.event_type == "confirm_prompted",
                MemoryAudit.created_at >= day_start,
                Message.conversation_id == conversation_id,
            )
        )
    ).scalar_one()
    return int(daily_count or 0) == 0


async def _prompted_confirmation_memory_ids(
    session: Any,
    *,
    user_id: str,
    conversation_id: str,
    memory_ids: list[str],
) -> set[str]:
    if not memory_ids:
        return set()
    rows = (
        await session.execute(
            select(
                MemoryAudit.memory_id,
                func.count(MemoryAudit.id),
            )
            .select_from(MemoryAudit)
            .join(Message, MemoryAudit.source_message_id == Message.id)
            .where(
                MemoryAudit.user_id == user_id,
                MemoryAudit.memory_id.in_(memory_ids),
                MemoryAudit.event_type == "confirm_prompted",
                Message.conversation_id == conversation_id,
            )
            .group_by(MemoryAudit.memory_id)
        )
    ).all()
    return {
        str(memory_id)
        for memory_id, count in rows
        if memory_id and int(count or 0) > 0
    }


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
    precomputed_similarities: Mapping[str, float] | None = None,
) -> UserMemory | None:
    if not user.confirmation_enabled:
        return None
    if re.search(
        r"(记住|remember|以后|不要|never|always)", user_text or "", re.IGNORECASE
    ):
        return None
    eligible = _eligible_confirmation_memories(memories, now=now)
    if not eligible:
        return None

    already_prompted_ids: set[str] = set()
    if parent_user_message_id:
        allowed = await _confirmation_window_allows_prompt(
            session,
            user_id=user.id,
            conversation_id=conversation_id,
            now=now,
            try_advisory_xact_lock=try_advisory_xact_lock,
            confirm_weekly_limit=confirm_weekly_limit,
        )
        if not allowed:
            return None
        eligible_ids = [memory.id for memory in eligible if memory.id]
        already_prompted_ids = await _prompted_confirmation_memory_ids(
            session,
            user_id=user.id,
            conversation_id=conversation_id,
            memory_ids=eligible_ids,
        )

    effective_query_vec: list[float] | None = query_vec
    for memory in eligible:
        if memory.id and memory.id in already_prompted_ids:
            continue
        if (
            precomputed_similarities is not None
            and memory.id in precomputed_similarities
        ):
            score = float(precomputed_similarities[memory.id])
        else:
            if effective_query_vec is None:
                effective_query_vec = deterministic_embedding(user_text)
            score = cosine_similarity(
                effective_query_vec,
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


@dataclass(frozen=True)
class PgPromptAssemblyHooks:
    prompt_mandatory_memory_rows: Callable[..., Awaitable[tuple[list[UserMemory], bool]]]
    prompt_pg_vector_candidates: Callable[
        ..., Awaitable[tuple[list[UserMemory], dict[str, float]]]
    ]
    prompt_pg_recent_fallback_candidates: Callable[..., Awaitable[list[UserMemory]]]
    prompt_pg_memory_similarities: Callable[..., Awaitable[dict[str, float]]]
    embedding_vector: EmbeddingVector
    ranked_prompt_memories: Callable[
        ...,
        Awaitable[
            tuple[list[UserMemory], list[UserMemory], list[UserMemory], list[float] | None]
        ],
    ]
    embedding_model: str = DEFAULT_EMBEDDING_CACHE_MODEL
    soft_timeout_s: float = QUERY_EMBEDDING_SOFT_TIMEOUT_SECONDS
    cache_ttl_s: int = QUERY_EMBEDDING_CACHE_TTL_SECONDS


async def commit_if_supported(session: Any) -> None:
    commit = getattr(session, "commit", None)
    if callable(commit):
        await commit()


async def assemble_pg_prompt_memories(
    session: Any,
    *,
    hooks: PgPromptAssemblyHooks,
    user_id: str,
    scope_ids: set[str],
    disabled_ids: set[str],
    user_text: str,
    now: datetime,
    redis: Any | None,
) -> tuple[
    list[UserMemory],
    list[UserMemory],
    list[UserMemory],
    list[float] | None,
    dict[str, float],
    bool,
]:
    mandatory_rows, has_candidates = await hooks.prompt_mandatory_memory_rows(
        session,
        user_id=user_id,
        scope_ids=scope_ids,
        disabled_ids=disabled_ids,
    )
    await commit_if_supported(session)

    candidate_rows: list[UserMemory] = []
    similarities: dict[str, float] = {}
    query_vec: list[float] | None = None
    degraded = False
    if has_candidates and len((user_text or "").strip()) >= 5:
        query_vec, degraded = await resolve_query_embedding_for_prompt(
            user_text,
            embedding_vector=hooks.embedding_vector,
            redis=redis,
            soft_timeout_s=hooks.soft_timeout_s,
            cache_ttl_s=hooks.cache_ttl_s,
            model=hooks.embedding_model,
        )
        if query_vec is not None and not degraded:
            candidate_rows, similarities = await hooks.prompt_pg_vector_candidates(
                session,
                user_id=user_id,
                scope_ids=scope_ids,
                query_embedding_literal=embedding_literal(query_vec),
                disabled_ids=disabled_ids,
            )
            if not candidate_rows:
                candidate_rows = await hooks.prompt_pg_recent_fallback_candidates(
                    session,
                    user_id=user_id,
                    scope_ids=scope_ids,
                    disabled_ids=disabled_ids,
                )
                degraded = True
        else:
            candidate_rows = await hooks.prompt_pg_recent_fallback_candidates(
                session,
                user_id=user_id,
                scope_ids=scope_ids,
                disabled_ids=disabled_ids,
            )
            degraded = True
        await commit_if_supported(session)

    profiles, avoids, context_memories, query_vec = await hooks.ranked_prompt_memories(
        [*mandatory_rows, *candidate_rows],
        user_text=user_text,
        now=now,
        redis=redis,
        precomputed_similarities=similarities,
        query_vec_override=query_vec,
        embedding_degraded=degraded,
    )
    return profiles, avoids, context_memories, query_vec, similarities, degraded


async def hydrate_pg_confirmation_similarities(
    session: Any,
    *,
    hooks: PgPromptAssemblyHooks,
    candidates: list[UserMemory],
    user: User,
    user_text: str,
    now: datetime,
    redis: Any | None,
    query_vec: list[float] | None,
    embedding_degraded: bool,
    similarities: dict[str, float],
) -> tuple[list[float] | None, dict[str, float]]:
    if not getattr(user, "confirmation_enabled", False):
        return query_vec, similarities
    missing_ids = [
        memory.id
        for memory in candidates
        if memory.id
        and memory.id not in similarities
        and memory.type in {"preference", "avoid"}
        and memory.positive_signal >= 3
        and not (
            memory.last_confirmed_at and (now - memory.last_confirmed_at).days < 14
        )
    ]
    if not missing_ids:
        return query_vec, similarities
    if (
        query_vec is None
        and not embedding_degraded
        and len((user_text or "").strip()) >= 5
    ):
        query_vec, _ = await resolve_query_embedding_for_prompt(
            user_text,
            embedding_vector=hooks.embedding_vector,
            redis=redis,
            soft_timeout_s=hooks.soft_timeout_s,
            cache_ttl_s=hooks.cache_ttl_s,
            model=hooks.embedding_model,
        )
    if query_vec is None:
        return None, similarities
    extra = await hooks.prompt_pg_memory_similarities(
        session,
        memory_ids=missing_ids,
        query_embedding_literal=embedding_literal(query_vec),
    )
    merged = dict(similarities)
    merged.update(extra)
    return query_vec, merged


@dataclass(frozen=True)
class PromptAssemblyOrchestrationDeps:
    embedding_provider_available: Callable[[Any], Awaitable[bool]]
    conversation_disabled_memory_ids: Callable[[Any, str], Awaitable[set[str]]]
    memory_scope_context: Callable[..., Awaitable[tuple[set[str], Any]]]
    session_dialect_name: Callable[[Any], str]
    prompt_memory_rows: Callable[..., Awaitable[list[UserMemory]]]
    ranked_prompt_memories: Callable[..., Awaitable[Any]]
    clipped_prompt_memories: Callable[..., tuple[list[UserMemory], list[UserMemory], list[UserMemory]]]
    record_used_memories: Callable[..., Awaitable[None]]
    pick_confirmation_candidate: Callable[..., Awaitable[UserMemory | None]]
    pg_hooks_factory: Callable[[], PgPromptAssemblyHooks]
    memory_lines: Callable[[str, list[UserMemory]], str | None]
    user_model: Any = User
    conversation_model: Any = Conversation


async def assemble_user_memory_prompt_with_deps(
    session: Any,
    *,
    deps: PromptAssemblyOrchestrationDeps,
    user_id: str,
    conversation_id: str,
    user_text: str,
    redis: Any | None = None,
    parent_user_message_id: str | None = None,
) -> AssembledMemoryPrompt:
    if not await deps.embedding_provider_available(None):
        return AssembledMemoryPrompt(None, None, None, [], [])
    disabled_ids = await deps.conversation_disabled_memory_ids(redis, conversation_id)
    user = await session.get(deps.user_model, user_id)
    conv = await session.get(deps.conversation_model, conversation_id)
    if user is None or conv is None:
        return AssembledMemoryPrompt(None, None, None, [], [])
    if user.memory_disabled or conv.memory_disabled:
        return AssembledMemoryPrompt(None, None, None, [], [])

    scope_ids, active_scope = await deps.memory_scope_context(
        session,
        user_id=user_id,
        conversation=conv,
    )
    now = datetime.now(timezone.utc)
    pg_hooks = (
        deps.pg_hooks_factory()
        if deps.session_dialect_name(session) == "postgresql"
        else None
    )
    precomputed_similarities: dict[str, float] | None = None
    if pg_hooks is not None:
        (
            profiles,
            avoids,
            context_memories,
            query_vec,
            pg_similarities,
            embedding_degraded,
        ) = await assemble_pg_prompt_memories(
            session,
            hooks=pg_hooks,
            user_id=user_id,
            scope_ids=scope_ids,
            disabled_ids=disabled_ids,
            user_text=user_text,
            now=now,
            redis=redis,
        )
    else:
        rows = await deps.prompt_memory_rows(
            session,
            user_id=user_id,
            scope_ids=scope_ids,
            disabled_ids=disabled_ids,
        )
        await commit_if_supported(session)
        profiles, avoids, context_memories, query_vec = (
            await deps.ranked_prompt_memories(
                rows,
                user_text=user_text,
                now=now,
                redis=redis,
            )
        )
        pg_similarities = {}
        embedding_degraded = False

    profiles, avoids, context_memories = deps.clipped_prompt_memories(
        profiles,
        avoids,
        context_memories,
    )
    used = [*profiles, *avoids, *context_memories]
    used_ids = [m.id for m in used]
    used_summary = [{"id": m.id, "type": m.type, "content": m.content} for m in used]
    await deps.record_used_memories(
        session,
        redis=redis,
        used_ids=used_ids,
        now=now,
    )

    confirmation_pool = context_memories + avoids
    if pg_hooks is not None:
        query_vec, precomputed_similarities = (
            await hydrate_pg_confirmation_similarities(
                session,
                hooks=pg_hooks,
                candidates=confirmation_pool,
                user=user,
                user_text=user_text,
                now=now,
                redis=redis,
                query_vec=query_vec,
                embedding_degraded=embedding_degraded,
                similarities=pg_similarities,
            )
        )

    try:
        confirmation_candidate = await deps.pick_confirmation_candidate(
            session,
            confirmation_pool,
            user=user,
            user_text=user_text,
            now=now,
            conversation_id=conversation_id,
            parent_user_message_id=parent_user_message_id,
            query_vec=query_vec,
            precomputed_similarities=precomputed_similarities,
        )
    except TypeError:
        confirmation_candidate = await deps.pick_confirmation_candidate(
            session,
            confirmation_pool,
            user=user,
            user_text=user_text,
            now=now,
            conversation_id=conversation_id,
            parent_user_message_id=parent_user_message_id,
            query_vec=query_vec,
        )

    return AssembledMemoryPrompt(
        profile_text=deps.memory_lines("user_profile", profiles),
        constraints_text=deps.memory_lines("user_constraints", avoids),
        context_text=deps.memory_lines("user_context", context_memories),
        used_memory_ids=used_ids,
        used_memory_summary=used_summary,
        scope_hint_text=(
            f"本会话上下文领域: {active_scope.name}"
            if active_scope is not None and not active_scope.is_default
            else None
        ),
        confirmation_candidate_id=confirmation_candidate.id
        if confirmation_candidate
        else None,
        confirmation_instruction=confirmation_instruction(confirmation_candidate),
    )
