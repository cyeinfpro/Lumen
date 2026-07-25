"""Workflow query services."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import WorkflowRunSnapshot
from ..ports.repositories import WorkflowRepository
from .errors import WorkflowNotFoundError


@dataclass(frozen=True)
class GetWorkflowRun:
    repository: WorkflowRepository

    async def execute(self, *, user_id: str, run_id: str) -> WorkflowRunSnapshot:
        run = await self.repository.get(user_id=user_id, run_id=run_id)
        if run is None:
            raise WorkflowNotFoundError(run_id)
        return run


__all__ = ["GetWorkflowRun"]
