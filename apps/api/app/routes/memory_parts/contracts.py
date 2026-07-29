"""Request and response contracts for account memory routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


MemoryType = Literal["profile", "preference", "avoid", "project"]


class MemoryScopeOut(BaseModel):
    id: str
    name: str
    emoji: str | None = None
    is_default: bool
    count: int = 0
    created_at: datetime


class MemoryOut(BaseModel):
    id: str
    type: MemoryType
    content: str
    source_message_id: str | None = None
    source_excerpt: str | None = None
    source: Literal["explicit", "auto", "manual"]
    confidence: float
    pinned: bool
    disabled: bool
    positive_signal: int
    negative_signal: int
    superseded_by: str | None = None
    last_used_at: datetime | None = None
    scope_id: str
    last_confirmed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MemoryListOut(BaseModel):
    items: list[MemoryOut]


class MemoryCreateIn(BaseModel):
    type: MemoryType
    content: str = Field(min_length=1, max_length=200)
    source_excerpt: str | None = Field(default=None, max_length=160)
    pinned: bool = False
    scope_id: str | None = None


class MemoryPatchIn(BaseModel):
    type: MemoryType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None
    disabled: bool | None = None
    scope_id: str | None = None


class MemorySettingsOut(BaseModel):
    paused: bool
    disabled: bool
    extraction_threshold: float
    onboarding_seen: int
    confirmation_enabled: bool
    embedding_available: bool


class MemorySettingsPatchIn(BaseModel):
    paused: bool | None = None
    disabled: bool | None = None
    confirmation_enabled: bool | None = None


class OnboardingSeenPatchIn(BaseModel):
    flag: int = Field(ge=0, le=30)


class MemoryStagingOut(BaseModel):
    id: str
    type: MemoryType
    content: str
    source_message_id: str | None = None
    source_excerpt: str | None = None
    confidence: float
    scope_id: str
    recommended_scope_id: str | None = None
    decision: Literal["pending", "accepted", "rejected"]
    expires_at: datetime
    created_at: datetime


class MemoryStagingListOut(BaseModel):
    items: list[MemoryStagingOut]


class MemoryStagingPatchIn(BaseModel):
    type: MemoryType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=200)
    scope_id: str | None = None


class MemoryUndoIn(BaseModel):
    undo_token: str


class MemoryAuditOut(BaseModel):
    id: str
    event_type: str
    memory_id: str | None = None
    staging_id: str | None = None
    old_content: str | None = None
    new_content: str | None = None
    source_message_id: str | None = None
    details: dict[str, Any]
    created_at: datetime


class MemoryTimelineOut(BaseModel):
    items: list[MemoryAuditOut]
    next_cursor: str | None = None


class MemoryScopeCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)


class MemoryScopePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    emoji: str | None = Field(default=None, max_length=8)


class MemoryConfirmIn(BaseModel):
    decision: Literal["yes", "no", "skip"]
    conversation_id: str | None = None


class ConversationMemoryDisabledIn(BaseModel):
    disabled: bool


class ConversationActiveScopeIn(BaseModel):
    scope_id: str | None = None


class UsedMemoriesOut(BaseModel):
    used_memory_ids: list[str] = []
    used_memory_summary: list[dict[str, str]] = []
