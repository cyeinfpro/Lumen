"""Workflow validation and preview services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..domain.models import WorkflowCommand, WorkflowPlan
from ..domain.validation import ValidationResult
from ..ports.assets import WorkflowAssetPort
from ..ports.providers import WorkflowPreviewPort
from .errors import WorkflowValidationError
from .policy_registry import WorkflowPolicyRegistry


@dataclass(frozen=True)
class ValidateWorkflow:
    policies: WorkflowPolicyRegistry
    assets: WorkflowAssetPort | None = None

    async def execute(
        self,
        command: WorkflowCommand,
    ) -> tuple[WorkflowPlan, ValidationResult]:
        policy = self.policies.require(command.workflow_kind)
        policy_result = policy.validate(command)
        if not policy_result.is_valid:
            return policy.plan(command), policy_result
        plan = policy.plan(command)
        if self.assets is None:
            return plan, policy_result
        asset_result = await self.assets.validate_assets(command=command, plan=plan)
        return plan, asset_result


@dataclass(frozen=True)
class PreviewWorkflow:
    validator: ValidateWorkflow
    preview_port: WorkflowPreviewPort

    async def execute(self, command: WorkflowCommand) -> Mapping[str, Any]:
        plan, result = await self.validator.execute(command)
        if not result.is_valid:
            raise WorkflowValidationError(result.issues)
        return await self.preview_port.preview(command=command, plan=plan)


__all__ = ["PreviewWorkflow", "ValidateWorkflow"]
