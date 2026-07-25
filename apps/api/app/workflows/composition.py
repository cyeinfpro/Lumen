"""Explicit workflow application composition.

The API startup layer supplies persistence, queue, provider, and transaction
adapters. Importing this module has no side effects and creates no global
registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .application.commands import CancelWorkflowRun
from .application.policy_registry import WorkflowPolicyRegistry
from .application.preflight import PreviewWorkflow, ValidateWorkflow
from .application.queries import GetWorkflowRun
from .application.submit import SubmitWorkflow
from .application.transaction import WorkflowTransactionFactory
from .domain.policies import WorkflowPolicy
from .ports.assets import WorkflowAssetPort
from .ports.providers import WorkflowPreviewPort
from .ports.queue import WorkflowQueuePort
from .ports.repositories import WorkflowRepository


@dataclass(frozen=True)
class WorkflowApplication:
    policies: WorkflowPolicyRegistry
    validate: ValidateWorkflow
    preview: PreviewWorkflow
    submit: SubmitWorkflow
    get_run: GetWorkflowRun
    cancel: CancelWorkflowRun


def build_workflow_application(
    *,
    policies: Iterable[WorkflowPolicy],
    repository: WorkflowRepository,
    assets: WorkflowAssetPort,
    preview: WorkflowPreviewPort,
    queue: WorkflowQueuePort,
    transaction_factory: WorkflowTransactionFactory,
) -> WorkflowApplication:
    registry = WorkflowPolicyRegistry(policies)
    validator = ValidateWorkflow(registry, assets)
    return WorkflowApplication(
        policies=registry,
        validate=validator,
        preview=PreviewWorkflow(validator, preview),
        submit=SubmitWorkflow(
            repository=repository,
            queue=queue,
            validator=validator,
            transaction_factory=transaction_factory,
        ),
        get_run=GetWorkflowRun(repository),
        cancel=CancelWorkflowRun(
            repository=repository,
            queue=queue,
            transaction_factory=transaction_factory,
        ),
    )


__all__ = ["WorkflowApplication", "build_workflow_application"]
