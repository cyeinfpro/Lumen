"""Typed mutable records used by project candidate application rules."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..domain.json_types import JsonObject


class CandidateState(Protocol):
    id: str
    status: str
    contact_sheet_image_id: str | None
    selected_at: datetime | None
    model_brief_json: JsonObject | None


class WorkflowStepState(Protocol):
    status: str
    approved_at: datetime | None
    approved_by: str | None
    input_json: JsonObject | None
    output_json: JsonObject | None
    task_ids: list[str] | None
    image_ids: list[str] | None


class WorkflowRunState(Protocol):
    current_step: str
    status: str


__all__ = ["CandidateState", "WorkflowRunState", "WorkflowStepState"]
