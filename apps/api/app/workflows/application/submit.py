"""Workflow submission command service."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import WorkflowCommand, WorkflowRunSnapshot
from ..ports.queue import WorkflowQueuePort
from ..ports.repositories import WorkflowRepository
from .errors import WorkflowValidationError
from .preflight import ValidateWorkflow
from .transaction import WorkflowTransactionFactory


@dataclass(frozen=True)
class SubmitWorkflow:
    repository: WorkflowRepository
    queue: WorkflowQueuePort
    validator: ValidateWorkflow
    transaction_factory: WorkflowTransactionFactory

    async def execute(self, command: WorkflowCommand) -> WorkflowRunSnapshot:
        existing = await self.repository.find_by_idempotency(
            user_id=command.user_id,
            idempotency_key=command.idempotency_key,
        )
        if existing is not None:
            return existing
        plan, validation = await self.validator.execute(command)
        if not validation.is_valid:
            raise WorkflowValidationError(validation.issues)
        async with self.transaction_factory() as transaction:
            run = await self.repository.create(command=command, plan=plan)
            await transaction.commit()
        await self.queue.publish_created(run)
        return run


__all__ = ["SubmitWorkflow"]
