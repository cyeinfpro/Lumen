"""Read-side contracts for workflow project listings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class WorkflowRunCursor:
    updated_at: datetime
    run_id: str


@dataclass(frozen=True)
class WorkflowRunListRecord:
    id: str
    conversation_id: str | None
    type: str
    status: str
    title: str
    user_prompt: str
    product_image_ids: tuple[str, ...]
    current_step: str
    quality_mode: str
    metadata_jsonb: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    output_count: int = 0


@dataclass(frozen=True)
class WorkflowRunReadPage:
    items: tuple[WorkflowRunListRecord, ...]
    has_more: bool


class WorkflowRunReadPort(Protocol):
    async def list_runs(
        self,
        *,
        user_id: str,
        workflow_type: str | None,
        excluded_types: tuple[str, ...],
        after: WorkflowRunCursor | None,
        limit: int,
    ) -> WorkflowRunReadPage: ...


__all__ = [
    "WorkflowRunCursor",
    "WorkflowRunListRecord",
    "WorkflowRunReadPage",
    "WorkflowRunReadPort",
]
