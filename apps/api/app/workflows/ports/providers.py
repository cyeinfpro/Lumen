"""Provider-facing workflow preview port."""

from __future__ import annotations

from typing import Protocol

from ..domain.json_types import JsonMapping
from ..domain.models import WorkflowCommand, WorkflowPlan


class WorkflowPreviewPort(Protocol):
    async def preview(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> JsonMapping: ...


__all__ = ["WorkflowPreviewPort"]
