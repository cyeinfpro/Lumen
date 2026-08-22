"""Sanitized administrator health view for the private Agent Runtime."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import AdminUser
from ..services.agent_health import agent_health_snapshot


router = APIRouter(prefix="/admin/agent", tags=["admin-agent"])


class AdminAgentHealthOut(BaseModel):
    enabled: bool
    operational: bool
    runtime_auth_configured: bool
    tool_gateway_configured: bool
    runtime_live: bool | None
    runtime_ready: bool | None
    runtime_version: str | None
    error_code: str | None


@router.get("/health", response_model=AdminAgentHealthOut)
async def get_admin_agent_health(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AdminAgentHealthOut:
    snapshot = await agent_health_snapshot(db)
    return AdminAgentHealthOut(
        enabled=snapshot.enabled,
        operational=snapshot.operational,
        runtime_auth_configured=snapshot.runtime_auth_configured,
        tool_gateway_configured=snapshot.tool_gateway_configured,
        runtime_live=snapshot.runtime_live,
        runtime_ready=snapshot.runtime_ready,
        runtime_version=snapshot.runtime_version,
        error_code=snapshot.error_code,
    )


__all__ = ["AdminAgentHealthOut", "router"]
