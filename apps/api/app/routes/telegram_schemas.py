"""Transport models for the Telegram bot API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS


class LinkCodeOut(BaseModel):
    code: str
    expires_in: int
    deep_link: str | None = None


class BindIn(BaseModel):
    chat_id: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=4, max_length=32)
    tg_user_id: str | None = Field(default=None, min_length=1, max_length=64)
    tg_username: str | None = Field(default=None, max_length=64)


class BindOut(BaseModel):
    user_id: str
    email: str
    display_name: str


class GenerateIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=10000)
    aspect_ratio: Literal[
        "1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "9:21", "4:5"
    ] = "1:1"
    render_quality: Literal["low", "medium", "high", "auto"] = "high"
    count: int = Field(default=1, ge=1, le=16)
    resolution: Literal["1k", "2k", "4k"] = "2k"
    output_format: Literal["png", "jpeg", "webp"] = "jpeg"
    fast: bool = False
    attachment_image_ids: list[str] = Field(
        default_factory=list, max_length=MAX_MESSAGE_ATTACHMENTS
    )


class GenerateOut(BaseModel):
    user_id: str
    conversation_id: str
    message_id: str
    generation_ids: list[str]


class EnhancePromptIn(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class EnhancePromptOut(BaseModel):
    enhanced: str


class GenerationStatusOut(BaseModel):
    id: str
    conversation_id: str
    status: str
    progress_stage: str
    error_code: str | None = None
    error_message: str | None = None
    image_ids: list[str] = Field(default_factory=list)
    input_image_ids: list[str] = Field(default_factory=list)
    prompt: str
    created_at: datetime
    aspect_ratio: str
    size_requested: str
    render_quality: str = "medium"
    output_format: str = "jpeg"
    fast: bool = False
    web_url: str | None = None
    edit_url: str | None = None
    project_url: str | None = None


class TaskListItem(BaseModel):
    id: str
    status: str
    prompt_excerpt: str
    aspect_ratio: str
    size_requested: str
    image_ids: list[str]
    error_message: str | None = None
    created_at: datetime


class TaskListOut(BaseModel):
    items: list[TaskListItem]


class RuntimeProxyOut(BaseModel):
    name: str
    url: str


class RuntimeConfigOut(BaseModel):
    bot_enabled: bool
    bot_token: str
    bot_username: str
    allowed_user_ids: str
    proxy: RuntimeProxyOut | None
    proxy_strategy: str
    failure_threshold: int
    cooldown_seconds: int


class RuntimeAccessOut(BaseModel):
    bot_enabled: bool
    allowed_user_ids: str


class ProxyReportIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    success: bool = False
