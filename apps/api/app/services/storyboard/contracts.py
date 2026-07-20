"""Storyboard request/response contracts shared by routes and services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.models import OutboxEvent
from lumen_core.schemas import VideoOut

from .common import (
    STORYBOARD_DEFAULT_ASPECT_RATIO,
    STORYBOARD_DEFAULT_DURATION_S,
    STORYBOARD_DEFAULT_MODEL,
    STORYBOARD_DEFAULT_RESOLUTION,
)


class StoryboardAssetOut(BaseModel):
    id: str
    kind: str
    name: str
    role: str
    description: str
    continuity: str
    revision: int
    status: str
    prompt: str
    image_id: str | None = None
    image_url: str | None = None
    display_url: str | None = None
    generation_id: str | None = None
    approved_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardShotOut(BaseModel):
    id: str
    index: int
    title: str
    purpose: str
    narration: str
    visual: str
    shot_type: str
    camera_move: str
    transition: str
    reference_notes: str
    duration_s: int
    asset_ids: list[str]
    keyframe_prompt: str
    keyframe_source_hash: str | None = None
    current_source_hash: str
    keyframe_stale: bool = False
    status: str
    keyframe_image_id: str | None = None
    keyframe_image_url: str | None = None
    keyframe_display_url: str | None = None
    keyframe_generation_id: str | None = None
    keyframe_approved_at: str | None = None
    video_generation_id: str | None = None
    video: VideoOut | None = None
    video_status: str | None = None
    video_progress_stage: str | None = None
    video_progress_pct: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardAssemblyOut(BaseModel):
    status: str
    video_id: str | None = None
    video_url: str | None = None
    poster_url: str | None = None
    segment_count: int = 0
    segment_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None


class StoryboardRunOut(BaseModel):
    id: str
    conversation_id: str | None = None
    title: str
    idea: str
    style: str
    script: str
    script_confirmed: bool
    script_revision: int
    aspect_ratio: str
    resolution: str
    model: str
    generate_audio: bool
    seed: int | None = None
    status: str
    current_stage: str
    assets: list[StoryboardAssetOut]
    shots: list[StoryboardShotOut]
    assembly: StoryboardAssemblyOut | None = None
    thumbnail_url: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardRunListItemOut(BaseModel):
    id: str
    title: str
    idea: str
    status: str
    current_stage: str
    asset_count: int
    approved_asset_count: int
    shot_count: int
    done_shot_count: int
    thumbnail_url: str | None = None
    created_at: datetime
    updated_at: datetime


class StoryboardRunListOut(BaseModel):
    items: list[StoryboardRunListItemOut]
    next_cursor: str | None = None


class StoryboardCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    idea: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    style: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default=STORYBOARD_DEFAULT_ASPECT_RATIO, max_length=16)
    resolution: str = Field(default=STORYBOARD_DEFAULT_RESOLUTION, max_length=16)
    model: str = Field(default=STORYBOARD_DEFAULT_MODEL, max_length=64)
    generate_audio: bool = True
    seed: int | None = Field(default=None, ge=-1, le=4_294_967_295)


class StoryboardPatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    idea: str | None = Field(default=None, min_length=1, max_length=MAX_PROMPT_CHARS)
    style: str | None = Field(default=None, max_length=2000)
    script: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    script_confirmed: bool | None = None
    aspect_ratio: str | None = Field(default=None, max_length=16)
    resolution: str | None = Field(default=None, max_length=16)
    model: str | None = Field(default=None, max_length=64)
    generate_audio: bool | None = None
    seed: int | None = Field(default=None, ge=-1, le=4_294_967_295)
    current_stage: str | None = Field(default=None, max_length=32)


class StoryboardAssetCreateIn(BaseModel):
    kind: Literal["character", "scene", "prop"] = "character"
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=160)
    description: str = Field(default="", max_length=2000)
    continuity: str = Field(default="", max_length=2000)


class StoryboardAssetPatchIn(BaseModel):
    kind: Literal["character", "scene", "prop"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    role: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    continuity: str | None = Field(default=None, max_length=2000)


class StoryboardGenerateIn(BaseModel):
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)


class StoryboardShotCreateIn(BaseModel):
    title: str = Field(default="", max_length=160)
    purpose: str = Field(default="", max_length=1000)
    narration: str = Field(default="", max_length=2000)
    visual: str = Field(default="", max_length=2000)
    shot_type: str = Field(default="", max_length=80)
    camera_move: str = Field(default="", max_length=80)
    transition: str = Field(default="", max_length=80)
    reference_notes: str = Field(default="", max_length=2000)
    duration_s: int = Field(default=STORYBOARD_DEFAULT_DURATION_S, ge=3, le=15)
    asset_ids: list[str] = Field(default_factory=list, max_length=16)
    keyframe_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)


class StoryboardShotPatchIn(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    purpose: str | None = Field(default=None, max_length=1000)
    narration: str | None = Field(default=None, max_length=2000)
    visual: str | None = Field(default=None, max_length=2000)
    shot_type: str | None = Field(default=None, max_length=80)
    camera_move: str | None = Field(default=None, max_length=80)
    transition: str | None = Field(default=None, max_length=80)
    reference_notes: str | None = Field(default=None, max_length=2000)
    duration_s: int | None = Field(default=None, ge=3, le=15)
    asset_ids: list[str] | None = Field(default=None, max_length=16)
    keyframe_prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)


class StoryboardShotsRebuildIn(BaseModel):
    shots: list[StoryboardShotCreateIn] | None = Field(default=None, max_length=60)
    replace: bool = True


class StoryboardShotMoveIn(BaseModel):
    direction: Literal[-1, 1]


class StoryboardSubmitShotIn(BaseModel):
    prompt: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    duration_s: int | None = Field(default=None, ge=3, le=15)
    idempotency_key: str | None = Field(default=None, max_length=96)


@dataclass
class StoryboardImageTask:
    generation_id: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[OutboxEvent]
