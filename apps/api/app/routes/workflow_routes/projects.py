"""Workflow project query routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schema_models.workflows import (
    WorkflowRunListItemOut,
    WorkflowRunListOut,
)

from ...db import get_db
from ...deps import CurrentUser
from ...workflows.adapters.sqlalchemy_reads import SQLAlchemyWorkflowRunReadAdapter
from ...workflows.application.errors import InvalidWorkflowCursorError
from ...workflows.application.queries import ListWorkflowRuns


router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.get("", response_model=WorkflowRunListOut)
async def list_workflows(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: str | None = Query(default=None),  # noqa: A002 - API field name
    cursor: Annotated[str | None, Query(max_length=512)] = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> WorkflowRunListOut:
    try:
        result = await ListWorkflowRuns(SQLAlchemyWorkflowRunReadAdapter(db)).execute(
            user_id=user.id,
            workflow_type=type,
            cursor=cursor,
            limit=limit,
        )
    except InvalidWorkflowCursorError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": exc.code,
                    "message": "cursor is invalid",
                }
            },
        ) from exc
    return WorkflowRunListOut(
        items=[WorkflowRunListItemOut.model_validate(item) for item in result.items],
        next_cursor=result.next_cursor,
    )


__all__ = ["list_workflows", "router"]
