"""HTTP error mapping for workflow application actions."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from ...application.errors import WorkflowRequestError


async def execute_workflow_action(
    action: Callable[..., Awaitable[Any]],
    **kwargs: Any,
) -> Any:
    try:
        return await action(**kwargs)
    except WorkflowRequestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
        ) from exc


__all__ = ["execute_workflow_action"]
