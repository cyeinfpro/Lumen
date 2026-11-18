"""Database reads used while assembling account-memory prompts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Float, bindparam, cast, func, literal_column, or_, select
from sqlalchemy.orm import aliased, attributes, defer

from lumen_core.model_entities import (
    Conversation,
    UserMemory,
    UserMemoryScope,
)
from lumen_core.sqltypes import HalfvecColumnType

PROMPT_MEMORY_ROW_LIMIT = 128
PROMPT_PG_VECTOR_CANDIDATE_LIMIT = 16
PROMPT_PG_HNSW_ANN_POOL_LIMIT = 64
PROMPT_FALLBACK_RECENT_LIMIT = 8


def session_dialect_name(session: Any) -> str:
    bind = getattr(session, "bind", None)
    if bind is None:
        sync_session = getattr(session, "sync_session", None)
        bind = getattr(sync_session, "bind", None)
    if bind is None:
        get_bind = getattr(session, "get_bind", None)
        if callable(get_bind):
            try:
                bind = get_bind()
            except Exception:
                bind = None
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "")


def _mark_embeddings_unloaded(rows: list[UserMemory]) -> list[UserMemory]:
    for row in rows:
        attributes.set_committed_value(row, "embedding", None)
    return rows


def _active_scope_predicates(
    *,
    user_id: str,
    scope_ids: set[str],
    disabled_ids: set[str] | None = None,
) -> list[Any]:
    predicates: list[Any] = [
        UserMemory.user_id == user_id,
        UserMemory.disabled.is_(False),
        UserMemory.superseded_by.is_(None),
        UserMemory.scope_id.in_(scope_ids),
    ]
    if disabled_ids:
        predicates.append(UserMemory.id.not_in(disabled_ids))
    return predicates


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
    scope_ids: set[str],
    disabled_ids: set[str] | None = None,
) -> list[UserMemory]:
    statement = select(UserMemory).where(
        *_active_scope_predicates(
            user_id=user_id,
            scope_ids=scope_ids,
            disabled_ids=disabled_ids,
        )
    )
    return list(
        (
            await session.execute(
                statement.order_by(
                    UserMemory.pinned.desc(),
                    UserMemory.confidence.desc(),
                    UserMemory.updated_at.desc(),
                    UserMemory.id.desc(),
                ).limit(PROMPT_MEMORY_ROW_LIMIT)
            )
        )
        .scalars()
        .all()
    )


def build_pg_mandatory_memories_statement(
    *,
    user_id: str,
    scope_ids: set[str],
    disabled_ids: set[str] | None = None,
) -> Any:
    return (
        select(UserMemory)
        .options(defer(UserMemory.embedding))
        .where(
            *_active_scope_predicates(
                user_id=user_id,
                scope_ids=scope_ids,
                disabled_ids=disabled_ids,
            ),
            or_(
                UserMemory.type.in_(("profile", "avoid")),
                UserMemory.pinned.is_(True),
            ),
        )
        .order_by(
            UserMemory.pinned.desc(),
            UserMemory.confidence.desc(),
            UserMemory.updated_at.desc(),
            UserMemory.id.desc(),
        )
    )


async def _prompt_mandatory_memory_rows(
    session: Any,
    *,
    user_id: str,
    scope_ids: set[str],
    disabled_ids: set[str] | None = None,
) -> tuple[list[UserMemory], bool]:
    mandatory_stmt = build_pg_mandatory_memories_statement(
        user_id=user_id,
        scope_ids=scope_ids,
        disabled_ids=disabled_ids,
    )
    mandatory_rows = _mark_embeddings_unloaded(
        list((await session.execute(mandatory_stmt)).scalars().all())
    )
    candidate_exists_stmt = (
        select(UserMemory.id)
        .where(
            *_active_scope_predicates(
                user_id=user_id,
                scope_ids=scope_ids,
                disabled_ids=disabled_ids,
            ),
            UserMemory.type.in_(("preference", "project")),
            UserMemory.pinned.is_(False),
        )
        .limit(1)
    )
    has_candidates = (
        await session.execute(candidate_exists_stmt)
    ).scalar_one_or_none() is not None
    return mandatory_rows, has_candidates


def build_pg_vector_candidates_statement(
    *,
    user_id: str,
    scope_ids: set[str],
    query_embedding_literal: str,
    disabled_ids: set[str] | None = None,
    limit: int = PROMPT_PG_VECTOR_CANDIDATE_LIMIT,
    dimensions: int = 3072,
) -> Any:
    query_halfvec = cast(
        bindparam("q", query_embedding_literal),
        HalfvecColumnType(dimensions),
    )
    ann_candidate = aliased(UserMemory)
    ann_predicates: list[Any] = [
        ann_candidate.user_id == user_id,
        ann_candidate.disabled.is_(False),
        ann_candidate.superseded_by.is_(None),
        ann_candidate.embedding.is_not(None),
        ann_candidate.scope_id.in_(scope_ids),
        ann_candidate.type.in_(("preference", "project")),
        ann_candidate.pinned.is_(False),
    ]
    if disabled_ids:
        ann_predicates.append(ann_candidate.id.not_in(disabled_ids))
    ann_subquery = (
        select(ann_candidate.id)
        .where(*ann_predicates)
        .order_by(
            ann_candidate.embedding.op("<=>", return_type=Float())(query_halfvec)
        )
        .limit(max(limit, PROMPT_PG_HNSW_ANN_POOL_LIMIT))
    )

    distance_expr = UserMemory.embedding.op("<=>", return_type=Float())(
        query_halfvec
    )
    similarity_expr = literal_column("1.0") - distance_expr
    signal_weight_expr = (
        literal_column("1.0")
        + literal_column("0.1") * UserMemory.positive_signal
        - literal_column("0.15") * UserMemory.negative_signal
    )
    weighted_score_expr = similarity_expr * signal_weight_expr

    return (
        select(UserMemory, similarity_expr.label("cosine_similarity"))
        .options(defer(UserMemory.embedding))
        .where(
            *_active_scope_predicates(
                user_id=user_id,
                scope_ids=scope_ids,
                disabled_ids=disabled_ids,
            ),
            UserMemory.type.in_(("preference", "project")),
            UserMemory.pinned.is_(False),
            UserMemory.embedding.is_not(None),
            UserMemory.id.in_(ann_subquery),
        )
        .order_by(
            weighted_score_expr.desc(),
            UserMemory.updated_at.desc(),
            UserMemory.id.desc(),
        )
        .limit(limit)
    )


async def _prompt_pg_vector_candidates(
    session: Any,
    *,
    user_id: str,
    scope_ids: set[str],
    query_embedding_literal: str,
    disabled_ids: set[str] | None = None,
    limit: int = PROMPT_PG_VECTOR_CANDIDATE_LIMIT,
) -> tuple[list[UserMemory], dict[str, float]]:
    statement = build_pg_vector_candidates_statement(
        user_id=user_id,
        scope_ids=scope_ids,
        query_embedding_literal=query_embedding_literal,
        disabled_ids=disabled_ids,
        limit=limit,
    )
    result_rows = (await session.execute(statement)).all()
    memories: list[UserMemory] = []
    similarities: dict[str, float] = {}
    for memory, similarity in result_rows:
        memories.append(memory)
        if similarity is not None:
            similarities[memory.id] = float(similarity)
    return _mark_embeddings_unloaded(memories), similarities


async def _prompt_pg_recent_fallback_candidates(
    session: Any,
    *,
    user_id: str,
    scope_ids: set[str],
    disabled_ids: set[str] | None = None,
    limit: int = PROMPT_FALLBACK_RECENT_LIMIT,
) -> list[UserMemory]:
    activity_anchor = func.coalesce(
        UserMemory.last_used_at,
        UserMemory.updated_at,
        UserMemory.created_at,
    )
    statement = (
        select(UserMemory)
        .options(defer(UserMemory.embedding))
        .where(
            *_active_scope_predicates(
                user_id=user_id,
                scope_ids=scope_ids,
                disabled_ids=disabled_ids,
            ),
            UserMemory.type.in_(("preference", "project")),
            UserMemory.pinned.is_(False),
        )
        .order_by(
            activity_anchor.desc(),
            UserMemory.confidence.desc(),
            UserMemory.id.desc(),
        )
        .limit(limit)
    )
    rows = list((await session.execute(statement)).scalars().all())
    return _mark_embeddings_unloaded(rows)


async def _prompt_pg_memory_similarities(
    session: Any,
    *,
    memory_ids: list[str],
    query_embedding_literal: str,
    dimensions: int = 3072,
) -> dict[str, float]:
    if not memory_ids:
        return {}
    query_halfvec = cast(
        bindparam("q", query_embedding_literal),
        HalfvecColumnType(dimensions),
    )
    similarity_expr = literal_column("1.0") - UserMemory.embedding.op(
        "<=>", return_type=Float()
    )(query_halfvec)
    rows = (
        await session.execute(
            select(UserMemory.id, similarity_expr).where(
                UserMemory.id.in_(memory_ids),
                UserMemory.embedding.is_not(None),
            )
        )
    ).all()
    return {
        str(memory_id): float(similarity)
        for memory_id, similarity in rows
        if memory_id and similarity is not None
    }
