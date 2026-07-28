"""Typed records for apparel workflow business rules."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..domain.json_types import JsonObject


class ApparelWorkflowRunState(Protocol):
    user_prompt: str
    metadata_jsonb: JsonObject | None
    current_step: str
    status: str


class ApparelWorkflowStepState(Protocol):
    status: str
    approved_at: datetime | None
    approved_by: str | None
    input_json: JsonObject | None
    output_json: JsonObject | None


class CandidateImageState(Protocol):
    contact_sheet_image_id: str | None
    model_brief_json: JsonObject | None


__all__ = [
    "ApparelWorkflowRunState",
    "ApparelWorkflowStepState",
    "CandidateImageState",
]
