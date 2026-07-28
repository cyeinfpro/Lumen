"""Explicit workflow application composition."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .adapters.http_operations import (
    ApparelWorkflowOperationsAdapter,
    ModelLibraryWorkflowOperationsAdapter,
    PosterWorkflowOperationsAdapter,
    ProjectWorkflowOperationsAdapter,
)
from .application.commands import CancelWorkflowRun
from .application.policy_registry import WorkflowPolicyRegistry
from .application.preflight import PreviewWorkflow, ValidateWorkflow
from .application.queries import GetWorkflowRun
from .application.runtime_state import WorkflowRuntimeState
from .application.submit import SubmitWorkflow
from .application.transaction import WorkflowTransactionFactory
from .domain.policies import WorkflowPolicy
from .ports.assets import WorkflowAssetPort
from .ports.providers import WorkflowPreviewPort
from .ports.queue import WorkflowQueuePort
from .ports.repositories import WorkflowRepository
from .transport.http.use_cases import WorkflowHttpUseCases


@dataclass(frozen=True, slots=True)
class WorkflowApplication:
    policies: WorkflowPolicyRegistry | None = None
    validate: ValidateWorkflow | None = None
    preview: PreviewWorkflow | None = None
    submit: SubmitWorkflow | None = None
    get_run: GetWorkflowRun | None = None
    cancel: CancelWorkflowRun | None = None
    http: WorkflowHttpUseCases | None = None
    runtime: WorkflowRuntimeState | None = None

    def require_http(self) -> WorkflowHttpUseCases:
        if self.http is None:
            raise RuntimeError("workflow HTTP use cases are not configured")
        return self.http

    def reset_runtime(self) -> None:
        if self.runtime is None:
            raise RuntimeError("workflow runtime is not configured")
        self.runtime.reset()


def _build_production_http_use_cases(
    runtime: WorkflowRuntimeState,
) -> WorkflowHttpUseCases:
    return WorkflowHttpUseCases(
        projects=ProjectWorkflowOperationsAdapter(runtime),
        apparel=ApparelWorkflowOperationsAdapter(runtime),
        model_library=ModelLibraryWorkflowOperationsAdapter(),
        poster=PosterWorkflowOperationsAdapter(),
    )


def build_workflow_application(
    *,
    policies: Iterable[WorkflowPolicy] | None = None,
    repository: WorkflowRepository | None = None,
    assets: WorkflowAssetPort | None = None,
    preview: WorkflowPreviewPort | None = None,
    queue: WorkflowQueuePort | None = None,
    transaction_factory: WorkflowTransactionFactory | None = None,
    include_http: bool = False,
    runtime: WorkflowRuntimeState | None = None,
) -> WorkflowApplication:
    core_dependencies = (
        policies,
        repository,
        assets,
        preview,
        queue,
        transaction_factory,
    )
    has_core_dependencies = any(
        dependency is not None for dependency in core_dependencies
    )
    if has_core_dependencies and not all(
        dependency is not None for dependency in core_dependencies
    ):
        raise TypeError("workflow core dependencies must be provided together")

    runtime_state = runtime or (WorkflowRuntimeState() if include_http else None)
    http = (
        _build_production_http_use_cases(runtime_state)
        if runtime_state is not None and include_http
        else None
    )
    if not has_core_dependencies:
        if http is None:
            raise TypeError("workflow application has no configured use cases")
        return WorkflowApplication(http=http, runtime=runtime_state)

    assert policies is not None
    assert repository is not None
    assert assets is not None
    assert preview is not None
    assert queue is not None
    assert transaction_factory is not None
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
        http=http,
        runtime=runtime_state,
    )


__all__ = [
    "WorkflowApplication",
    "build_workflow_application",
]
