"""Public Agent run snapshot and cancellation routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schema_models import AgentRunContinueIn, AgentRunOut

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..services.agent.runs import (
    cancel_agent_run,
    continue_agent_run,
    get_agent_run_snapshot,
)


router = APIRouter(prefix="/agent/runs", tags=["agent"])


@router.get("/{run_id}", response_model=AgentRunOut)
async def get_agent_run(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunOut:
    return await get_agent_run_snapshot(db, run_id=run_id, user=user)


@router.post(
    "/{run_id}/cancel",
    response_model=AgentRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_agent_run_cancel(
    run_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunOut:
    return await cancel_agent_run(
        db,
        run_id=run_id,
        user=user,
        request=request,
    )


@router.post(
    "/{run_id}/continue",
    response_model=AgentRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_agent_run_continue(
    run_id: str,
    body: AgentRunContinueIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentRunOut:
    return await continue_agent_run(
        db,
        run_id=run_id,
        body=body,
        user=user,
        request=request,
    )


__all__ = ["router"]
