"""Asset validation port."""

from __future__ import annotations

from typing import Protocol

from ..domain.models import WorkflowCommand, WorkflowPlan
from ..domain.validation import ValidationResult


class WorkflowAssetPort(Protocol):
    async def validate_assets(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> ValidationResult: ...


__all__ = ["WorkflowAssetPort"]
