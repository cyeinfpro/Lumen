"""Pydantic I/O schemas——DESIGN §5 的请求/响应体。

约定：
- 对 API 与 Worker 都可见（Worker 通过 XADD payload 读取任务元信息）
- 字段保守：除非 DESIGN §5 里写了，否则不增加
- 前端 TypeScript 类型由 OpenAPI 生成（后续接 openapi-typescript）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from .constants import (
    MAX_PROMPT_CHARS,
)
from .sizing import AspectRatio as AspectRatioLiteral


class BaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth / User ----------

class SignupIn(BaseModel):
    # EmailStr 依赖 pydantic[email]（apps/api 已声明）；触发 422 而非 500，比手写正则更严格。
    email: EmailStr
    password: str
    display_name: str = ""
    invite_token: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseOut):
    id: str
    email: str
    display_name: str
    role: str
    notification_email: bool
    default_system_prompt_id: str | None = None


# ---------- System Prompts ----------

class SystemPromptOut(BaseOut):
    id: str
    name: str
    content: str
    is_default: bool = False
    created_at: datetime
    updated_at: datetime


class SystemPromptCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=10000)
    make_default: bool = False


class SystemPromptPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    content: str | None = Field(default=None, min_length=1, max_length=10000)
    make_default: bool | None = None


class SystemPromptListOut(BaseModel):
    items: list[SystemPromptOut]
    default_id: str | None = None


# ---------- Conversations ----------

class ConversationOut(BaseOut):
    id: str
    title: str
    pinned: bool
    archived: bool
    last_activity_at: datetime
    default_params: dict[str, Any]
    default_system: str | None = None
    default_system_prompt_id: str | None = None
    created_at: datetime


class ConversationPatchIn(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    default_params: dict[str, Any] | None = None
    default_system: str | None = None
    default_system_prompt_id: str | None = None


# ---------- Messages ----------

class ImageParamsIn(BaseModel):
    # AspectRatio 的 Literal 联合在 sizing.py 里维护；此处 import 复用，
    # 避免两处列表漂移（之前 3:2/2:3/4:3/9:21 未同步就是这里遗漏）
    aspect_ratio: "AspectRatioLiteral" = "1:1"
    size_mode: Literal["auto", "fixed"] = "auto"
    fixed_size: str | None = None
    style_preset_id: str | None = None
    count: int = Field(default=1, ge=1, le=16)
    fast: bool = False
    # Rendering quality is distinct from the UI's 1K/2K/4K resolution preset.
    render_quality: Literal["auto", "low", "medium", "high"] = "medium"
    output_format: Literal["png", "jpeg", "webp"] | None = None
    # Only applies to jpeg/webp. None lets the API layer use the no-compression
    # default.
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
    max_output_tokens: int = Field(default=2048, ge=1, le=32000)
    stream: bool = True
    # 推理强度（仅 chat / vision_qa 生效；"none"=不思考；"minimal" 兼容旧客户端）
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None
    ) = None
    # Fast 模式：走上游 priority 处理通道，换更低更稳的延迟（付费、不降质）。
    # 对应上游 /v1/responses 的 service_tier="priority"。
    fast: bool = False
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


class PostMessageIn(BaseModel):
    """DESIGN §5.4 核心写入接口。"""
    idempotency_key: str = Field(min_length=1, max_length=64)
    # 上游 prompt 上限对齐：单条用户输入限制 10k 字符，避免恶意 / 误粘大文本撑爆 DB / 上游。
    text: str = Field(max_length=MAX_PROMPT_CHARS)
    attachment_image_ids: list[str] = Field(default_factory=list)
    # intent 必须由前端显式给出；V1 删掉了 auto 启发式（命中率低，易误判）。
    # 历史客户端若仍带 "auto"，统一按 chat 处理（后端在 intent.resolve_intent 里兜底）。
    intent: Literal["auto", "chat", "vision_qa", "text_to_image", "image_to_image"] = "chat"
    image_params: ImageParamsIn = Field(default_factory=ImageParamsIn)
    chat_params: ChatParamsIn = Field(default_factory=ChatParamsIn)
    advanced: AdvancedIn = Field(default_factory=AdvancedIn)


class MessageOut(BaseOut):
    id: str
    conversation_id: str
    role: str
    content: dict[str, Any]
    intent: str | None = None
    status: str | None = None
    parent_message_id: str | None = None
    created_at: datetime


class PostMessageOut(BaseModel):
    user_message: MessageOut
    assistant_message: MessageOut
    completion_id: str | None = None
    generation_ids: list[str] = Field(default_factory=list)


# ---------- Tasks ----------

class GenerationOut(BaseOut):
    id: str
    message_id: str
    action: str
    prompt: str
    size_requested: str
    aspect_ratio: str
    input_image_ids: list[str]
    primary_input_image_id: str | None
    status: str
    progress_stage: str
    attempt: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class CompletionOut(BaseOut):
    id: str
    message_id: str
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


class TaskItemOut(BaseModel):
    """Global Task Tray 聚合（DESIGN §5.5）。"""
    kind: Literal["generation", "completion"]
    id: str
    message_id: str
    status: str
    progress_stage: str
    started_at: datetime | None


class ActiveTasksOut(BaseModel):
    """`/tasks/mine/active` 用户级中心列表完整快照。

    返回当前用户所有未完成的 generations / completions 完整字段，前端启动 / SSE 重连时
    一次性 hydrate 到 store，避免任务列表按会话碎片化。"""
    generations: list[GenerationOut]
    completions: list[CompletionOut]


# ---------- Images ----------

class ImageOut(BaseOut):
    id: str
    source: str
    parent_image_id: str | None
    owner_generation_id: str | None = None  # generated 图反查所属 Generation；uploaded 图为 None
    width: int
    height: int
    mime: str
    blurhash: str | None
    url: str  # API 组装的短期签名 URL 或反代路径
    display_url: str | None = None
    preview_url: str | None = None
    thumb_url: str | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)


# ---------- Worker payload (XADD into Redis Stream) ----------

class TaskQueueItem(BaseModel):
    """Worker 从 queue:generations / queue:completions 读取的最小 payload。"""
    task_id: str
    kind: Literal["generation", "completion"]
    user_id: str


# ---------- SSE envelopes ----------

class SSEEvent(BaseModel):
    event: str  # 事件名，见 constants.EV_*
    data: dict[str, Any]
    id: str | None = None  # Last-Event-ID


# ---------- Errors ----------

class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None
    retry_after_ms: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------- Admin / Usage / Share (V1.0 收尾) ----------

class AllowedEmailOut(BaseOut):
    id: str
    email: str
    invited_by_email: str | None
    created_at: datetime


class AdminUserOut(BaseOut):
    id: str
    email: str
    role: str
    display_name: str | None
    created_at: datetime
    generations_count: int
    completions_count: int
    messages_count: int


class UsageOut(BaseModel):
    range_start: datetime
    range_end: datetime
    messages_count: int
    generations_count: int
    generations_succeeded: int
    generations_failed: int
    completions_count: int
    completions_succeeded: int
    completions_failed: int
    total_pixels_generated: int
    total_tokens_in: int
    total_tokens_out: int
    storage_bytes: int


class ShareOut(BaseOut):
    id: str
    image_id: str
    image_ids: list[str] = Field(default_factory=list)
    token: str
    url: str
    image_url: str
    show_prompt: bool
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class PublicShareImageOut(BaseModel):
    id: str
    image_url: str
    display_url: str | None = None
    preview_url: str | None = None
    thumb_url: str | None = None
    width: int
    height: int
    mime: str
    prompt: str | None = None


class PublicShareOut(BaseModel):
    token: str
    image_url: str
    images: list[PublicShareImageOut] = Field(default_factory=list)
    width: int
    height: int
    mime: str
    show_prompt: bool
    prompt: str | None
    created_at: datetime
    expires_at: datetime | None


# ---------- Invite Links（V1.0 收尾） ----------

class InviteLinkOut(BaseModel):
    id: str
    token: str
    url: str
    email: str | None
    role: str
    expires_at: datetime | None
    used_at: datetime | None
    used_by_email: str | None
    revoked_at: datetime | None
    created_at: datetime


class InviteLinkPublicOut(BaseModel):
    token: str
    email: str | None
    role: str
    expires_at: datetime | None
    used: bool
    valid: bool
    invalid_reason: str | None = None  # expired/used/revoked/not_found


# ---------- System Settings（V1.0 收尾） ----------

class SystemSettingItem(BaseModel):
    key: str
    value: str | None
    has_value: bool
    is_sensitive: bool
    description: str


class SystemSettingsOut(BaseModel):
    items: list[SystemSettingItem]


class SystemSettingsUpdateItem(BaseModel):
    key: str
    value: str  # 空字符串 = 删除该 key（fallback 到 env）


class SystemSettingsUpdateIn(BaseModel):
    items: list[SystemSettingsUpdateItem]


# ---------- Providers（管理员 Provider Pool CRUD + 探活） ----------

class ProviderItemOut(BaseModel):
    name: str
    base_url: str
    api_key_hint: str
    priority: int
    weight: int
    enabled: bool
    proxy: str | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_concurrency: int = 1


class ProviderProxyOut(BaseModel):
    name: str
    type: str
    host: str
    port: int
    username: str | None = None
    password_hint: str | None = None
    private_key_path: str | None = None
    enabled: bool = True


class ProvidersOut(BaseModel):
    items: list[ProviderItemOut]
    proxies: list[ProviderProxyOut] = []
    source: str  # "db" | "env" | "none"


class AdminModelOut(BaseModel):
    id: str
    providers: list[str]
    object: str = "model"


class AdminModelsErrorOut(BaseModel):
    provider: str
    message: str


class AdminModelsOut(BaseModel):
    models: list[AdminModelOut]
    fetched_at: datetime
    errors: list[AdminModelsErrorOut] = []


class ProviderItemIn(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    priority: int = 0
    weight: int = 1
    enabled: bool = True
    proxy: str | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_concurrency: int = 1


class ProviderProxyIn(BaseModel):
    name: str
    type: str = "socks5"
    host: str
    port: int = 1080
    username: str | None = None
    password: str = ""
    private_key_path: str | None = None
    enabled: bool = True


class ProvidersUpdateIn(BaseModel):
    items: list[ProviderItemIn]
    proxies: list[ProviderProxyIn] = []


class ProvidersProbeIn(BaseModel):
    names: list[str] | None = None


class ProviderProbeResult(BaseModel):
    name: str
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    status: str = "unknown"  # "healthy" | "unhealthy" | "disabled" | "unknown"


class ProvidersProbeOut(BaseModel):
    items: list[ProviderProbeResult]
    probed_at: str | None = None


class ProviderStatsItem(BaseModel):
    name: str
    total: int = 0
    success: int = 0
    fail: int = 0
    success_rate: float = 0.0  # 0.0 ~ 1.0
    traffic_pct: float = 0.0   # 该 provider 占总请求的比例


class ProviderStatsOut(BaseModel):
    items: list[ProviderStatsItem]
    auto_probe_interval: int = 120  # 文本算术 probe 间隔（0=关闭，默认 120）
    auto_image_probe_interval: int = 0  # Image probe 间隔（0=关闭，默认 0）


# ---------- Sessions（V1.0 收尾：/me/sessions） ----------

class SessionOut(BaseModel):
    id: str
    ua: str | None
    ip: str | None
    created_at: datetime
    expires_at: datetime
    is_current: bool


class SessionsOut(BaseModel):
    items: list[SessionOut]


# ---------- Regenerate（V1.0 收尾） ----------

class RegenerateIn(BaseModel):
    intent: Literal["chat", "vision_qa", "text_to_image", "image_to_image"]
    idempotency_key: str


class RegenerateOut(BaseModel):
    assistant_message_id: str
    completion_id: str | None = None
    generation_ids: list[str] = []


__all__ = [
    "SignupIn",
    "LoginIn",
    "UserOut",
    "SystemPromptOut",
    "SystemPromptCreateIn",
    "SystemPromptPatchIn",
    "SystemPromptListOut",
    "ConversationOut",
    "ConversationPatchIn",
    "PostMessageIn",
    "PostMessageOut",
    "MessageOut",
    "ImageParamsIn",
    "ChatParamsIn",
    "AdvancedIn",
    "GenerationOut",
    "CompletionOut",
    "TaskItemOut",
    "ImageOut",
    "TaskQueueItem",
    "SSEEvent",
    "ErrorBody",
    "ErrorResponse",
    "AllowedEmailOut",
    "AdminUserOut",
    "UsageOut",
    "ShareOut",
    "PublicShareImageOut",
    "PublicShareOut",
    "InviteLinkOut",
    "InviteLinkPublicOut",
    "SystemSettingItem",
    "SystemSettingsOut",
    "SystemSettingsUpdateItem",
    "SystemSettingsUpdateIn",
    "ProviderItemOut",
    "ProvidersOut",
    "AdminModelOut",
    "AdminModelsErrorOut",
    "AdminModelsOut",
    "ProviderItemIn",
    "ProvidersUpdateIn",
    "ProvidersProbeIn",
    "ProviderProbeResult",
    "ProvidersProbeOut",
    "ProviderStatsItem",
    "ProviderStatsOut",
    "SessionOut",
    "SessionsOut",
    "RegenerateIn",
    "RegenerateOut",
]
