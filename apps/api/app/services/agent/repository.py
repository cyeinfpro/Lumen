"""Agent session ownership, pagination, and public snapshot queries."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_events import AGENT_RUN_ACTIVE_STATUSES
from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    user_visible_filter as byok_retention_user_visible_filter,
)
from lumen_core.constants import Role
from lumen_core.model_entities import (
    AgentRun,
    AgentRunReference,
    AgentSession,
    AgentToolCall,
    Completion,
    Conversation,
    Generation,
    Image,
    Message,
    User,
)
from lumen_core.schema_models import (
    AgentMessageListOut,
    AgentRunOut,
    AgentSessionListOut,
    AgentSessionOut,
    CompletionOut,
    GenerationOut,
    ImageOut,
    MessageOut,
)

from ...byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..conversations.cursor import (
    cursor_field_datetime,
    cursor_field_str,
    dec_cursor,
    enc_cursor,
)
from ..conversations.messages import image_to_out
from .common import http_error
from .presentation import agent_run_out, agent_session_out


async def retention_filter(
    db: AsyncSession,
    user: User,
    column: Any,
) -> Any | None:
    if not byok_retention_applies_to_user(user):
        return None
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    return byok_retention_user_visible_filter(user, column, policy=policy)


async def get_owned_agent_session(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    for_update: bool = False,
) -> tuple[AgentSession, Conversation]:
    statement = (
        select(AgentSession, Conversation)
        .join(Conversation, Conversation.id == AgentSession.conversation_id)
        .where(
            AgentSession.id == session_id,
            AgentSession.user_id == user_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).first()
    if row is None:
        raise http_error("not_found", "Agent session not found", 404)
    return row[0], row[1]


async def load_run_parts(
    db: AsyncSession,
    runs: list[AgentRun],
) -> tuple[dict[str, list[AgentRunReference]], dict[str, list[AgentToolCall]]]:
    if not runs:
        return {}, {}
    run_ids = [run.id for run in runs]
    references = list(
        (
            await db.execute(
                select(AgentRunReference)
                .where(AgentRunReference.agent_run_id.in_(run_ids))
                .order_by(
                    AgentRunReference.agent_run_id.asc(),
                    AgentRunReference.ordinal.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    tool_calls = list(
        (
            await db.execute(
                select(AgentToolCall)
                .where(AgentToolCall.agent_run_id.in_(run_ids))
                .order_by(
                    AgentToolCall.agent_run_id.asc(),
                    AgentToolCall.ordinal.asc(),
                )
            )
        )
        .scalars()
        .all()
    )
    refs_by_run: dict[str, list[AgentRunReference]] = {}
    tools_by_run: dict[str, list[AgentToolCall]] = {}
    for reference in references:
        refs_by_run.setdefault(reference.agent_run_id, []).append(reference)
    for tool_call in tool_calls:
        tools_by_run.setdefault(tool_call.agent_run_id, []).append(tool_call)
    return refs_by_run, tools_by_run


async def load_agent_run_out(db: AsyncSession, run: AgentRun) -> AgentRunOut:
    refs_by_run, tools_by_run = await load_run_parts(db, [run])
    return agent_run_out(
        run,
        references=refs_by_run.get(run.id),
        tool_calls=tools_by_run.get(run.id),
    )


async def _active_runs_by_session(
    db: AsyncSession,
    session_ids: list[str],
) -> dict[str, AgentRun]:
    if not session_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(AgentRun).where(
                    AgentRun.agent_session_id.in_(session_ids),
                    AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    return {run.agent_session_id: run for run in rows}


async def list_agent_sessions(
    db: AsyncSession,
    *,
    user: User,
    cursor: str | None,
    query: str | None,
    limit: int,
) -> AgentSessionListOut:
    statement = (
        select(AgentSession, Conversation)
        .join(Conversation, Conversation.id == AgentSession.conversation_id)
        .where(
            AgentSession.user_id == user.id,
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
        )
    )
    visible = await retention_filter(db, user, Conversation.last_activity_at)
    if visible is not None:
        statement = statement.where(visible)
    if query:
        value = query.strip()[:200]
        if value:
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(
                Conversation.title.ilike(f"%{escaped}%", escape="\\")
            )
    current = dec_cursor(cursor)
    if current is not None:
        updated_at = cursor_field_datetime(current, "ua")
        current_id = cursor_field_str(current, "id")
        statement = statement.where(
            or_(
                AgentSession.updated_at < updated_at,
                and_(
                    AgentSession.updated_at == updated_at,
                    AgentSession.id < current_id,
                ),
            )
        )
    rows = (
        await db.execute(
            statement.order_by(
                desc(AgentSession.updated_at),
                desc(AgentSession.id),
            ).limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    active_by_session = await _active_runs_by_session(
        db,
        [session.id for session, _conversation in page],
    )
    active_runs = list(active_by_session.values())
    refs_by_run, tools_by_run = await load_run_parts(db, active_runs)
    active_outputs = {
        run.agent_session_id: agent_run_out(
            run,
            references=refs_by_run.get(run.id),
            tool_calls=tools_by_run.get(run.id),
        )
        for run in active_runs
    }
    next_cursor = None
    if len(rows) > limit and page:
        last_session = page[-1][0]
        next_cursor = enc_cursor(
            {"ua": last_session.updated_at.isoformat(), "id": last_session.id}
        )
    return AgentSessionListOut(
        items=[
            agent_session_out(
                session,
                conversation,
                active_run=active_outputs.get(session.id),
            )
            for session, conversation in page
        ],
        next_cursor=next_cursor,
    )


async def get_agent_session_out(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
) -> AgentSessionOut:
    session, conversation = await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
    )
    active = (
        await db.execute(
            select(AgentRun).where(
                AgentRun.agent_session_id == session.id,
                AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
            )
        )
    ).scalar_one_or_none()
    return agent_session_out(
        session,
        conversation,
        active_run=await load_agent_run_out(db, active) if active else None,
    )


async def list_agent_messages(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
    cursor: str | None,
    since: str | None,
    limit: int,
    include_tasks: bool,
) -> AgentMessageListOut:
    session, conversation = await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
    )
    statement = select(Message).where(
        Message.conversation_id == conversation.id,
        Message.deleted_at.is_(None),
    )
    visible = await retention_filter(db, user, Message.created_at)
    if visible is not None:
        statement = statement.where(visible)
    if since:
        try:
            since_at = datetime.fromisoformat(since)
        except ValueError:
            since_at = None
        if since_at is not None:
            if since_at.tzinfo is None:
                since_at = since_at.replace(tzinfo=timezone.utc)
            statement = statement.where(Message.created_at > since_at)
        else:
            boundary = (
                await db.execute(
                    select(Message.created_at).where(
                        Message.id == since,
                        Message.conversation_id == conversation.id,
                        Message.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if boundary is None:
                raise http_error("invalid_since", "invalid Agent message boundary", 422)
            statement = statement.where(
                or_(
                    Message.created_at > boundary,
                    and_(Message.created_at == boundary, Message.id > since),
                )
            )
    current = dec_cursor(cursor)
    if current is not None:
        created_at = cursor_field_datetime(current, "ca")
        current_id = cursor_field_str(current, "id")
        statement = statement.where(
            or_(
                Message.created_at < created_at,
                and_(Message.created_at == created_at, Message.id < current_id),
            )
        )
    rows = list(
        (
            await db.execute(
                statement.order_by(desc(Message.created_at), desc(Message.id)).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    page_desc = rows[:limit]
    page = list(reversed(page_desc))
    next_cursor = None
    if len(rows) > limit and page_desc:
        oldest = page_desc[-1]
        next_cursor = enc_cursor(
            {"ca": oldest.created_at.isoformat(), "id": oldest.id}
        )
    assistant_ids = [
        message.id for message in page if message.role == Role.ASSISTANT.value
    ]
    runs = (
        list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        AgentRun.agent_session_id == session.id,
                        AgentRun.assistant_message_id.in_(assistant_ids),
                    )
                    .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if assistant_ids
        else []
    )
    refs_by_run, tools_by_run = await load_run_parts(db, runs)
    generations: list[GenerationOut] = []
    completions: list[CompletionOut] = []
    images: list[ImageOut] = []
    if include_tasks and assistant_ids:
        generation_rows = list(
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == user.id,
                        Generation.message_id.in_(assistant_ids),
                    )
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                    .limit(max(100, len(assistant_ids) * 4))
                )
            )
            .scalars()
            .all()
        )
        completion_rows = list(
            (
                await db.execute(
                    select(Completion)
                    .where(
                        Completion.user_id == user.id,
                        Completion.message_id.in_(assistant_ids),
                    )
                    .order_by(Completion.created_at.asc(), Completion.id.asc())
                    .limit(max(100, len(assistant_ids)))
                )
            )
            .scalars()
            .all()
        )
        generations = [GenerationOut.model_validate(item) for item in generation_rows]
        completions = [CompletionOut.model_validate(item) for item in completion_rows]
        if generation_rows:
            image_statement = select(Image).where(
                Image.user_id == user.id,
                Image.owner_generation_id.in_([item.id for item in generation_rows]),
                Image.deleted_at.is_(None),
            )
            image_visible = await retention_filter(db, user, Image.created_at)
            if image_visible is not None:
                image_statement = image_statement.where(image_visible)
            image_rows = list(
                (await db.execute(image_statement)).scalars().all()
            )
            images = [image_to_out(item) for item in image_rows]
    return AgentMessageListOut(
        items=[MessageOut.model_validate(message) for message in page],
        runs=[
            agent_run_out(
                run,
                references=refs_by_run.get(run.id),
                tool_calls=tools_by_run.get(run.id),
            )
            for run in runs
        ],
        next_cursor=next_cursor,
        generations=generations,
        completions=completions,
        images=images,
    )


__all__ = [
    "get_agent_session_out",
    "get_owned_agent_session",
    "list_agent_messages",
    "list_agent_sessions",
    "load_agent_run_out",
    "load_run_parts",
    "retention_filter",
]
