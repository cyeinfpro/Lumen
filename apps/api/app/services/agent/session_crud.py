"""Agent session creation, mutable settings, and visibility deletion."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_events import (
    AGENT_RUN_ACTIVE_STATUSES,
    EV_AGENT_RUN_CANCELLED,
    AgentRunStatus,
)
from lumen_core.constants import MessageStatus
from lumen_core.model_base import new_uuid7
from lumen_core.model_entities import (
    AgentRun,
    AgentSession,
    AgentSessionImage,
    Conversation,
    Message,
    SystemPrompt,
    User,
    UserMemoryScope,
)
from lumen_core.schema_models import (
    AgentSessionBranchIn,
    AgentSessionCreateIn,
    AgentSessionOut,
    AgentSessionPatchIn,
)

from ...audit import hash_email, request_ip_hash, write_audit
from ...deps import durable_session_id_from_db
from ...redis_client import get_redis
from ..active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    lock_active_user,
)
from .common import (
    http_error,
    publish_agent_events_best_effort,
    release_queued_agent_hold,
    stage_agent_event,
)
from .presentation import (
    agent_default_params,
    agent_session_out,
    conversation_agent_defaults,
)
from .repository import get_owned_agent_session, retention_filter


logger = logging.getLogger(__name__)


async def _validate_system_prompt(
    db: AsyncSession,
    *,
    user_id: str,
    prompt_id: str | None,
) -> None:
    if prompt_id is None:
        return
    exists = (
        await db.execute(
            select(SystemPrompt.id).where(
                SystemPrompt.id == prompt_id,
                SystemPrompt.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise http_error("system_prompt_not_found", "system prompt not found", 404)


async def create_agent_session(
    db: AsyncSession,
    *,
    user: User,
    body: AgentSessionCreateIn,
    request: Any | None,
) -> AgentSessionOut:
    await _validate_system_prompt(
        db,
        user_id=user.id,
        prompt_id=body.default_system_prompt_id,
    )
    try:
        await lock_active_user(
            db,
            user.id,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    conversation = Conversation(
        user_id=user.id,
        title=body.title,
        default_system=body.default_system,
        default_system_prompt_id=body.default_system_prompt_id,
        default_params=agent_default_params(
            image_defaults=body.image_defaults,
            allow_image=body.allow_image,
            allow_web_search=body.allow_web_search,
            allow_file_tools=body.allow_file_tools,
        ),
    )
    db.add(conversation)
    await db.flush()
    session = AgentSession(
        user_id=user.id,
        conversation_id=conversation.id,
        runtime_version="",
    )
    db.add(session)
    await db.flush()
    await write_audit(
        db,
        event_type="agent.session.create",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={"agent_session_id": session.id, "conversation_id": conversation.id},
        autocommit=False,
    )
    await db.commit()
    await db.refresh(session)
    await db.refresh(conversation)
    return agent_session_out(session, conversation)


async def patch_agent_session(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
    body: AgentSessionPatchIn,
    request: Any | None,
) -> AgentSessionOut:
    try:
        await lock_active_user(
            db,
            user.id,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    session, conversation = await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
        for_update=True,
    )
    if "default_system_prompt_id" in body.model_fields_set:
        await _validate_system_prompt(
            db,
            user_id=user.id,
            prompt_id=body.default_system_prompt_id,
        )
    if "active_scope_id" in body.model_fields_set and body.active_scope_id is not None:
        scope = (
            await db.execute(
                select(UserMemoryScope.id).where(
                    UserMemoryScope.id == body.active_scope_id,
                    UserMemoryScope.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if scope is None:
            raise http_error("memory_scope_not_found", "memory scope not found", 404)
    for field in ("title", "pinned", "archived", "memory_disabled"):
        value = getattr(body, field)
        if value is not None:
            setattr(conversation, field, value)
    for field in ("active_scope_id", "default_system", "default_system_prompt_id"):
        if field in body.model_fields_set:
            setattr(conversation, field, getattr(body, field))
    (
        current_defaults,
        current_allow_image,
        current_allow_web_search,
        current_allow_file_tools,
    ) = conversation_agent_defaults(conversation)
    if any(
        value is not None
        for value in (
            body.image_defaults,
            body.allow_image,
            body.allow_web_search,
            body.allow_file_tools,
        )
    ):
        conversation.default_params = agent_default_params(
            image_defaults=body.image_defaults or current_defaults,
            allow_image=(
                body.allow_image
                if body.allow_image is not None
                else current_allow_image
            ),
            allow_web_search=(
                body.allow_web_search
                if body.allow_web_search is not None
                else current_allow_web_search
            ),
            allow_file_tools=(
                body.allow_file_tools
                if body.allow_file_tools is not None
                else current_allow_file_tools
            ),
            existing=conversation.default_params,
        )
    session.updated_at = datetime.now(timezone.utc)
    await write_audit(
        db,
        event_type="agent.session.update",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "agent_session_id": session.id,
            "fields": sorted(body.model_fields_set),
        },
        autocommit=False,
    )
    await db.commit()
    await db.refresh(session)
    await db.refresh(conversation)
    return agent_session_out(session, conversation)


async def branch_agent_session(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
    body: AgentSessionBranchIn,
    request: Any | None,
) -> AgentSessionOut:
    try:
        await lock_active_user(
            db,
            user.id,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    source_session, source_conversation = await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
        for_update=True,
    )
    active_run = (
        await db.execute(
            select(AgentRun.id).where(
                AgentRun.agent_session_id == source_session.id,
                AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
            )
        )
    ).scalar_one_or_none()
    if active_run is not None:
        raise http_error(
            "agent_session_active",
            "Agent session cannot branch while a run is active",
            409,
        )

    message_statement = (
        select(Message)
        .where(
            Message.conversation_id == source_conversation.id,
            Message.deleted_at.is_(None),
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    visible = await retention_filter(db, user, Message.created_at)
    if visible is not None:
        message_statement = message_statement.where(visible)
    source_messages = list((await db.execute(message_statement)).scalars().all())
    source_catalog = list(
        (
            await db.execute(
                select(AgentSessionImage)
                .where(AgentSessionImage.agent_session_id == source_session.id)
                .order_by(AgentSessionImage.created_at, AgentSessionImage.id)
            )
        )
        .scalars()
        .all()
    )
    source_ids = {message.id for message in source_messages}
    ordered_messages: list[Message] = []
    pending_messages = list(source_messages)
    emitted_ids: set[str] = set()
    while pending_messages:
        ready = [
            message
            for message in pending_messages
            if not message.parent_message_id
            or message.parent_message_id not in source_ids
            or message.parent_message_id in emitted_ids
        ]
        if not ready:
            ready = [pending_messages[0]]
        for message in ready:
            ordered_messages.append(message)
            emitted_ids.add(message.id)
            pending_messages.remove(message)
    source_messages = ordered_messages
    requested_title = body.title.strip() if body.title else ""
    source_title = source_conversation.title.strip() or "新会话"
    branched_conversation = Conversation(
        id=new_uuid7(),
        user_id=user.id,
        title=(requested_title or f"{source_title} 分支")[:255],
        pinned=False,
        archived=False,
        last_activity_at=datetime.now(timezone.utc),
        default_params=deepcopy(source_conversation.default_params or {}),
        default_system=source_conversation.default_system,
        default_system_prompt_id=source_conversation.default_system_prompt_id,
        summary_jsonb=None,
        memory_disabled=source_conversation.memory_disabled,
        active_scope_id=source_conversation.active_scope_id,
    )
    branched_session = AgentSession(
        id=new_uuid7(),
        user_id=user.id,
        conversation_id=branched_conversation.id,
        runtime_version=source_session.runtime_version,
    )
    db.add(branched_conversation)
    await db.flush()
    db.add(branched_session)
    await db.flush()

    cloned_by_source_id: dict[str, Message] = {}
    for source_message in source_messages:
        cloned_content = deepcopy(source_message.content or {})
        cloned_content.pop("agent_run_id", None)
        clone = Message(
            id=new_uuid7(),
            conversation_id=branched_conversation.id,
            role=source_message.role,
            content=cloned_content,
            parent_message_id=None,
            intent=source_message.intent,
            status=source_message.status,
        )
        cloned_by_source_id[source_message.id] = clone
        db.add(clone)
    for source_message in source_messages:
        parent = cloned_by_source_id.get(source_message.parent_message_id or "")
        cloned_by_source_id[source_message.id].parent_message_id = (
            parent.id if parent is not None else None
        )
    for catalog_item in source_catalog:
        db.add(
            AgentSessionImage(
                id=new_uuid7(),
                agent_session_id=branched_session.id,
                user_id=user.id,
                image_id=catalog_item.image_id,
                reference_label=catalog_item.reference_label,
                role=catalog_item.role,
                display_label=catalog_item.display_label,
                source=catalog_item.source,
                active=catalog_item.active,
            )
        )

    await write_audit(
        db,
        event_type="agent.session.branch",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "source_agent_session_id": source_session.id,
            "agent_session_id": branched_session.id,
            "message_count": len(source_messages),
            "session_image_count": len(source_catalog),
        },
        autocommit=False,
    )
    await db.commit()
    await db.refresh(branched_session)
    await db.refresh(branched_conversation)
    return agent_session_out(branched_session, branched_conversation)


async def delete_agent_session(
    db: AsyncSession,
    *,
    session_id: str,
    user: User,
    request: Any | None,
) -> dict[str, bool]:
    now = datetime.now(timezone.utc)
    try:
        await lock_active_user(
            db,
            user.id,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    session, conversation = await get_owned_agent_session(
        db,
        session_id=session_id,
        user_id=user.id,
        for_update=True,
    )
    run = (
        await db.execute(
            select(AgentRun)
            .where(
                AgentRun.agent_session_id == session.id,
                AgentRun.status.in_(AGENT_RUN_ACTIVE_STATUSES),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    events: list[dict[str, Any]] = []
    if run is not None:
        previous_status = run.status
        run.cancel_requested_at = now
        run.status = AgentRunStatus.CANCELLED.value
        run.finished_at = now
        run.execution_epoch += 1
        run.error_code = "agent_cancelled"
        if previous_status == AgentRunStatus.QUEUED.value:
            await release_queued_agent_hold(
                db,
                run=run,
                reason="session_deleted",
            )
        assistant_message = (
            await db.execute(
                select(Message)
                .where(
                    Message.id == run.assistant_message_id,
                    Message.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if assistant_message is not None:
            assistant_message.status = MessageStatus.CANCELED.value
        events.append(stage_agent_event(db, run=run, event_name=EV_AGENT_RUN_CANCELLED))
    conversation.deleted_at = now
    await write_audit(
        db,
        event_type="agent.session.delete",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "agent_session_id": session.id,
            "active_run_cancelled": run is not None,
        },
        autocommit=False,
    )
    await db.commit()
    if run is not None:
        try:
            await get_redis().set(f"agent:{run.id}:cancel", "1", ex=3600)
        except Exception:
            logger.warning(
                "agent session cancel signal failed run=%s user=%s",
                run.id,
                user.id,
                exc_info=True,
            )
    await publish_agent_events_best_effort(
        user_id=user.id,
        agent_session_id=session.id,
        events=events,
    )
    return {"ok": True}


__all__ = [
    "branch_agent_session",
    "create_agent_session",
    "delete_agent_session",
    "patch_agent_session",
]
