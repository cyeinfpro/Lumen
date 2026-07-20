"""Messaging Pydantic contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ..constants import MAX_MESSAGE_ATTACHMENTS, MAX_PROMPT_CHARS
from ..message_content import public_message_content
from ..sizing import AspectRatio as AspectRatioLiteral
from .common import BaseOut

# ---------- Messages ----------


class ImageParamsIn(BaseModel):
    # AspectRatio 的 Literal 联合在 sizing.py 里维护；此处 import 复用，
    # 避免两处列表漂移（之前 3:2/2:3/4:3/9:21 未同步就是这里遗漏）
    aspect_ratio: "AspectRatioLiteral" = "7:10"
    size_mode: Literal["auto", "fixed"] = "auto"
    fixed_size: str | None = None
    style_preset_id: str | None = None
    count: int = Field(default=1, ge=1, le=10)
    # UI resolution preset used for billing. fixed_size remains the actual
    # upstream dimensions, whose pixel count can be lower than the nominal tier
    # for wide/tall aspect ratios.
    quality: Literal["1k", "2k", "4k"] | None = "4k"
    # Image Fast uses the lighter responses reasoning model for image_generation:
    # gpt-5.4-mini when enabled, gpt-5.4 when disabled.
    fast: bool | None = None
    # Rendering quality is distinct from the UI's 1K/2K/4K resolution preset.
    render_quality: Literal["auto", "low", "medium", "high"] = "high"
    output_format: Literal["png", "jpeg", "webp"] | None = None
    # Only applies to jpeg/webp. None omits the provider compression option.
    output_compression: int | None = Field(default=None, ge=0, le=100)
    background: Literal["auto", "opaque", "transparent"] = "auto"
    moderation: Literal["auto", "low"] = "low"

    @model_validator(mode="after")
    def normalize_transparent_output(self) -> "ImageParamsIn":
        if self.background == "transparent":
            self.output_format = "png"
            self.output_compression = None
        return self


class ChatParamsIn(BaseModel):
    # system_prompt 与用户文本一同进入上游 instructions，保持同样上限。
    system_prompt: str | None = Field(default=None, max_length=10000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # TODO: 后续 release 内把 le 收敛到 16000；当前先保持 32000，避免老客户端
    # 提交 (16000, 32000] 区间值被 422 拒绝。worker 侧若需要更紧的实际上限，
    # 应在调度层 silent clamp，不要在 schema 这一层做 hard fail。
    max_output_tokens: int = Field(default=2048, ge=1, le=32000)
    stream: bool = True
    # 推理强度（仅 chat / vision_qa 生效；"none"=不思考；"minimal" 兼容旧客户端）
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None
    # Fast 模式：走上游 priority 处理通道，换更低更稳的延迟（付费、不降质）。
    # 对应上游 /v1/responses 的 service_tier="priority"。
    fast: bool | None = None
    # Web search：仅 chat / vision_qa 生效。前端默认打开，模型按需调用。
    web_search: bool = False
    # File search：需要后台或请求侧提供 OpenAI vector_store id。
    file_search: bool = False
    vector_store_ids: list[str] = Field(default_factory=list, max_length=8)
    # Code Interpreter：让 Responses API 使用托管代码执行环境。
    code_interpreter: bool = False
    # Image generation：在 chat 中允许模型按需生成图片，结果会保存到当前助手消息。
    image_generation: bool = False


class AdvancedIn(BaseModel):
    stream_partial_image: bool = False


AttachmentRole = Literal[
    "reference",
    "subject",
    "product",
    "style",
    "edit_target",
    "ask_target",
    "background",
    "mask",
    "other",
]


class MessageAttachmentIn(BaseModel):
    """Structured image attachment metadata for composer/workflow routing."""

    image_id: str = Field(min_length=1, max_length=64)
    role: AttachmentRole = "reference"
    label: str | None = Field(default=None, max_length=40)
    weight: float | None = Field(default=None, ge=0, le=1)


class PostMessageIn(BaseModel):
    """DESIGN §5.4 核心写入接口。"""

    idempotency_key: str = Field(min_length=1, max_length=64)
    # 上游 prompt 上限对齐：单条用户输入限制 10k 字符，避免恶意 / 误粘大文本撑爆 DB / 上游。
    text: str = Field(max_length=MAX_PROMPT_CHARS)
    attachment_image_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_MESSAGE_ATTACHMENTS,
    )
    attachments: list[MessageAttachmentIn] = Field(
        default_factory=list,
        max_length=MAX_MESSAGE_ATTACHMENTS,
    )
    # Future-facing metadata used by UI actions, projects, Telegram deep links,
    # and worker diagnostics. All fields are optional so legacy clients keep
    # working with only text + attachment_image_ids.
    input_images: list[str] = Field(
        default_factory=list, max_length=MAX_MESSAGE_ATTACHMENTS
    )
    source: str | None = Field(default=None, max_length=48)
    action_source: str | None = Field(default=None, max_length=80)
    trace_id: str | None = Field(default=None, max_length=96)
    # 局部 inpaint 用 mask（attachment 级别，不进 image_params）。
    # RGBA PNG，alpha=0 处即要重画区域。复用 POST /images/upload 上传后把返回
    # 的 image_id 填到这里；worker 侧用 PIL 自适应 resize 到第一张参考图尺寸。
    mask_image_id: str | None = None
    # intent 必须由前端显式给出；V1 删掉了 auto 启发式（命中率低，易误判）。
    # 历史客户端若仍带 "auto"，统一按 chat 处理（后端在 intent.resolve_intent 里兜底）。
    intent: Literal["auto", "chat", "vision_qa", "text_to_image", "image_to_image"] = (
        "chat"
    )
    image_params: ImageParamsIn = Field(default_factory=ImageParamsIn)
    chat_params: ChatParamsIn = Field(default_factory=ChatParamsIn)
    advanced: AdvancedIn = Field(default_factory=AdvancedIn)

    @model_validator(mode="after")
    def normalize_attachment_contract(self) -> "PostMessageIn":
        structured_ids = [att.image_id for att in self.attachments]
        input_ids = [value for value in self.input_images if value]
        legacy_ids = list(self.attachment_image_ids or [])

        def same_ids(left: list[str], right: list[str]) -> bool:
            return left == right

        if structured_ids and legacy_ids and not same_ids(structured_ids, legacy_ids):
            raise ValueError(
                "attachments image_id values must match attachment_image_ids"
            )
        if input_ids and legacy_ids and not same_ids(input_ids, legacy_ids):
            raise ValueError("input_images must match attachment_image_ids")
        if input_ids and structured_ids and not same_ids(input_ids, structured_ids):
            raise ValueError("input_images must match attachments image_id values")

        canonical_ids = structured_ids or legacy_ids or input_ids
        if canonical_ids:
            self.attachment_image_ids = list(canonical_ids)
            if not self.input_images or not same_ids(input_ids, canonical_ids):
                self.input_images = list(canonical_ids)
        return self


class MessageOut(BaseOut):
    id: str
    conversation_id: str
    role: str
    content: dict[str, Any]
    intent: str | None = None
    status: str | None = None
    parent_message_id: str | None = None
    created_at: datetime

    @field_validator("content", mode="before")
    @classmethod
    def _strip_internal_content(cls, value: Any) -> dict[str, Any]:
        return public_message_content(value)


class PostMessageOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    completion_id: str | None = None
    generation_ids: list[str] = Field(default_factory=list)


# ---------- Tasks ----------


class GenerationOut(BaseOut):
    id: str
    message_id: str
    user_api_credential_id: str | None = None
    upstream_supplier_id: str | None = None
    parent_generation_id: str | None = None
    action: str
    prompt: str
    size_requested: str
    aspect_ratio: str
    input_image_ids: list[str]
    primary_input_image_id: str | None
    mask_image_id: str | None = None
    status: str
    progress_stage: str
    attempt: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    is_dual_race_bonus: bool = False
    billing_free: bool = False
    billing_label: str | None = None
    billing_exempt_reason: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    revised_prompt: str | None = None
    requested_params: dict[str, Any] | None = None
    effective_params: dict[str, Any] | None = None
    provider_attempts: list[dict[str, Any]] = Field(default_factory=list)
    source: str | None = None
    action_source: str | None = None
    trace_id: str | None = None
    attachment_roles: list[dict[str, Any]] = Field(default_factory=list)
    source_image_id: str | None = None
    queue_lane: str | None = None
    workflow_type: str | None = None
    workflow_step_key: str | None = None
    pixel_count: int | None = None
    size_bucket: str | None = None
    cost_class: str | None = None
    queue_wait_ms: int | None = None


class CompletionOut(BaseOut):
    id: str
    message_id: str
    user_api_credential_id: str | None = None
    upstream_supplier_id: str | None = None
    upstream_request: dict[str, Any] | None = None
    model: str
    input_image_ids: list[str]
    text: str
    tokens_in: int
    tokens_out: int
    status: str
    progress_stage: str
    attempt: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    source: str | None = None
    action_source: str | None = None
    trace_id: str | None = None
    queue_lane: str | None = None
    workflow_type: str | None = None
    workflow_step_key: str | None = None
    pixel_count: int | None = None
    size_bucket: str | None = None
    cost_class: str | None = None
    queue_wait_ms: int | None = None


class TaskRecommendedActionOut(BaseModel):
    id: str
    label: str
    kind: Literal["retry", "link", "adjust", "wait", "details"] = "details"
    href: str | None = None


class TaskItemOut(BaseModel):
    """Global Task Tray 聚合（DESIGN §5.5）。"""

    kind: Literal["generation", "completion"]
    id: str
    message_id: str
    status: str
    progress_stage: str
    stage: str | None = None
    started_at: datetime | None
    date: datetime | None = None
    cursor: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    source: str | None = None
    action_source: str | None = None
    trace_id: str | None = None
    conversation_id: str | None = None
    project_id: str | None = None
    workflow_type: str | None = None
    workflow_step_key: str | None = None
    queue_lane: str | None = None
    pixel_count: int | None = None
    size_bucket: str | None = None
    cost_class: str | None = None
    queue_wait_ms: int | None = None
    queue_position: int | None = None
    substage: str | None = None
    retrying: bool | None = None
    waiting_provider: bool | None = None
    cancelled: bool | None = None
    title: str | None = None
    prompt: str | None = None
    source_image_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    recommended_actions: list[TaskRecommendedActionOut] = Field(default_factory=list)
    thumb_url: str | None = None


class TaskListOut(BaseModel):
    # Preserve the legacy OpenAPI/Pydantic identity used by generated clients.
    __module__ = "lumen_core.schemas"

    items: list[TaskItemOut]
    next_cursor: str | None = None


class ActiveTasksOut(BaseModel):
    """`/tasks/mine/active` 用户级中心列表完整快照。

    返回当前用户所有未完成的 generations / completions 完整字段，前端启动 / SSE 重连时
    一次性 hydrate 到 store，避免任务列表按会话碎片化。"""

    generations: list[GenerationOut]
    completions: list[CompletionOut]


__all__ = [
    "ImageParamsIn",
    "ChatParamsIn",
    "AdvancedIn",
    "AttachmentRole",
    "MessageAttachmentIn",
    "PostMessageIn",
    "MessageOut",
    "PostMessageOut",
    "GenerationOut",
    "CompletionOut",
    "TaskRecommendedActionOut",
    "TaskItemOut",
    "TaskListOut",
    "ActiveTasksOut",
]
