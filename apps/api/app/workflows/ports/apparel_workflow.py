"""Typed records for apparel workflow business rules."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class ApparelWorkflowRunState(Protocol):
    user_prompt: str
    metadata_jsonb: dict[str, Any] | None
    current_step: str
    status: str


class ApparelWorkflowStepState(Protocol):
    status: str
    approved_at: datetime | None
    approved_by: str | None
    input_json: dict[str, Any] | None
    output_json: dict[str, Any] | None


class CandidateImageState(Protocol):
    contact_sheet_image_id: str | None
    model_brief_json: dict[str, Any] | None


__all__ = [
    "ApparelWorkflowRunState",
    "ApparelWorkflowStepState",
    "CandidateImageState",
]
