"""Workflow mutation services."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import WorkflowRunSnapshot
from ..ports.queue import WorkflowQueuePort
from ..ports.repositories import WorkflowRepository
from .errors import WorkflowNotFoundError
from .transaction import WorkflowTransactionFactory


@dataclass(frozen=True)
class CancelWorkflowRun:
    repository: WorkflowRepository
    queue: WorkflowQueuePort
    transaction_factory: WorkflowTransactionFactory

    async def execute(self, *, user_id: str, run_id: str) -> WorkflowRunSnapshot:
        async with self.transaction_factory() as transaction:
            run = await self.repository.get(
                user_id=user_id,
                run_id=run_id,
                for_update=True,
            )
            if run is None:
                raise WorkflowNotFoundError(run_id)
            cancelled = await self.repository.cancel(run)
            await transaction.commit()
        await self.queue.publish_cancelled(cancelled)
        return cancelled


__all__ = ["CancelWorkflowRun"]
