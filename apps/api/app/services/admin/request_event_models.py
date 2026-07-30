"""Response models for the admin request-events service."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from lumen_core.utils import ensure_utc


ImageRole = Literal["input", "output"]


class _RequestEventImageOut(BaseModel):
    id: str
    roles: list[ImageRole]
    source: str
    url: str
    display_url: str
    preview_url: str | None
    thumb_url: str | None
    width: int
    height: int
    mime: str
    parent_image_id: str | None = None
    owner_generation_id: str | None = None


class _RequestEventLiveLane(BaseModel):
    """One provider lane from an in-flight request snapshot."""

    label: str
    provider: str | None = None
    route: str | None = None
    endpoint: str | None = None
    status: str | None = None
    last_failed: str | None = None


class _RequestEventOut(BaseModel):
    id: str
    kind: Literal["generation", "completion"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    status: str
    progress_stage: str
    attempt: int
    model: str
    user_id: str
    user_email: str
    conversation_id: str | None
    conversation_title: str | None
    message_id: str
    prompt: str | None = None
    action: str | None = None
    intent: str | None = None
    upstream_provider: str | None = None
    upstream_route: str | None = None
    upstream_endpoint: str | None = None
    queue_lane: str | None = None
    workflow_type: str | None = None
    workflow_step_key: str | None = None
    pixel_count: int | None = None
    size_bucket: str | None = None
    cost_class: str | None = None
    queue_wait_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    images: list[_RequestEventImageOut] = Field(default_factory=list)
    upstream: dict[str, Any] = Field(default_factory=dict)
    live_provider: str | None = None
    live_lanes: list[_RequestEventLiveLane] = Field(default_factory=list)


class _RequestEventModelStatOut(BaseModel):
    model: str
    count: int
    share: float


class _RequestEventsOut(BaseModel):
    items: list[_RequestEventOut] = Field(default_factory=list)
    total: int
    model_stats: list[_RequestEventModelStatOut] = Field(default_factory=list)


RequestEventImageOut = _RequestEventImageOut
RequestEventLiveLane = _RequestEventLiveLane
RequestEventOut = _RequestEventOut
RequestEventModelStatOut = _RequestEventModelStatOut
RequestEventsOut = _RequestEventsOut


def duration_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    now: datetime,
) -> int | None:
    if started_at is None:
        return None
    return max(
        0,
        int(
            (ensure_utc(finished_at or now) - ensure_utc(started_at)).total_seconds()
            * 1000
        ),
    )
