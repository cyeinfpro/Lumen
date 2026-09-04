"""Strict shared request and public response contracts for Agent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

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


AGENT_MAX_REFERENCE_IMAGES = 16
AGENT_MAX_SESSION_IMAGES = 64
AGENT_MAX_IMAGES_PER_TOOL = 4
AGENT_MAX_TEXT_FILES = 8
AGENT_MAX_FILE_BYTES = 256 * 1024
AGENT_MAX_FILE_CHARS = 200_000
AGENT_MAX_TOTAL_FILE_BYTES = 1024 * 1024
AGENT_MAX_TOTAL_FILE_CHARS = 800_000
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


class AgentTextFileIn(_StrictAgentIn):
    name: str = Field(min_length=1, max_length=128)
    mime_type: str = Field(default="text/plain", min_length=1, max_length=96)
    size: int = Field(ge=0, le=AGENT_MAX_FILE_BYTES)
    content: str = Field(max_length=AGENT_MAX_FILE_CHARS)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or any(ord(char) < 32 for char in normalized)
        ):
            raise ValueError("invalid Agent file name")
        return normalized

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized.startswith("text/") and normalized not in {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
            "application/javascript",
            "application/typescript",
            "application/sql",
        }:
            raise ValueError("unsupported Agent file type")
        return normalized

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Agent text files cannot contain NUL bytes")
        return value

    @model_validator(mode="after")
    def normalize_size(self) -> "AgentTextFileIn":
        actual_size = len(self.content.encode("utf-8"))
        if actual_size > AGENT_MAX_FILE_BYTES:
            raise ValueError("Agent text file exceeds the byte limit")
        self.size = actual_size
        return self


class AgentSessionCreateIn(_StrictAgentIn):
    title: str = Field(default="", max_length=255)
    default_system: str | None = Field(default=None, max_length=MAX_PROMPT_CHARS)
    default_system_prompt_id: str | None = Field(default=None, max_length=64)
    image_defaults: AgentImageDefaultsIn = Field(default_factory=AgentImageDefaultsIn)
    allow_image: bool = True
    allow_web_search: bool = False
    allow_file_tools: bool = True


class AgentSessionBranchIn(_StrictAgentIn):
    title: str | None = Field(default=None, min_length=1, max_length=255)


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
    allow_web_search: bool | None = None
    allow_file_tools: bool | None = None


class AgentMessageCreateIn(_StrictAgentIn):
    idempotency_key: str = Field(min_length=1, max_length=96)
    text: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    attachments: list[AgentReferenceIn] = Field(
        default_factory=list,
        max_length=AGENT_MAX_REFERENCE_IMAGES,
    )
    files: list[AgentTextFileIn] = Field(
        default_factory=list,
        max_length=AGENT_MAX_TEXT_FILES,
    )
    image_defaults: AgentImageDefaultsIn = Field(default_factory=AgentImageDefaultsIn)
    allow_image: bool = True
    allow_web_search: bool = False
    allow_file_tools: bool = True
    model: str | None = Field(default=None, min_length=1, max_length=128)
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = None

    @model_validator(mode="after")
    def validate_message(self) -> "AgentMessageCreateIn":
        if not self.text.strip() and not self.attachments and not self.files:
            raise ValueError("text, image attachment, or text file is required")
        image_ids = [attachment.image_id for attachment in self.attachments]
        if len(set(image_ids)) != len(image_ids):
            raise ValueError("attachment image_id values must be unique")
        file_names = [item.name.casefold() for item in self.files]
        if len(set(file_names)) != len(file_names):
            raise ValueError("Agent file names must be unique")
        if sum(item.size for item in self.files) > AGENT_MAX_TOTAL_FILE_BYTES:
            raise ValueError("Agent files exceed the total byte limit")
        if sum(len(item.content) for item in self.files) > AGENT_MAX_TOTAL_FILE_CHARS:
            raise ValueError("Agent files exceed the total text limit")
        if self.files and not self.allow_file_tools:
            raise ValueError("Agent files require file tools")
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
        valid_labels = {
            stable_reference_label(index) for index in range(AGENT_MAX_SESSION_IMAGES)
        }
        for label in self.reference_labels:
            if label not in valid_labels:
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


class AgentProviderDispatchIn(_StrictAgentIn):
    dispatch_ordinal: int = Field(ge=1, le=128)
    execution_epoch: int = Field(ge=0)


class AgentProviderDispatchOut(BaseModel):
    permit_id: str = Field(min_length=1, max_length=192)
    dispatch_ordinal: int = Field(ge=1, le=128)


class AgentReferenceOut(BaseModel):
    id: str
    image_id: str
    ordinal: int
    reference_label: str
    role: str
    display_label: str | None = None


class _AgentToolDetailsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentWebSearchToolDetailsOut(_AgentToolDetailsOut):
    kind: Literal["web_search"]
    query: str | None = Field(default=None, max_length=2_000)
    result_snippets: list[str] = Field(default_factory=list, max_length=6)


class AgentFileToolDetailsOut(_AgentToolDetailsOut):
    kind: Literal["file_list", "file_read", "file_search"]
    file_names: list[str] = Field(default_factory=list, max_length=8)
    query: str | None = Field(default=None, max_length=256)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    result_snippets: list[str] = Field(default_factory=list, max_length=6)


class AgentImageToolDetailsOut(_AgentToolDetailsOut):
    kind: Literal["image"]
    prompt: str | None = Field(default=None, max_length=4_000)
    reference_count: int = Field(default=0, ge=0, le=AGENT_MAX_REFERENCE_IMAGES)
    count: int | None = Field(default=None, ge=1, le=AGENT_MAX_IMAGES_PER_TOOL)
    aspect_ratio: AspectRatio | None = None
    quality: Literal["1k", "2k", "4k"] | None = None
    render_quality: Literal["auto", "low", "medium", "high"] | None = None
    background: Literal["auto", "opaque", "transparent"] | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None


AgentToolDetailsOut = Annotated[
    AgentWebSearchToolDetailsOut | AgentFileToolDetailsOut | AgentImageToolDetailsOut,
    Field(discriminator="kind"),
]


class AgentToolCallOut(BaseModel):
    id: str
    agent_run_id: str
    ordinal: int
    name: str
    mode: (
        Literal[
            "text_to_image",
            "image_to_image",
            "web_search",
            "file_list",
            "file_read",
            "file_search",
        ]
        | None
    ) = None
    status: Literal[
        "queued", "running", "succeeded", "failed", "cancelled", "timed_out"
    ]
    generation_ids: list[str] = Field(default_factory=list)
    generation_count: int = 0
    details: AgentToolDetailsOut | None = None
    duration_ms: int | None = Field(default=None, ge=0)
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
    status: Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]
    execution_epoch: int
    last_event_seq: int
    output_revision: int = 0
    output_runtime_seq: int = 0
    idempotency_key: str
    model: str | None = None
    reasoning_effort: str | None = None
    memory_state: Literal["disabled", "empty", "ready", "degraded"] | None = None
    continuable: bool = False
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
    allow_web_search: bool = False
    allow_file_tools: bool = True
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
    generation_ids: list[str] = Field(
        min_length=1, max_length=AGENT_MAX_IMAGES_PER_TOOL
    )
    mode: Literal["text_to_image", "image_to_image"]
    accepted: AgentCreateImageNormalized
    replayed: bool = False
    pi_tool_call_id: str = Field(min_length=1, max_length=128)
    ordinal: int = Field(ge=0)
    request_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AgentRunContinueIn(_StrictAgentIn):
    idempotency_key: str = Field(min_length=1, max_length=96)


class AgentSessionImageOut(BaseModel):
    image_id: str
    reference_label: str
    role: str
    display_label: str | None = None
    source: str
    active: bool


class AgentSessionImageListOut(BaseModel):
    items: list[AgentSessionImageOut]
    used: int
    maximum: int


class AgentEventTextBlock(_StrictAgentIn):
    kind: Literal["text"]
    turn: int = Field(ge=1, le=128)
    text: str = Field(min_length=1, max_length=20_000)


class AgentEventToolBlock(_StrictAgentIn):
    kind: Literal["tool"]
    turn: int = Field(ge=1, le=128)
    tool_call_id: str | None = Field(default=None, max_length=128)
    ordinal: int | None = Field(default=None, ge=0)
    name: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    generation_ids: list[str] = Field(default_factory=list, max_length=4)
    result_text: str | None = Field(default=None, max_length=20_000)


class AgentEventEnvelope(_StrictAgentIn):
    agent_session_id: str = Field(min_length=1, max_length=64)
    agent_run_id: str = Field(min_length=1, max_length=64)
    assistant_message_id: str = Field(min_length=1, max_length=64)
    execution_epoch: int = Field(ge=0)
    event_seq: int = Field(ge=1)
    event_name: str
    event_id: str | None = Field(default=None, max_length=192)
    text_delta: str | None = Field(default=None, max_length=20_000)
    text_operation: Literal["append", "replace"] | None = None
    replacement_text: str | None = Field(default=None, max_length=20_000)
    snapshot_required: bool = False
    blocks: list[AgentEventTextBlock | AgentEventToolBlock] = Field(
        default_factory=list,
        max_length=32,
    )
    output_revision: int | None = Field(default=None, ge=0)
    output_runtime_seq: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=32)
    error_code: str | None = Field(default=None, max_length=64)
    tool_call_id: str | None = Field(default=None, max_length=64)
    generation_ids: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("event_name")
    @classmethod
    def validate_event_name(cls, value: str) -> str:
        if value not in AGENT_EVENT_NAMES:
            raise ValueError("unsupported Agent event")
        return value

    @model_validator(mode="after")
    def validate_event_payload(self) -> "AgentEventEnvelope":
        if self.event_name == "agent.output.delta":
            if self.text_delta is None or self.text_operation is None:
                raise ValueError("Agent output delta is incomplete")
        elif self.event_name == "agent.output.reset":
            if self.snapshot_required:
                if self.replacement_text is not None or self.blocks:
                    raise ValueError("snapshot marker must not carry replacement data")
            elif self.text_operation != "replace" or self.replacement_text is None:
                raise ValueError("Agent output reset is incomplete")
        elif (
            self.text_delta is not None
            or self.text_operation is not None
            or self.replacement_text is not None
            or self.snapshot_required
            or self.blocks
        ):
            raise ValueError("non-output Agent event contains output fields")
        return self


class AgentModelOptionOut(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    vision_supported: bool = False
    reasoning_supported: bool = False


class AgentStatusOut(BaseModel):
    enabled: bool = True
    tool_gateway_configured: bool = False
    default_model: str | None = Field(default=None, max_length=128)
    models: list[AgentModelOptionOut] = Field(default_factory=list, max_length=128)


def stable_reference_label(index: int) -> str:
    if index < 0 or index >= AGENT_MAX_SESSION_IMAGES:
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
    "AGENT_MAX_FILE_BYTES",
    "AGENT_MAX_FILE_CHARS",
    "AGENT_MAX_IMAGES_PER_TOOL",
    "AGENT_MAX_REFERENCE_IMAGES",
    "AGENT_MAX_SESSION_IMAGES",
    "AGENT_MAX_TEXT_FILES",
    "AGENT_MAX_TOTAL_FILE_BYTES",
    "AGENT_MAX_TOTAL_FILE_CHARS",
    "AgentCreateImageArgumentsIn",
    "AgentCreateImageNormalized",
    "AgentEventEnvelope",
    "AgentEventTextBlock",
    "AgentEventToolBlock",
    "AgentImageDefaultsIn",
    "AgentMessageCreateIn",
    "AgentMessageCreateOut",
    "AgentMessageListOut",
    "AgentModelOptionOut",
    "AgentReferenceIn",
    "AgentReferenceOut",
    "AgentProviderDispatchIn",
    "AgentProviderDispatchOut",
    "AgentRunOut",
    "AgentRunContinueIn",
    "AgentSessionBranchIn",
    "AgentSessionCreateIn",
    "AgentSessionListOut",
    "AgentSessionOut",
    "AgentSessionPatchIn",
    "AgentSessionImageListOut",
    "AgentSessionImageOut",
    "AgentStatusOut",
    "AgentToolCallOut",
    "AgentToolCreateImageIn",
    "AgentTextFileIn",
    "AgentToolCreateImageOut",
    "agent_message_request_fingerprint",
    "agent_tool_semantic_key",
    "canonical_agent_hash",
    "normalize_create_image_arguments",
    "stable_reference_label",
]
