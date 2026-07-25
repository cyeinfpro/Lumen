"""Provider-facing workflow preview port."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..domain.models import WorkflowCommand, WorkflowPlan


class WorkflowPreviewPort(Protocol):
    async def preview(
        self,
        *,
        command: WorkflowCommand,
        plan: WorkflowPlan,
    ) -> Mapping[str, Any]: ...


__all__ = ["WorkflowPreviewPort"]
