"""Capability-authenticated internal Agent tool gateway."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.agent_capability import (
    AgentCapabilityClaims,
    AgentCapabilityError,
    verify_agent_capability,
)
from lumen_core.schema_models import (
    AgentProviderDispatchIn,
    AgentProviderDispatchOut,
    AgentToolCreateImageIn,
    AgentToolCreateImageOut,
)

from ..config import settings
from ..db import get_db
from ..services.agent.tools import (
    authorize_provider_dispatch,
    submit_create_image_tool,
)


router = APIRouter(prefix="/internal/agent", tags=["internal-agent"])


def _capability_error(error: AgentCapabilityError) -> HTTPException:
    status_code = 503 if error.code == "agent_capability_unconfigured" else 401
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": error.code, "message": str(error)}},
    )


async def require_agent_capability(request: Request) -> AgentCapabilityClaims:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "agent_capability_required",
                    "message": "Agent capability required",
                }
            },
        )
    try:
        return verify_agent_capability(
            settings.agent_tool_capability_secret,
            token.strip(),
        )
    except AgentCapabilityError as exc:
        raise _capability_error(exc) from None


@router.post(
    "/runs/{run_id}/tools/create-image",
    response_model=AgentToolCreateImageOut,
)
async def post_internal_agent_create_image(
    run_id: str,
    body: AgentToolCreateImageIn,
    claims: Annotated[AgentCapabilityClaims, Depends(require_agent_capability)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentToolCreateImageOut:
    return await submit_create_image_tool(
        db,
        run_id=run_id,
        claims=claims,
        body=body,
    )


@router.post(
    "/runs/{run_id}/provider-dispatch",
    response_model=AgentProviderDispatchOut,
)
async def post_internal_agent_provider_dispatch(
    run_id: str,
    body: AgentProviderDispatchIn,
    claims: Annotated[AgentCapabilityClaims, Depends(require_agent_capability)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AgentProviderDispatchOut:
    return await authorize_provider_dispatch(
        db,
        run_id=run_id,
        claims=claims,
        body=body,
    )


__all__ = ["require_agent_capability", "router"]
