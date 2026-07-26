from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.workflows import (
    AssetRequirement,
    CostEstimate,
    WorkflowCommand,
    WorkflowInput,
    WorkflowKind,
    WorkflowPlan,
    WorkflowPolicyNotFoundError,
    WorkflowPolicyRegistry,
    WorkflowRunSnapshot,
    WorkflowStepPlan,
    WorkflowValidationError,
    build_workflow_application,
)
from app.workflows.domain.validation import ValidationIssue, ValidationResult


@dataclass(frozen=True)
class _Policy:
    kind: WorkflowKind
    valid: bool = True

    def validate(self, command: WorkflowCommand) -> ValidationResult:
        assert command.workflow_kind is self.kind
        if self.valid:
            return ValidationResult.valid()
        return ValidationResult.invalid(
            ValidationIssue("invalid_input", "workflow input is invalid", "input")
        )

    def plan(self, command: WorkflowCommand) -> WorkflowPlan:
        del command
        return WorkflowPlan(
            steps=(WorkflowStepPlan("prepare", "Prepare"),),
            required_assets=(AssetRequirement("product", minimum=1, maximum=3),),
            estimated_cost=CostEstimate(10, 20),
        )


class _Repository:
    def __init__(self) -> None:
        self.created = 0
        self.existing: WorkflowRunSnapshot | None = None

    async def find_by_idempotency(
        self,
        *,
        user_id: str,
        idempotency_key: str,
    ) -> WorkflowRunSnapshot | None:
        del user_id, idempotency_key
        return self.existing

    async def create(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> WorkflowRunSnapshot:
        self.created += 1
        return WorkflowRunSnapshot(
            run_id="run-1",
            user_id=command.user_id,
            workflow_kind=command.workflow_kind,
            status="running",
            current_step=plan.steps[0].key,
        )

    async def get(
        self,
        *,
        user_id: str,
        run_id: str,
        for_update: bool = False,
    ) -> WorkflowRunSnapshot | None:
        del user_id, run_id, for_update
        return self.existing

    async def cancel(self, run: WorkflowRunSnapshot) -> WorkflowRunSnapshot:
        return WorkflowRunSnapshot(
            run_id=run.run_id,
            user_id=run.user_id,
            workflow_kind=run.workflow_kind,
            status="cancelled",
            current_step=run.current_step,
        )


class _Assets:
    async def validate_assets(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> ValidationResult:
        del command, plan
        return ValidationResult.valid()


class _Preview:
    async def preview(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> dict[str, Any]:
        return {"kind": command.workflow_kind.value, "steps": len(plan.steps)}


class _Queue:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def publish_created(self, run: WorkflowRunSnapshot) -> None:
        self.events.append(f"published:{run.run_id}")

    async def publish_cancelled(self, run: WorkflowRunSnapshot) -> None:
        self.events.append(f"cancelled:{run.run_id}")


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> _Transaction:
        self.events.append("begin")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        self.events.append("end")

    async def commit(self) -> None:
        self.events.append("commit")


def _command() -> WorkflowCommand:
    return WorkflowCommand(
        user_id="user-1",
        workflow_kind=WorkflowKind.APPAREL_SHOWCASE,
        input=WorkflowInput({"product_image_ids": ["image-1"]}),
        idempotency_key="idem-1",
    )


def test_policy_registry_rejects_duplicates_and_missing_kinds() -> None:
    policy = _Policy(WorkflowKind.APPAREL_SHOWCASE)
    with pytest.raises(ValueError, match="duplicate workflow policy"):
        WorkflowPolicyRegistry([policy, policy])

    registry = WorkflowPolicyRegistry([policy])
    assert registry.require(WorkflowKind.APPAREL_SHOWCASE) is policy
    with pytest.raises(WorkflowPolicyNotFoundError):
        registry.require(WorkflowKind.POSTER_DESIGN)


@pytest.mark.asyncio
async def test_application_services_own_commit_then_publish_boundary() -> None:
    events: list[str] = []
    repository = _Repository()
    application = build_workflow_application(
        policies=[_Policy(WorkflowKind.APPAREL_SHOWCASE)],
        repository=repository,
        assets=_Assets(),
        preview=_Preview(),
        queue=_Queue(events),
        transaction_factory=lambda: _Transaction(events),
    )

    run = await application.submit.execute(_command())

    assert run.run_id == "run-1"
    assert repository.created == 1
    assert events == ["begin", "commit", "end", "published:run-1"]
    assert await application.preview.execute(_command()) == {
        "kind": "apparel_model_showcase",
        "steps": 1,
    }


@pytest.mark.asyncio
async def test_application_submit_is_idempotent_without_new_side_effects() -> None:
    events: list[str] = []
    repository = _Repository()
    repository.existing = WorkflowRunSnapshot(
        run_id="run-existing",
        user_id="user-1",
        workflow_kind=WorkflowKind.APPAREL_SHOWCASE,
        status="running",
        current_step="prepare",
    )
    application = build_workflow_application(
        policies=[_Policy(WorkflowKind.APPAREL_SHOWCASE)],
        repository=repository,
        assets=_Assets(),
        preview=_Preview(),
        queue=_Queue(events),
        transaction_factory=lambda: _Transaction(events),
    )

    run = await application.submit.execute(_command())

    assert run is repository.existing
    assert repository.created == 0
    assert events == []


@pytest.mark.asyncio
async def test_application_cancel_commits_before_publication() -> None:
    events: list[str] = []
    repository = _Repository()
    repository.existing = WorkflowRunSnapshot(
        run_id="run-1",
        user_id="user-1",
        workflow_kind=WorkflowKind.APPAREL_SHOWCASE,
        status="running",
        current_step="prepare",
    )
    application = build_workflow_application(
        policies=[_Policy(WorkflowKind.APPAREL_SHOWCASE)],
        repository=repository,
        assets=_Assets(),
        preview=_Preview(),
        queue=_Queue(events),
        transaction_factory=lambda: _Transaction(events),
    )

    cancelled = await application.cancel.execute(user_id="user-1", run_id="run-1")

    assert cancelled.status == "cancelled"
    assert events == ["begin", "commit", "end", "cancelled:run-1"]


@pytest.mark.asyncio
async def test_application_validation_is_side_effect_free() -> None:
    events: list[str] = []
    repository = _Repository()
    application = build_workflow_application(
        policies=[_Policy(WorkflowKind.APPAREL_SHOWCASE, valid=False)],
        repository=repository,
        assets=_Assets(),
        preview=_Preview(),
        queue=_Queue(events),
        transaction_factory=lambda: _Transaction(events),
    )

    with pytest.raises(WorkflowValidationError) as excinfo:
        await application.submit.execute(_command())

    assert excinfo.value.issues[0].code == "invalid_input"
    assert repository.created == 0
    assert events == []
