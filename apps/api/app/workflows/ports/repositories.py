"""Persistence ports owned by the workflow application layer."""

from __future__ import annotations

from typing import Protocol

from ..domain.models import WorkflowCommand, WorkflowPlan, WorkflowRunSnapshot


class WorkflowRepository(Protocol):
    async def find_by_idempotency(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> WorkflowRunSnapshot | None: ...

    async def create(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> WorkflowRunSnapshot: ...

    async def get(
        self,
        *,
        user_id: str,
        run_id: str,
        for_update: bool = False,
    ) -> WorkflowRunSnapshot | None: ...

    async def cancel(self, run: WorkflowRunSnapshot) -> WorkflowRunSnapshot: ...


__all__ = ["WorkflowRepository"]
