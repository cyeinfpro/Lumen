"""Strict shared request and public response contracts for Agent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..agent_events import AGENT_EVENT_NAMES, AGENT_TOOL_CREATE_IMAGE
from ..constants import MAX_PROMPT_CHARS
from ..sizing import AspectRatio, quality_to_fixed_size
from .messaging import (
    AttachmentRole,
    CompletionOut,
    GenerationOut,
    ImageParamsIn,
    MessageOut,
)
from .video import ImageOut


AGENT_MAX_REFERENCE_IMAGES = 4
AGENT_MAX_IMAGES_PER_TOOL = 4
AGENT_REFERENCE_LABEL_PREFIX = "ref_"


class _StrictAgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AgentImageDefaultsIn(_StrictAgentIn):
    count: int = Field(default=1, ge=1, le=AGENT_MAX_IMAGES_PER_TOOL)
    aspect_ratio: AspectRatio = "1:1"
    quality: Literal["1k", "2k", "4k"] = "2k"
    render_quality: Literal["auto", "low", "medium", "high"] = "high"
    background: Literal["auto", "opaque", "transparent"] = "auto"
    output_format: Literal["png", "jpeg", "webp"] = "webp"


class AgentReferenceIn(_StrictAgentIn):
    image_id: str = Field(min_length=1, max_length=64)
    role: AttachmentRole = "reference"
    label: str | None = Field(default=None, max_length=40)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class AgentSessionCreateIn(_StrictAgentIn):
    title: str = Field(default="", max_length=255)
    default_system: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    default_system_prompt_id: str | None = Field(default=None, max_length=64)
    image_defaults: AgentImageDefaultsIn = Field(default_factory=AgentImageDefaultsIn)
    allow_image: bool = True


class AgentSessionPatchIn(_StrictAgentIn):
    title: str | None = Field(default=None, max_length=255)
    pinned: bool | None = None
    archived: bool | None = None
    memory_disabled: bool | None = None
    active_scope_id: str | None = Field(default=None, max_length=64)
    default_system: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    default_system_prompt_id: str | None = Field(default=None, max_length=64)
    image_defaults: AgentImageDefaultsIn | None = None
    allow_image: bool | None = None


class AgentMessageCreateIn(_StrictAgentIn):
    idempotency_key: str = Field(min_length=1, max_length=96)
    text: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    attachments: list[AgentReferenceIn] = Field(
        default_factory=list,
        max_length=AGENT_MAX_REFERENCE_IMAGES,
    )
    image_defaults: AgentImageDefaultsIn = Field(default_factory=AgentImageDefaultsIn)
    allow_image: bool = True
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None

    @model_validator(mode="after")
    def validate_message(self) -> "AgentMessageCreateIn":
        if not self.text.strip() and not self.attachments:
            raise ValueError("text or at least one attachment is required")
        image_ids = [attachment.image_id for attachment in self.attachments]
        if len(set(image_ids)) != len(image_ids):
            raise ValueError("attachment image_id values must be unique")
        return self


class AgentCreateImageArgumentsIn(_StrictAgentIn):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    reference_labels: list[str] = Field(
        default_factory=list,
        max_length=AGENT_MAX_REFERENCE_IMAGES,
    )
    count: int | None = Field(default=None, ge=1, le=AGENT_MAX_IMAGES_PER_TOOL)
    aspect_ratio: AspectRatio | None = None
    quality: Literal["1k", "2k", "4k"] | None = None
    render_quality: Literal["auto", "low", "medium", "high"] | None = None
    background: Literal["auto", "opaque", "transparent"] | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("prompt must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_references(self) -> "AgentCreateImageArgumentsIn":
        if len(set(self.reference_labels)) != len(self.reference_labels):
            raise ValueError("reference_labels must be unique")
        for label in self.reference_labels:
            if not label.startswith(AGENT_REFERENCE_LABEL_PREFIX):
                raise ValueError("invalid reference label")
        return self


class AgentCreateImageNormalized(_StrictAgentIn):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    reference_labels: list[str] = Field(max_length=AGENT_MAX_REFERENCE_IMAGES)
    count: int = Field(ge=1, le=AGENT_MAX_IMAGES_PER_TOOL)
    aspect_ratio: AspectRatio
    quality: Literal["1k", "2k", "4k"]
    render_quality: Literal["auto", "low", "medium", "high"]
    background: Literal["auto", "opaque", "transparent"]
    output_format: Literal["png", "jpeg", "webp"]

    def image_params(self) -> ImageParamsIn:
        fixed_size = quality_to_fixed_size(self.quality, self.aspect_ratio)
        return ImageParamsIn(
            count=self.count,
            aspect_ratio=self.aspect_ratio,
            quality=self.quality,
            size_mode="fixed",
            fixed_size=fixed_size,
            render_quality=self.render_quality,
            background=self.background,
            output_format=self.output_format,
        )


class AgentToolCreateImageIn(_StrictAgentIn):
    pi_tool_call_id: str = Field(min_length=1, max_length=128)
    ordinal: int = Field(ge=0)
    execution_epoch: int = Field(ge=0)
    arguments: AgentCreateImageArgumentsIn


class AgentReferenceOut(BaseModel):
    id: str
    image_id: str
    ordinal: int
    reference_label: str
    role: str
    display_label: str | None = None


class AgentToolCallOut(BaseModel):
    id: str
    agent_run_id: str
    ordinal: int
    name: str
    mode: Literal["text_to_image", "image_to_image"] | None = None
    status: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "timed_out"
    ]
    generation_ids: list[str] = Field(default_factory=list)
    generation_count: int = 0
    error_code: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentRunOut(BaseModel):
    id: str
    agent_session_id: str
    user_message_id: str
    assistant_message_id: str
    status: Literal[
        "queued", "running", "succeeded", "partial", "failed", "cancelled"
    ]
    execution_epoch: int
    last_event_seq: int
    idempotency_key: str
    model: str | None = None
    reasoning_effort: str | None = None
    turn_count: int
    tool_call_count: int
    usage: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    references: list[AgentReferenceOut] = Field(default_factory=list)
    tool_calls: list[AgentToolCallOut] = Field(default_factory=list)


class AgentSessionOut(BaseModel):
    id: str
    conversation_id: str
    title: str
    pinned: bool
    archived: bool
    memory_disabled: bool
    active_scope_id: str | None = None
    default_system: str | None = None
    default_system_prompt_id: str | None = None
    image_defaults: AgentImageDefaultsIn = Field(default_factory=AgentImageDefaultsIn)
    allow_image: bool = True
    runtime_version: str
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime
    active_run: AgentRunOut | None = None


class AgentSessionListOut(BaseModel):
    items: list[AgentSessionOut]
    next_cursor: str | None = None


class AgentMessageListOut(BaseModel):
    items: list[MessageOut]
    runs: list[AgentRunOut] = Field(default_factory=list)
    next_cursor: str | None = None
    generations: list[GenerationOut] = Field(default_factory=list)
    completions: list[CompletionOut] = Field(default_factory=list)
    images: list[ImageOut] = Field(default_factory=list)


class AgentMessageCreateOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    agent_run: AgentRunOut


class AgentToolCreateImageOut(BaseModel):
    tool_call: AgentToolCallOut
    generation_ids: list[str] = Field(default_factory=list)
    mode: Literal["text_to_image", "image_to_image"]
    accepted: AgentCreateImageNormalized
    replayed: bool = False


class AgentEventEnvelope(_StrictAgentIn):
    agent_session_id: str = Field(min_length=1, max_length=64)
    agent_run_id: str = Field(min_length=1, max_length=64)
    assistant_message_id: str = Field(min_length=1, max_length=64)
    execution_epoch: int = Field(ge=0)
    event_seq: int = Field(ge=1)
    event_name: str
    tool_call_id: str | None = Field(default=None, max_length=64)
    generation_ids: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("event_name")
    @classmethod
    def validate_event_name(cls, value: str) -> str:
        if value not in AGENT_EVENT_NAMES:
            raise ValueError("unsupported Agent event")
        return value


class AgentStatusOut(BaseModel):
    enabled: bool = True
    tool_gateway_configured: bool = False


def stable_reference_label(index: int) -> str:
    if index < 0 or index >= AGENT_MAX_REFERENCE_IMAGES:
        raise ValueError("reference index out of range")
    return f"{AGENT_REFERENCE_LABEL_PREFIX}{index + 1}"


def normalize_create_image_arguments(
    arguments: AgentCreateImageArgumentsIn,
    defaults: AgentImageDefaultsIn,
) -> AgentCreateImageNormalized:
    return AgentCreateImageNormalized(
        prompt=arguments.prompt,
        reference_labels=list(arguments.reference_labels),
        count=arguments.count if arguments.count is not None else defaults.count,
        aspect_ratio=arguments.aspect_ratio or defaults.aspect_ratio,
        quality=arguments.quality or defaults.quality,
        render_quality=arguments.render_quality or defaults.render_quality,
        background=arguments.background or defaults.background,
        output_format=arguments.output_format or defaults.output_format,
    )


def canonical_agent_hash(value: BaseModel | dict[str, Any]) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def agent_message_request_fingerprint(body: AgentMessageCreateIn) -> str:
    return canonical_agent_hash(body)


def agent_tool_semantic_key(
    run_id: str,
    ordinal: int,
    normalized: AgentCreateImageNormalized,
) -> tuple[str, str]:
    request_hash = canonical_agent_hash(normalized)
    semantic_key = hashlib.sha256(
        f"{run_id}\n{AGENT_TOOL_CREATE_IMAGE}\n{ordinal}\n{request_hash}".encode(
            "utf-8"
        )
    ).hexdigest()
    return request_hash, semantic_key


__all__ = [
    "AGENT_MAX_IMAGES_PER_TOOL",
    "AGENT_MAX_REFERENCE_IMAGES",
    "AgentCreateImageArgumentsIn",
    "AgentCreateImageNormalized",
    "AgentEventEnvelope",
    "AgentImageDefaultsIn",
    "AgentMessageCreateIn",
    "AgentMessageCreateOut",
    "AgentMessageListOut",
    "AgentReferenceIn",
    "AgentReferenceOut",
    "AgentRunOut",
    "AgentSessionCreateIn",
    "AgentSessionListOut",
    "AgentSessionOut",
    "AgentSessionPatchIn",
    "AgentStatusOut",
    "AgentToolCallOut",
    "AgentToolCreateImageIn",
    "AgentToolCreateImageOut",
    "agent_message_request_fingerprint",
    "agent_tool_semantic_key",
    "canonical_agent_hash",
    "normalize_create_image_arguments",
    "stable_reference_label",
]
