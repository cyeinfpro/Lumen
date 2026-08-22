"""Agent run ownership, snapshots, and cancellation."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_events import (
    AGENT_RUN_ACTIVE_STATUSES,
    AGENT_RUN_TERMINAL_STATUSES,
    EV_AGENT_RUN_CANCELLED,
    AgentRunStatus,
)
from lumen_core.constants import MessageStatus
from lumen_core.model_entities import AgentRun, AgentSession, Conversation, Message, User

from ...audit import hash_email, request_ip_hash, write_audit
from ...deps import durable_session_id_from_db
from ...redis_client import get_redis
from ..active_user import (
    ActiveUserFenceError,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from .common import (
    http_error,
    publish_agent_events_best_effort,
    release_queued_agent_hold,
    stage_agent_event,
)
from .repository import load_agent_run_out


logger = logging.getLogger(__name__)


async def get_owned_agent_run(
    db: AsyncSession,
    *,
    run_id: str,
    user_id: str,
    for_update: bool = False,
) -> AgentRun:
    statement = (
        select(AgentRun)
        .join(AgentSession, AgentSession.id == AgentRun.agent_session_id)
        .join(Conversation, Conversation.id == AgentSession.conversation_id)
        .where(
            AgentRun.id == run_id,
            AgentRun.user_id == user_id,
            AgentSession.user_id == user_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
    )
    if for_update:
        statement = statement.with_for_update(of=AgentRun)
    run = (await db.execute(statement)).scalar_one_or_none()
    if run is None:
        raise http_error("not_found", "Agent run not found", 404)
    return run


async def get_agent_run_snapshot(
    db: AsyncSession,
    *,
    run_id: str,
    user: User,
):
    run = await get_owned_agent_run(
        db,
        run_id=run_id,
        user_id=user.id,
    )
    return await load_agent_run_out(db, run)


async def get_active_agent_run_snapshot(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
):
    run = (
        await db.execute(
            select(AgentRun)
            .join(AgentSession, AgentSession.id == AgentRun.agent_session_id)
            .join(Conversation, Conversation.id == AgentSession.conversation_id)
            .where(
                AgentSession.id == session_id,
                AgentSession.user_id == user.id,
                AgentRun.user_id == user.id,
                AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return await load_agent_run_out(db, run) if run is not None else None


async def cancel_agent_run(
    db: AsyncSession,
    *,
    run_id: str,
    user: User,
    request: Any | None,
):
    expected_mode = account_mode_from_user(user)
    try:
        snapshot = await lock_active_user_snapshot(
            db,
            user.id,
            expected_mode,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    run = await get_owned_agent_run(
        db,
        run_id=run_id,
        user_id=snapshot.user.id,
        for_update=True,
    )
    if run.status in AGENT_RUN_TERMINAL_STATUSES:
        output = await load_agent_run_out(db, run)
        await db.rollback()
        return output

    previous_status = run.status
    now = datetime.now(timezone.utc)
    run.cancel_requested_at = run.cancel_requested_at or now
    run.status = AgentRunStatus.CANCELLED.value
    run.finished_at = now
    run.execution_epoch += 1
    run.error_code = "agent_cancelled"
    run.error_message = None
    assistant_message = (
        await db.execute(
            select(Message)
            .where(
                Message.id == run.assistant_message_id,
                Message.conversation_id.in_(
                    select(AgentSession.conversation_id).where(
                        AgentSession.id == run.agent_session_id
                    )
                ),
                Message.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if assistant_message is not None:
        assistant_message.status = MessageStatus.CANCELED.value
    if previous_status == AgentRunStatus.QUEUED.value:
        await release_queued_agent_hold(
            db,
            run=run,
            reason="user_cancelled",
        )
    event = stage_agent_event(db, run=run, event_name=EV_AGENT_RUN_CANCELLED)
    await write_audit(
        db,
        event_type="agent.run.cancel",
        user_id=snapshot.user.id,
        actor_email_hash=hash_email(snapshot.user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "agent_run_id": run.id,
            "agent_session_id": run.agent_session_id,
            "previous_status": previous_status,
            "execution_epoch": run.execution_epoch,
        },
        autocommit=False,
    )
    await db.commit()
    try:
        await get_redis().set(f"agent:{run.id}:cancel", "1", ex=3600)
    except Exception:
        logger.warning(
            "agent cancel signal failed run=%s user=%s",
            run.id,
            snapshot.user.id,
            exc_info=True,
        )
    await publish_agent_events_best_effort(
        user_id=snapshot.user.id,
        agent_session_id=run.agent_session_id,
        events=[event],
    )
    await db.refresh(run)
    return await load_agent_run_out(db, run)


__all__ = [
    "cancel_agent_run",
    "get_active_agent_run_snapshot",
    "get_agent_run_snapshot",
    "get_owned_agent_run",
]
