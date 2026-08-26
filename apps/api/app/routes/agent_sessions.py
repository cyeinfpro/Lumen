"""Public Agent session and message routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schema_models import (
    AgentMessageCreateIn,
    AgentMessageCreateOut,
    AgentMessageListOut,
    AgentRunOut,
    AgentSessionCreateIn,
    AgentSessionListOut,
    AgentSessionImageListOut,
    AgentSessionOut,
    AgentSessionPatchIn,
    AgentStatusOut,
)

from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..services.agent.runs import get_active_agent_run_snapshot
from ..services.agent.session_images import (
    eject_agent_session_image,
    list_agent_session_images,
)
from ..services.agent.sessions import agent_session_services


router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/status", response_model=AgentStatusOut)
async def agent_status(_user: CurrentUser) -> AgentStatusOut:
    return AgentStatusOut(
        enabled=True,
        tool_gateway_configured=(
            len(settings.agent_tool_capability_secret.encode("utf-8")) >= 32
        ),
    )


@router.get("/sessions", response_model=AgentSessionListOut)
async def get_agent_sessions(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    q: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> AgentSessionListOut:
    return await agent_session_services.list_agent_sessions(
        db,
        user=user,
        cursor=cursor,
        query=q,
        limit=limit,
    )


@router.post(
    "/sessions",
    response_model=AgentSessionOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_agent_session(
    body: AgentSessionCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionOut:
    await MESSAGES_LIMITER.check(get_redis(), f"rl:agent:session:{user.id}")
    return await agent_session_services.create_agent_session(
        db,
        user=user,
        body=body,
        request=request,
    )


@router.get("/sessions/{session_id}", response_model=AgentSessionOut)
async def get_agent_session(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionOut:
    return await agent_session_services.get_agent_session_out(
        db,
        session_id=session_id,
        user=user,
    )


@router.patch(
    "/sessions/{session_id}",
    response_model=AgentSessionOut,
    dependencies=[Depends(verify_csrf)],
)
async def update_agent_session(
    session_id: str,
    body: AgentSessionPatchIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionOut:
    return await agent_session_services.patch_agent_session(
        db,
        session_id=session_id,
        user=user,
        body=body,
        request=request,
    )


@router.delete(
    "/sessions/{session_id}",
    dependencies=[Depends(verify_csrf)],
)
async def remove_agent_session(
    session_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await agent_session_services.delete_agent_session(
        db,
        session_id=session_id,
        user=user,
        request=request,
    )


@router.get(
    "/sessions/{session_id}/messages",
    response_model=AgentMessageListOut,
)
async def get_agent_messages(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    since: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    include: str | None = Query(default=None),
) -> AgentMessageListOut:
    include_tasks = "tasks" in {
        value.strip() for value in (include or "").split(",") if value.strip()
    }
    return await agent_session_services.list_agent_messages(
        db,
        session_id=session_id,
        user=user,
        cursor=cursor,
        since=since,
        limit=limit,
        include_tasks=include_tasks,
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=AgentMessageCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_agent_message(
    session_id: str,
    body: AgentMessageCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentMessageCreateOut:
    await MESSAGES_LIMITER.check(get_redis(), f"rl:agent:msg:{user.id}")
    return await agent_session_services.submit_agent_message(
        db,
        session_id=session_id,
        user=user,
        body=body,
        request=request,
    )


@router.get(
    "/sessions/{session_id}/active-run",
    response_model=AgentRunOut | None,
)
async def get_agent_active_run(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunOut | None:
    return await get_active_agent_run_snapshot(
        db,
        session_id=session_id,
        user=user,
    )


@router.get(
    "/sessions/{session_id}/images",
    response_model=AgentSessionImageListOut,
)
async def get_agent_session_images(
    session_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionImageListOut:
    return await list_agent_session_images(
        db,
        session_id=session_id,
        user=user,
    )


@router.delete(
    "/sessions/{session_id}/images/{image_id}",
    response_model=AgentSessionImageListOut,
    dependencies=[Depends(verify_csrf)],
)
async def delete_agent_session_image(
    session_id: str,
    image_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentSessionImageListOut:
    return await eject_agent_session_image(
        db,
        session_id=session_id,
        image_id=image_id,
        user=user,
    )


__all__ = ["router"]
