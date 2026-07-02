"""Pydantic I/O schemas——DESIGN §5 的请求/响应体。

约定：
- 对 API 与 Worker 都可见（Worker 通过 XADD payload 读取任务元信息）
- 字段保守：除非 DESIGN §5 里写了，否则不增加
- 前端 TypeScript 类型由 OpenAPI 生成（后续接 openapi-typescript）
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from .constants import (
    MAX_MESSAGE_ATTACHMENTS,
    MAX_PROMPT_CHARS,
)
from .sizing import AspectRatio as AspectRatioLiteral
from .url_security import is_private_host


_ASSET_URL_PREFIX_RE = re.compile(r"^asset\s*:\s*/\s*/", re.IGNORECASE)
_ASSET_ID_RE = re.compile(r"^asset[-_][A-Za-z0-9_-]+$", re.IGNORECASE)
_ASSET_URL_WRAPPER_CHARS = "\"'`“”‘’"


def normalize_asset_reference_url(raw_url: str) -> str | None:
    value = raw_url.strip().strip(_ASSET_URL_WRAPPER_CHARS).strip()
    if not value:
        return None
    without_prefix = _ASSET_URL_PREFIX_RE.sub("", value, count=1)
    if without_prefix == value and not _ASSET_ID_RE.fullmatch(value):
        return None
    asset_id = without_prefix.replace("\\", "/").lstrip("/").strip()
    return f"asset://{asset_id.lower()}" if asset_id else ""


class BaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Auth / User ----------


class SignupIn(BaseModel):
    # EmailStr 依赖 pydantic[email]（apps/api 已声明）；触发 422 而非 500，比手写正则更严格。
    email: EmailStr
    password: str = Field(max_length=128)
    display_name: str = ""
    invite_token: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class NavigationVisibilityOut(BaseModel):
    studio: bool = True
    video: bool = True
    projects: bool = True
    assets: bool = True


class RuntimeDefaultsOut(BaseModel):
    fast: bool = True
    upload_max_source_bytes: int = 50 * 1024 * 1024
    nav_visibility: NavigationVisibilityOut = Field(
        default_factory=NavigationVisibilityOut
    )


class UserOut(BaseOut):
    id: str
    email: str
    display_name: str
    role: str
    account_mode: Literal["wallet", "byok"] = "wallet"
    notification_email: bool
    default_system_prompt_id: str | None = None
    runtime_defaults: RuntimeDefaultsOut = Field(default_factory=RuntimeDefaultsOut)


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
    memory_disabled: bool = False
    active_scope_id: str | None = None
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
    items: list[TaskItemOut]
    next_cursor: str | None = None


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
    owner_generation_id: str | None = (
        None  # generated 图反查所属 Generation；uploaded 图为 None
    )
    width: int
    height: int
    mime: str
    blurhash: str | None
    url: str  # API 组装的短期签名 URL 或反代路径
    display_url: str | None = None
    preview_url: str | None = None
    thumb_url: str | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    is_dual_race_bonus: bool = False
    billing_free: bool = False
    billing_label: str | None = None
    billing_exempt_reason: str | None = None


class VideoOut(BaseOut):
    id: str
    url: str
    poster_url: str | None = None
    width: int
    height: int
    duration_ms: int
    fps: float | None = None
    has_audio: bool
    mime: str = "video/mp4"
    size_bytes: int | None = None
    faststart: bool | None = None
    created_at: datetime | None = None


class VideoTemporaryDownloadOut(BaseModel):
    source: str
    url: str
    expires_at: datetime
    expires_in_s: int


VideoAction = Literal["t2v", "i2v", "reference"]
VideoPricingVariant = Literal[
    "t2v",
    "i2v",
    "reference",
    "reference_image",
    "reference_video",
]
VideoResolution = Literal["480p", "720p", "1080p", "4k"]
VideoAspectRatio = Literal["adaptive", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"]
_VIDEO_REFERENCE_ID_RE = re.compile(r"^ref:(image|video|audio):[1-9][0-9]{0,2}$")


class VideoReferenceMediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["image", "video", "audio"]
    image_id: str | None = Field(default=None, max_length=36)
    video_id: str | None = Field(default=None, max_length=36)
    url: str | None = Field(default=None, max_length=2048)
    label: str | None = Field(default=None, max_length=32)
    ref_id: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_reference_source(self) -> "VideoReferenceMediaIn":
        if self.ref_id:
            ref_id = self.ref_id.strip().lower()
            match = _VIDEO_REFERENCE_ID_RE.fullmatch(ref_id)
            if match is None:
                raise ValueError("reference media ref_id must look like ref:<kind>:1")
            if match.group(1) != self.kind:
                raise ValueError("reference media ref_id kind must match kind")
            self.ref_id = ref_id
        sources = [
            bool((self.image_id or "").strip()),
            bool((self.video_id or "").strip()),
            bool((self.url or "").strip()),
        ]
        if sum(1 for item in sources if item) != 1:
            raise ValueError("reference media must include exactly one source")
        if self.kind == "image" and not (
            (self.image_id or "").strip() or (self.url or "").strip()
        ):
            raise ValueError("image reference requires image_id or url")
        if self.kind == "video" and not (
            (self.video_id or "").strip() or (self.url or "").strip()
        ):
            raise ValueError("video reference requires video_id or url")
        if self.kind == "audio" and not (self.url or "").strip():
            raise ValueError("audio reference requires url")
        if self.kind == "image" and (self.video_id or "").strip():
            raise ValueError("image reference must not include video_id")
        if self.kind == "video" and (self.image_id or "").strip():
            raise ValueError("video reference must not include image_id")
        if self.kind == "audio" and (
            (self.image_id or "").strip() or (self.video_id or "").strip()
        ):
            raise ValueError("audio reference supports url only")
        if self.url:
            asset_url = normalize_asset_reference_url(self.url)
            if asset_url is not None:
                if not asset_url:
                    raise ValueError("reference media asset url must not be empty")
                self.url = asset_url
                return self
            value = self.url.strip()
            parsed = urlsplit(value)
            if parsed.scheme.lower() == "asset":
                if not (parsed.netloc or parsed.path.strip("/")):
                    raise ValueError("reference media asset url must not be empty")
                return self
            if parsed.scheme.lower() != "https" or not parsed.hostname:
                raise ValueError("reference media url must be an https or asset URL")
            if parsed.username or parsed.password:
                raise ValueError("reference media url must not include credentials")
            if is_private_host(parsed.hostname):
                raise ValueError("reference media url host is not allowed")
        return self


class VideoReferenceMediaOut(BaseModel):
    kind: Literal["image", "video", "audio"]
    image_id: str | None = None
    video_id: str | None = None
    url: str | None = None
    label: str | None = None
    ref_id: str | None = None
    mime: str | None = None


class VideoCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: VideoAction
    model: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    input_image_id: str | None = Field(default=None, max_length=36)
    reference_media: list[VideoReferenceMediaIn] = Field(default_factory=list)
    duration_s: int = Field(ge=-1, le=15)
    resolution: VideoResolution
    aspect_ratio: VideoAspectRatio
    generate_audio: bool = True
    seed: int | None = Field(default=None, ge=-1, le=4_294_967_295)
    watermark: bool = False
    idempotency_key: str = Field(min_length=1, max_length=96)

    @model_validator(mode="after")
    def validate_action_image_contract(self) -> "VideoCreateIn":
        if self.duration_s != -1 and self.duration_s < 3:
            raise ValueError("duration_s must be -1 or between 3 and 15")
        if self.action == "t2v" and self.input_image_id:
            raise ValueError("t2v must not include input_image_id")
        if self.action == "t2v" and self.reference_media:
            raise ValueError("t2v must not include reference_media")
        if self.action == "i2v" and not self.input_image_id:
            raise ValueError("i2v requires input_image_id")
        if self.action == "i2v" and self.reference_media:
            raise ValueError("i2v must not include reference_media")
        if self.action == "reference" and self.input_image_id:
            raise ValueError("reference must not include input_image_id")
        if self.action == "reference":
            if not self.reference_media:
                raise ValueError("reference requires at least one reference media")
            image_count = sum(
                1 for item in self.reference_media if item.kind == "image"
            )
            video_count = sum(
                1 for item in self.reference_media if item.kind == "video"
            )
            audio_count = sum(
                1 for item in self.reference_media if item.kind == "audio"
            )
            if image_count > 9:
                raise ValueError("reference supports at most 9 images")
            if video_count > 3:
                raise ValueError("reference supports at most 3 videos")
            if audio_count > 1:
                raise ValueError("reference supports at most 1 audio")
        return self


class VideoPriceOptionOut(BaseModel):
    model: str
    action: VideoPricingVariant
    resolution: str | None = None
    variant: str | None = None
    unit: Literal["per_mtoken"] = "per_mtoken"
    price: MoneyOut
    enabled: bool = True
    note: str | None = None


class VideoModelOptionOut(BaseModel):
    model: str
    billing_model: str | None = None
    billing_models: dict[str, str] = Field(default_factory=dict)
    actions: list[VideoAction] = Field(default_factory=list)
    durations_s: list[int] = Field(default_factory=list)
    durations_by_action: dict[VideoAction, list[int]] = Field(default_factory=dict)
    durations_by_action_resolution: dict[VideoAction, dict[str, list[int]]] = Field(
        default_factory=dict
    )
    resolutions: list[VideoResolution] = Field(default_factory=list)


class VideoOptionsOut(BaseModel):
    enabled: bool
    models: list[VideoModelOptionOut] = Field(default_factory=list)
    durations_s: list[int] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    aspect_ratios: list[str] = Field(default_factory=list)
    generate_audio: bool = True
    pricing: list[VideoPriceOptionOut] = Field(default_factory=list)
    hold_estimates: dict[str, Any] = Field(default_factory=dict)
    unavailable_reason: str | None = None


class VideoGenerationOut(BaseOut):
    id: str
    action: str
    model: str
    prompt: str
    input_image_id: str | None = None
    reference_media: list[VideoReferenceMediaOut] = Field(default_factory=list)
    duration_s: int
    resolution: str
    aspect_ratio: str
    fps: int | None = None
    generate_audio: bool = True
    seed: int | None = None
    status: str
    progress_stage: str
    progress_pct: int
    provider_name: str | None = None
    provider_kind: str | None = None
    est_token_upper: int
    est_cost: MoneyOut
    billed_tokens: int | None = None
    billed_cost: MoneyOut | None = None
    video: VideoOut | None = None
    temporary_download: VideoTemporaryDownloadOut | None = None
    elapsed_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    finished_at: datetime | None = None


class VideoGenerationsOut(BaseModel):
    items: list[VideoGenerationOut]
    next_cursor: str | None = None


# ---------- Workflows ----------

WorkflowType = Literal["apparel_model_showcase", "poster_design"]
WorkflowRunStatus = Literal[
    "draft",
    "running",
    "needs_review",
    "completed",
    "failed",
]
WorkflowStepStatus = Literal[
    "waiting_input",
    "running",
    "needs_review",
    "approved",
    "failed",
    "completed",
]
WorkflowStepKey = Literal[
    # apparel_model_showcase steps
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
    # poster_design steps（与 apparel 互不重叠；前端按 type 解析）
    "copy_input",
    "style_selection",
    "copy_analysis",
    "master_generation",
    "master_approval",
    "multi_size_generation",
]


class WorkflowStepOut(BaseOut):
    id: str
    workflow_run_id: str
    step_key: str
    status: str
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] = Field(default_factory=dict)
    task_ids: list[str] = Field(default_factory=list)
    image_ids: list[str] = Field(default_factory=list)
    approved_at: datetime | None = None
    approved_by: str | None = None
    created_at: datetime
    updated_at: datetime


class ModelCandidateOut(BaseOut):
    id: str
    workflow_run_id: str
    candidate_index: int
    portrait_image_id: str | None = None
    front_image_id: str | None = None
    side_image_id: str | None = None
    back_image_id: str | None = None
    contact_sheet_image_id: str | None = None
    model_brief_json: dict[str, Any] = Field(default_factory=dict)
    task_ids: list[str] = Field(default_factory=list)
    status: str
    selected_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class QualityReportOut(BaseOut):
    id: str
    workflow_run_id: str
    image_id: str
    overall_score: int
    product_fidelity_score: int
    model_consistency_score: int
    aesthetic_score: int
    artifact_score: int
    issues_json: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: str
    created_at: datetime
    updated_at: datetime


class PosterMasterOut(BaseOut):
    id: str
    workflow_run_id: str
    candidate_index: int
    image_id: str | None = None
    style_summary_json: dict[str, Any] = Field(default_factory=dict)
    task_ids: list[str] = Field(default_factory=list)
    status: str
    selected_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PosterRenderOut(BaseOut):
    id: str
    workflow_run_id: str
    master_id: str | None = None
    aspect_ratio: str
    size: str
    image_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    status: str
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WorkflowRunOut(BaseOut):
    id: str
    conversation_id: str | None = None
    user_id: str
    type: str
    status: str
    title: str
    user_prompt: str
    product_image_ids: list[str] = Field(default_factory=list)
    current_step: str
    quality_mode: str
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    steps: list[WorkflowStepOut] = Field(default_factory=list)
    model_candidates: list[ModelCandidateOut] = Field(default_factory=list)
    quality_reports: list[QualityReportOut] = Field(default_factory=list)
    # 海报工作流（type=poster_design）使用；其它类型保持空列表，不破坏现有 schema。
    poster_masters: list[PosterMasterOut] = Field(default_factory=list)
    poster_renders: list[PosterRenderOut] = Field(default_factory=list)
    product_images: list[ImageOut] = Field(default_factory=list)
    generated_images: list[ImageOut] = Field(default_factory=list)
    generations: list[GenerationOut] = Field(default_factory=list)


class WorkflowRunListItemOut(BaseOut):
    id: str
    conversation_id: str | None = None
    type: str
    status: str
    title: str
    user_prompt: str
    product_image_ids: list[str] = Field(default_factory=list)
    current_step: str
    quality_mode: str
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    output_count: int = 0
    next_action: str


class WorkflowRunListOut(BaseModel):
    items: list[WorkflowRunListItemOut]
    next_cursor: str | None = None


class WorkflowRunPatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)


class ApparelWorkflowCreateIn(BaseModel):
    conversation_id: str | None = None
    product_image_ids: list[str] = Field(min_length=1, max_length=3)
    user_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    quality_mode: Literal["standard", "premium"] = "premium"
    title: str | None = Field(default=None, max_length=120)


class ApparelWorkflowCreateOut(BaseModel):
    workflow_run_id: str
    status: str
    current_step: str


class ProductAnalysisApproveIn(BaseModel):
    corrections: dict[str, Any] = Field(default_factory=dict)


class AccessoryPlanIn(BaseModel):
    enabled: bool = True
    items: list[str] = Field(default_factory=list, max_length=12)
    strength: Literal["subtle", "medium", "strong"] = "subtle"


class ModelCandidatesCreateIn(BaseModel):
    candidate_count: int = Field(default=3, ge=3, le=3)
    style_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    avoid: list[str] = Field(default_factory=list, max_length=20)
    accessory_plan: AccessoryPlanIn = Field(default_factory=AccessoryPlanIn)


class ModelCandidateApproveIn(BaseModel):
    adjustments: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    accessory_plan: AccessoryPlanIn = Field(default_factory=AccessoryPlanIn)
    selected_accessory_image_id: str | None = None


class AccessoryPreviewCreateIn(BaseModel):
    candidate_id: str
    accessory_plan: AccessoryPlanIn = Field(default_factory=AccessoryPlanIn)
    style_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)


class AccessorySelectionIn(BaseModel):
    selected_accessory_image_id: str | None = None


AgeSegment = Literal[
    "all",
    "user_favorites",
    "toddler",
    "child",
    "teen",
    "young_adult",
    "adult",
    "middle_aged",
    "senior",
]
ModelAgeSegment = Literal[
    "user_favorites",
    "toddler",
    "child",
    "teen",
    "young_adult",
    "adult",
    "middle_aged",
    "senior",
]
ModelLibrarySource = Literal["preset", "favorite", "user_upload", "generated"]
ModelLibraryVisibilityScope = Literal["global_preset", "user_private"]
# 模特库独立生成任务允许的张数档位。前端展示按钮 1/2/4/16，后端做白名单校验。
ModelLibraryGenerateCount = Literal[1, 2, 4, 16]


class ApparelModelLibrarySyncOut(BaseModel):
    status: Literal["ok", "failed", "skipped"] = "ok"
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    last_success_at: datetime | None = None
    last_error: str | None = None


class ApparelModelLibrarySyncStateOut(BaseModel):
    last_success_at: datetime | None = None
    last_error: str | None = None
    can_sync: bool = False
    github_contents_url: str | None = None


class ApparelModelLibraryItemOut(BaseModel):
    id: str
    source: ModelLibrarySource
    visibility_scope: ModelLibraryVisibilityScope
    title: str
    age_segment: ModelAgeSegment
    gender: str | None = None
    appearance_direction: str | None = None
    style_tags: list[str] = Field(default_factory=list)
    image_url: str
    # display 大图 URL —— lightbox 大图预览用。user item 走 display2048
    # variant；preset 走 preset 原图（preset 文件通常 <2MB，无需独立 display
    # 变体）。前端 lightbox 必须走 display_url 而非 thumb_url，否则 preset
    # 来源的图会用真小缩略图填 lightbox 导致拉伸糊。
    display_url: str | None = None
    thumb_url: str | None = None
    image_id: str | None = None
    preset_id: str | None = None
    version: int | None = None
    library_folder: str | None = None
    prompt_hint: str | None = None
    download_filename: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class ApparelModelLibraryListOut(BaseModel):
    items: list[ApparelModelLibraryItemOut]
    sync: ApparelModelLibrarySyncStateOut = Field(
        default_factory=ApparelModelLibrarySyncStateOut
    )


class ApparelModelLibraryItemCreateIn(BaseModel):
    source: Literal["favorite", "user_upload", "generated"] = "user_upload"
    visibility_scope: Literal["user_private"] = "user_private"
    image_id: str
    title: str = Field(min_length=1, max_length=120)
    age_segment: ModelAgeSegment
    gender: str | None = Field(default=None, max_length=40)
    appearance_direction: str | None = Field(default=None, max_length=80)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    # 上传/收藏入库时是否在后台触发 vision 自动识别（默认开）。
    auto_tag: bool = True


class ApparelModelLibraryItemPatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    age_segment: ModelAgeSegment | None = None
    gender: str | None = Field(default=None, max_length=40)
    appearance_direction: str | None = Field(default=None, max_length=80)
    style_tags: list[str] | None = Field(default=None, max_length=12)


class ApparelModelLibraryBatchDeleteIn(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=1000)


class ApparelModelLibraryBatchDeleteOut(BaseModel):
    ok: bool = True
    deleted: int = 0
    not_found: list[str] = Field(default_factory=list)


class ApparelModelLibrarySelectIn(BaseModel):
    library_item_id: str
    mode: Literal["use_directly"] = "use_directly"
    style_prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    accessory_plan: AccessoryPlanIn | None = None


class ApparelModelLibraryGenerateIn(BaseModel):
    """模特库独立生成入参（不绑定项目）。

    后端会创建一条隐藏的 WorkflowRun(type="apparel_model_library_generate")
    + 一个 step + N 个 candidate 任务，每个 candidate 输出一张独立模特肖像。
    """

    mode: Literal["text", "reference_image"] = "text"
    reference_image_id: str | None = Field(default=None, max_length=64)
    age_segment: ModelAgeSegment | None = None
    gender: Literal["female", "male"] | None = None
    genders: list[Literal["female", "male"]] | None = Field(default=None, max_length=2)
    appearance_direction: str | None = Field(default=None, max_length=80)
    extra_requirements: str | None = Field(default=None, max_length=400)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    count: ModelLibraryGenerateCount = 4
    # 生成完是否对每张自动 vision 打标签（用户筛选/收藏前预填字段，默认开）。
    auto_tag: bool = True

    @model_validator(mode="after")
    def _validate_mode(self) -> "ApparelModelLibraryGenerateIn":
        if self.mode == "reference_image":
            if not self.reference_image_id:
                raise ValueError(
                    "reference_image_id is required when mode='reference_image'"
                )
            return self
        if self.reference_image_id:
            raise ValueError(
                "reference_image_id only allowed when mode='reference_image'"
            )
        if self.age_segment is None:
            raise ValueError("age_segment is required when mode='text'")
        return self


class ApparelModelLibraryJobItemOut(BaseModel):
    """模特库任务里的单张产出。已收藏的 image_id 也可在浏览页里查到。"""

    image_id: str
    image_url: str
    # display2048 variant URL —— 给 lightbox 大图预览用（thumb_url 是 256 缩略图，
    # 直接拉伸会糊）。后端按需 materialize，前端总能拿到真大图。
    display_url: str | None = None
    thumb_url: str | None = None
    saved_item_id: str | None = None  # 已收藏入库时携带 library item id
    style_tags: list[str] = Field(default_factory=list)
    appearance_direction: str | None = None
    gender: str | None = None
    download_filename: str | None = None
    is_dual_race_bonus: bool = False
    billing_free: bool = False
    billing_label: str | None = None
    billing_exempt_reason: str | None = None


class ApparelModelLibraryJobOut(BaseModel):
    """聚合视图：

    - origin="library_generate"：独立模特库生成 workflow
    - origin="project_candidate"：项目里的 model_candidates step（聚合用，方便用户在一处看到所有生成中/已生成的模特）
    """

    job_id: str
    origin: Literal["library_generate", "project_candidate"]
    workflow_run_id: str
    project_title: str | None = None  # 仅 project_candidate 场景填
    status: Literal["queued", "running", "succeeded", "failed", "partial"]
    requested_count: int
    finished_count: int
    age_segment: ModelAgeSegment | None = None
    gender: str | None = None
    appearance_direction: str | None = None
    extra_requirements: str | None = None
    reference_image_id: str | None = None
    reference_image_url: str | None = None
    extracted_profile: dict[str, Any] | None = None
    items: list[ApparelModelLibraryJobItemOut] = Field(default_factory=list)
    # dual_race 模式下另一路 provider 的产出。前端展示在「候选区」，不参与 finished_count，可按需入库。
    candidates: list[ApparelModelLibraryJobItemOut] = Field(default_factory=list)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class ApparelModelLibraryJobsOut(BaseModel):
    items: list[ApparelModelLibraryJobOut]
    limit: int = 50
    offset: int = 0
    has_more: bool = False


class ApparelModelLibraryJobsClearOut(BaseModel):
    ok: bool = True
    deleted: int = 0


class ApparelModelLibrarySaveJobItemIn(BaseModel):
    """从任务中心把一张 generated 图收藏入库。"""

    title: str = Field(min_length=1, max_length=120)
    age_segment: ModelAgeSegment
    gender: Literal["female", "male"] = "female"
    appearance_direction: str | None = Field(default=None, max_length=80)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    auto_tag: bool = True


class ApparelModelLibraryAutoTagOut(BaseModel):
    """vision 自动识别返回。"""

    item_id: str
    style_tags: list[str] = Field(default_factory=list)
    appearance_direction: str | None = None
    age_segment: ModelAgeSegment | None = None
    gender: str | None = None
    notes: str | None = None  # 模型给出的简短说明（可选展示）


class ModelCandidateSaveToLibraryIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    age_segment: ModelAgeSegment
    gender: str | None = Field(default=None, max_length=40)
    appearance_direction: str | None = Field(default=None, max_length=80)
    style_tags: list[str] = Field(default_factory=list, max_length=12)


class ShowcaseImagesCreateIn(BaseModel):
    template: Literal[
        "white_ecommerce",
        "premium_studio",
        "urban_commute",
        "lifestyle",
        "daily_snapshot",
        "natural_phone_snapshot",
        "social_seed",
    ] = "premium_studio"
    shot_plan: list[
        Literal[
            "front_full_body",
            "natural_pose",
            "detail_half_body",
            "side_or_back",
        ]
    ] = Field(
        default_factory=lambda: [
            "front_full_body",
            "natural_pose",
            "detail_half_body",
        ],
        min_length=1,
        max_length=4,
    )
    aspect_ratio: AspectRatioLiteral = "4:5"
    final_quality: Literal["standard", "high", "4k"] = "high"
    output_count: Literal[1, 2, 4, 8, 16] = 4
    scene_environment: Literal["indoor", "outdoor"] = "indoor"
    scene_strategy: Literal["balanced", "natural_series", "editorial_campaign"] = (
        "natural_series"
    )
    scene_variety: Literal["safe", "rich", "wild"] = "rich"
    scene_planner: Literal["gpt55_preflight", "gpt55_batch_only", "rules_fallback"] = (
        "gpt55_preflight"
    )
    continuity_anchor: Literal["none", "accessory", "pet", "location_series"] = (
        "accessory"
    )
    allow_pet: bool = False
    allow_background_people: bool = True


class ImageRevisionIn(BaseModel):
    instruction: str = Field(max_length=MAX_PROMPT_CHARS)
    scope: Literal["full_image", "local_repair"] = "full_image"


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
    account_mode: Literal["wallet", "byok"] = "wallet"
    display_name: str | None
    created_at: datetime
    generations_count: int
    completions_count: int
    messages_count: int


# ---------- Billing / Wallet ----------


class MoneyOut(BaseModel):
    micro: int
    rmb: str


class WalletOut(BaseModel):
    mode: Literal["wallet", "byok"]
    balance: MoneyOut | None
    hold: MoneyOut | None
    low_balance_threshold: MoneyOut | None = None
    frozen: bool = False


class WalletTransactionOut(BaseOut):
    id: str
    kind: str
    amount: MoneyOut
    balance_after: MoneyOut
    hold_after: MoneyOut
    ref_type: str | None = None
    ref_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    created_by_admin: str | None = None


class WalletTransactionListOut(BaseModel):
    items: list[WalletTransactionOut]
    next_cursor: str | None = None


class BillingWindowOut(BaseModel):
    used_micro: int
    limit_micro: int
    resets_at: datetime | None = None


class BillingUsageByKindOut(BaseModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    image: int = 0
    reasoning: int = 0


class BillingSnapshotOut(BaseModel):
    balance_micro: int
    billing_rate_multiplier: str
    windows: dict[str, BillingWindowOut]
    by_kind_30d: BillingUsageByKindOut


class AdminBillingUsageOut(BaseModel):
    user_id: str
    balance_micro: int
    billing_rate_multiplier: str
    range_start: datetime
    range_end: datetime
    windows: dict[str, BillingWindowOut]
    by_kind_30d: BillingUsageByKindOut
    total_micro: int = 0
    transaction_count: int = 0


class AdminPricingBulkRatesIn(BaseModel):
    input: str | int | float | None = None
    output: str | int | float | None = None
    cache_read: str | int | float | None = None
    cache_creation: str | int | float | None = None
    cache_creation_5m: str | int | float | None = None
    cache_creation_1h: str | int | float | None = None
    image_output: str | int | float | None = None
    reasoning: str | int | float | None = None
    input_priority: str | int | float | None = None
    output_priority: str | int | float | None = None
    cache_read_priority: str | int | float | None = None
    long_context_threshold: int | None = None
    long_context_input_multiplier: float | None = None
    long_context_output_multiplier: float | None = None


class AdminPricingBulkIn(BaseModel):
    model: str = Field(min_length=1, max_length=64)
    channel: str | None = Field(default=None, max_length=32)
    rates: AdminPricingBulkRatesIn
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


PricingUnit = Literal[
    "per_image",
    "per_1k_tokens_in",
    "per_1k_tokens_out",
    "per_1k_tokens_cache_read",
    "per_1k_tokens_cache_creation",
    "per_1k_tokens_cache_creation_5m",
    "per_1k_tokens_cache_creation_1h",
    "per_1k_tokens_image_output",
    "per_1k_tokens_reasoning",
    "per_1k_tokens_input_priority",
    "per_1k_tokens_output_priority",
    "per_1k_tokens_cache_read_priority",
    "long_context_threshold",
    "long_context_input_multiplier",
    "long_context_output_multiplier",
    "per_mtoken",
]


class PricingRuleOut(BaseOut):
    id: str
    scope: Literal["image_size", "chat_model", "video"]
    key: str
    variant: str = "default"
    unit: PricingUnit
    price: MoneyOut
    enabled: bool
    note: str | None = None
    created_at: datetime
    updated_at: datetime


class PricingRulesOut(BaseModel):
    items: list[PricingRuleOut]
    image_size_thresholds: dict[str, int] | None = None
    billing_enabled: bool | None = None
    show_estimate_in_composer: bool | None = None


class PricingRuleUpsertIn(BaseModel):
    scope: Literal["image_size", "chat_model", "video"]
    key: str = Field(min_length=1, max_length=64)
    variant: str = Field(default="default", min_length=1, max_length=32)
    unit: PricingUnit
    price_rmb: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    note: str | None = Field(default=None, max_length=500)


class PricingRulesUpdateIn(BaseModel):
    items: list[PricingRuleUpsertIn] = Field(min_length=1, max_length=500)
    image_size_thresholds: dict[str, int] | None = None
    force: bool = False


class PricingImportIn(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    rate: float = Field(default=1.0, gt=0, le=100)


class RedemptionIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)


class RedemptionOut(BaseModel):
    amount: MoneyOut
    balance: MoneyOut


class RedemptionUsageOut(BaseOut):
    id: str
    code_id: str
    amount: MoneyOut
    redeemed_at: datetime


class RedemptionUsageListOut(BaseModel):
    items: list[RedemptionUsageOut]
    next_cursor: str | None = None


class AdminRedemptionCodeOut(BaseOut):
    id: str
    code_prefix: str
    amount: MoneyOut
    max_redemptions: int
    redeemed_count: int
    usable_count: int = 0
    status: Literal["active", "revoked", "expired", "exhausted"] = "active"
    batch_id: str | None = None
    note: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime


class AdminRedemptionCodeListOut(BaseModel):
    items: list[AdminRedemptionCodeOut]
    next_cursor: str | None = None


class AdminRedemptionUsageOut(BaseOut):
    id: str
    code_id: str
    user_id: str
    user_email: str | None = None
    amount: MoneyOut
    wallet_tx_id: str
    redeemed_at: datetime
    ip_hash: str | None = None


class AdminRedemptionUsageListOut(BaseModel):
    items: list[AdminRedemptionUsageOut]
    next_cursor: str | None = None


class AdminRedemptionCodeCreateIn(BaseModel):
    amount_rmb: str = Field(min_length=1, max_length=32)
    count: int = Field(default=1, ge=1, le=1000)
    max_redemptions: int = Field(default=1, ge=1, le=1000)
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("expires_at")
    @classmethod
    def _expires_at_must_be_future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return value
        from datetime import timezone as _tz

        now = datetime.now(_tz.utc)
        # Require at least 1 minute of validity so the batch isn't dead-on-arrival.
        if value.tzinfo is None:
            value = value.replace(tzinfo=_tz.utc)
        if (value - now).total_seconds() < 60:
            raise ValueError("expires_at must be at least 1 minute in the future")
        return value


class AdminRedemptionCodeCreateOut(BaseModel):
    batch_id: str
    count: int
    amount: MoneyOut
    download_token: str
    plaintext_codes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class AdminWalletOut(BaseModel):
    user_id: str
    email: str
    account_mode: Literal["wallet", "byok"]
    wallet: WalletOut
    last_topup_at: datetime | None = None
    last_charge_at: datetime | None = None


class AdminWalletListOut(BaseModel):
    items: list[AdminWalletOut]
    next_cursor: str | None = None


class AdminWalletDetailOut(AdminWalletOut):
    last_redemption_at: datetime | None = None
    transactions: list[WalletTransactionOut] = Field(default_factory=list)
    redemptions: list[AdminRedemptionUsageOut] = Field(default_factory=list)


class AdminBillingAuditEventOut(BaseOut):
    id: str
    event_type: str
    user_id: str | None = None
    target_user_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AdminBillingOverviewOut(BaseModel):
    billing_enabled: bool
    redemption_secret_configured: bool
    bootstrap_completed: bool
    wallet_total_balance: MoneyOut
    active_holds_count: int
    active_holds: MoneyOut
    codes_active: int
    codes_redeemed_24h: int
    codes_redeemed_24h_amount: MoneyOut
    charges_24h: MoneyOut
    thresholds_pricing_aligned: bool
    thresholds_missing_prices: list[str] = Field(default_factory=list)
    recent_audit_events: list[AdminBillingAuditEventOut] = Field(default_factory=list)


class AdminWalletAuditOut(BaseModel):
    ok: bool
    transactions: int
    users: int
    mismatch_count: int
    mismatches: list[str] = Field(default_factory=list)


class AdminOrphanHoldOut(BaseModel):
    tx: WalletTransactionOut
    user_id: str
    age_seconds: int


class AdminBillingBootstrapIn(BaseModel):
    redemption_code_secret: str | None = Field(
        default=None, min_length=16, max_length=2048
    )
    enabled: bool = True
    usd_to_rmb_rate: float = Field(default=1.0, gt=0, le=100)
    low_balance_warn_rmb: str = Field(default="2")
    image_size_thresholds: dict[str, int] = Field(
        default_factory=lambda: {"1k": 1_572_864, "2k": 3_686_400, "4k": 8_294_400}
    )
    image_prices_rmb: dict[str, str] = Field(
        default_factory=lambda: {"1k": "0.2", "2k": "0.4", "4k": "0.8"}
    )


class AdminRedemptionBatchRedownloadOut(BaseModel):
    batch_id: str
    count: int
    download_token: str
    plaintext_codes: list[str] = Field(default_factory=list)
    expires_in_seconds: int = 300


class AdminWalletAdjustIn(BaseModel):
    amount_rmb_signed: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=1, max_length=500)


class AdminSetAccountModeIn(BaseModel):
    mode: Literal["wallet", "byok"]
    on_residual_balance: Literal["freeze", "zero"] = "freeze"


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


# ---------- Storage 后端（local / smb 切换） ----------


class StorageMountStatusOut(BaseModel):
    mode: str = ""  # "local" | "smb" | ""
    mounted: bool = False
    source: str = ""
    fstype: str = ""
    target: str = "/opt/lumendata"
    disabled: bool = False
    updated_at: int | None = None


class StorageLocalConfigOut(BaseModel):
    root: str = "/var/lib/lumen-data"


class StorageSmbConfigOut(BaseModel):
    host: str = ""
    port: int = 0  # 0 = 留空走默认 445；非零时 mount.cifs 用 -o port=N
    share: str = ""
    subpath: str = "/"
    username: str = ""
    has_password: bool = False


class StorageConfigOut(BaseModel):
    backend: str = ""  # "local" | "smb" | ""
    local: StorageLocalConfigOut
    smb: StorageSmbConfigOut
    status: StorageMountStatusOut | None = None
    last_apply: dict | None = None
    last_test: dict | None = None


class StorageLocalConfigIn(BaseModel):
    root: str = Field(..., min_length=1, max_length=512)


class StorageSmbConfigIn(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    # 0 / 不传 = 走 mount.cifs 默认 445；其他值 1-65535 写入 -o port=
    port: int = Field(0, ge=0, le=65535)
    share: str = Field(..., min_length=1, max_length=255)
    subpath: str = Field("/", max_length=512)
    username: str = Field(..., min_length=1, max_length=255)
    # 空字符串 = 保留旧值
    password: str = Field("", max_length=512)


class StorageConfigUpdateIn(BaseModel):
    backend: str  # "local" | "smb"
    local: StorageLocalConfigIn | None = None
    smb: StorageSmbConfigIn | None = None


class StorageTestIn(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(0, ge=0, le=65535)
    share: str = Field(..., min_length=1, max_length=255)
    subpath: str = Field("/", max_length=512)
    username: str = Field(..., min_length=1, max_length=255)
    # 空字符串 = 用已存的密码（必须 has_password）
    password: str = Field("", max_length=512)


class StorageTestResultOut(BaseModel):
    status: str  # "ok" | "fail" | "pending"
    message: str = ""
    tested_at: int | None = None
    call_id: str | None = None


class StorageApplyResponseOut(BaseModel):
    config: StorageConfigOut
    call_id: str
    status: str  # "pending" | "ok" | "fail"
    message: str = ""


# ---------- Providers（管理员 Provider Pool CRUD + 探活） ----------


class ProviderItemOut(BaseModel):
    name: str
    base_url: str
    api_key_hint: str
    priority: int
    weight: int
    enabled: bool
    purposes: list[Literal["chat", "image", "embedding"]] = Field(
        default_factory=lambda: ["chat", "image"]
    )
    proxy: str | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: Literal["url", "file"] = "url"
    image_concurrency: int = 1
    # Capability tri-state (image-stability-hardening §P2). null = 未知（默认）。
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


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
    purposes: list[Literal["chat", "image", "embedding"]] = Field(
        default_factory=lambda: ["chat", "image"], min_length=1
    )
    proxy: str | None = None
    image_jobs_enabled: bool = False
    image_jobs_endpoint: str = "auto"
    image_jobs_endpoint_lock: bool = False
    image_jobs_base_url: str = ""
    image_edit_input_transport: Literal["url", "file"] = "url"
    image_concurrency: int = 1
    # Capability tri-state（详见 ProviderItemOut 注释）
    responses_supported: bool | None = None
    image_generations_supported: bool | None = None
    image_responses_supported: bool | None = None


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


VideoProviderKind = Literal[
    "volcano",
    "volcano_third_party",
    "volcano_newapi",
    "dashscope",
    "veo",
    "omni_flash",
    "fake",
]


class VideoProviderItemOut(BaseModel):
    name: str
    kind: VideoProviderKind = "volcano"
    base_url: str
    api_key_hint: str
    enabled: bool = True
    priority: int = 0
    weight: int = 1
    concurrency: int = 1
    proxy: str | None = None
    models: dict[str, str] = Field(default_factory=dict)


class VideoProvidersOut(BaseModel):
    enabled: bool = False
    items: list[VideoProviderItemOut]
    proxies: list[ProviderProxyOut] = Field(default_factory=list)
    source: str  # "db" | "env" | "none"


class VideoProviderItemIn(BaseModel):
    name: str
    kind: VideoProviderKind = "volcano"
    base_url: str
    api_key: str = ""
    enabled: bool = True
    priority: int = 0
    weight: int = 1
    concurrency: int = 1
    proxy: str | None = None
    models: dict[str, str] = Field(default_factory=dict)


class VideoProvidersUpdateIn(BaseModel):
    enabled: bool = False
    items: list[VideoProviderItemIn]


class ProvidersProbeIn(BaseModel):
    names: list[str] | None = None


class ProviderProbeResult(BaseModel):
    name: str
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    status: str = "unknown"  # "healthy" | "unhealthy" | "disabled" | "unknown"
    # image-stability-hardening §P2 capability 探测信号。
    # 取值：
    #   None           — 探测未给出明确能力信号（默认）
    #   "supported"    — 端点确认支持
    #   "unsupported"  — 端点确认不支持（HTTP 404/405），可写 capability=False
    #   "auth"         — 鉴权问题（401/403），不能据此判定能力
    #   "transient"    — 临时不健康（429/5xx），不能据此判定能力
    capability_signal: str | None = None
    # 上游 HTTP status，便于 UI 直接显示
    http_status: int | None = None


class ProvidersProbeOut(BaseModel):
    items: list[ProviderProbeResult]
    probed_at: str | None = None


class ProviderStatsItem(BaseModel):
    name: str
    total: int = 0
    success: int = 0
    fail: int = 0
    success_rate: float = 0.0  # 0.0 ~ 1.0
    traffic_pct: float = 0.0  # 该 provider 占总请求的比例


class ProviderStatsOut(BaseModel):
    items: list[ProviderStatsItem]
    auto_probe_interval: int = 120  # 文本算术 probe 间隔（0=关闭，默认 120）
    auto_image_probe_interval: int = 0  # Image probe 间隔（0=关闭，默认 0）


# ---------- BYOK 用户自带 API Key ----------

ByokPurpose = Literal["chat", "image", "embedding"]


def _default_byok_purposes() -> list[ByokPurpose]:
    return ["chat", "image"]


class ByokSettingsOut(BaseModel):
    mode_enabled: bool = False
    byok_signup_enabled: bool = False
    byok_signup_bypasses_allowlist: bool = False
    fallback_to_admin_provider: bool = False
    validation_model: str = "gpt-5.4"
    validation_timeout_ms: int = 15000
    pending_token_ttl_seconds: int = 900
    retention_hide_enabled: bool = True
    retention_delete_enabled: bool = False
    retention_hide_days: int = 3
    retention_delete_days: int = 7


class ByokSettingsPatchIn(BaseModel):
    mode_enabled: bool | None = None
    byok_signup_enabled: bool | None = None
    byok_signup_bypasses_allowlist: bool | None = None
    fallback_to_admin_provider: bool | None = None
    validation_model: str | None = Field(default=None, max_length=64)
    validation_timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    pending_token_ttl_seconds: int | None = Field(default=None, ge=60, le=3600)
    retention_hide_enabled: bool | None = None
    retention_delete_enabled: bool | None = None
    retention_hide_days: int | None = Field(default=None, ge=1, le=3650)
    retention_delete_days: int | None = Field(default=None, ge=1, le=3650)


class ApiSupplierTemplateOut(BaseOut):
    id: str
    name: str
    slug: str
    base_url: str
    enabled: bool
    public_signup_enabled: bool
    user_bind_enabled: bool
    purposes: list[ByokPurpose]
    validation_model: str
    default_chat_model: str
    # review #12：image 任务必须用独立的 default_image_model；nullable
    # 表示该 supplier 不支持图片或 admin 未配置。
    default_image_model: str | None = None
    fast_chat_model: str | None = None
    validation_timeout_ms: int
    proxy_name: str | None = None
    text_concurrency_per_key: int
    image_concurrency_per_key: int
    capabilities_jsonb: dict[str, Any] = Field(default_factory=dict)
    active_credentials: int = 0
    recent_success_rate: float | None = None
    recent_error_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ApiSupplierTemplatePublicOut(BaseModel):
    id: str
    name: str
    purposes: list[ByokPurpose]
    validation_model: str


class ApiSupplierTemplateListOut(BaseModel):
    items: list[ApiSupplierTemplateOut]


class ApiSupplierTemplatePublicListOut(BaseModel):
    items: list[ApiSupplierTemplatePublicOut]


def _validate_supplier_base_url(v: str) -> str:
    """review #18：BYOK supplier 的 base_url 必须是 http(s) URL，禁止内嵌凭证。

    与 system_settings.providers 校验行为对齐：清理空白/末尾斜杠、
    要求 scheme + hostname、拒绝 user:pass@host 这种历史漏洞。
    """
    from urllib.parse import urlsplit

    v = v.strip().rstrip("/")
    parsed = urlsplit(v)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url must be http or https")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain credentials")
    if not parsed.hostname:
        raise ValueError("base_url must include a hostname")
    return v


def _validate_byok_api_key(v: str) -> str:
    """review #18：BYOK 用户 API Key shape 校验；strip 后判空+长度上限。"""
    v = v.strip()
    if not v:
        raise ValueError("api_key is required")
    if len(v) > 512:
        raise ValueError("api_key exceeds 512 characters")
    return v


class ApiSupplierTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # 设计 §5.2 / review #8：slug 未传时由后端从 name 派生（去空白 + lower-case +
    # 限制字符集），便于公开列表里直接走 GET /byok/suppliers/<slug>。
    slug: str | None = Field(default=None, max_length=80)
    base_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    public_signup_enabled: bool = False
    user_bind_enabled: bool = True
    purposes: list[ByokPurpose] = Field(
        default_factory=_default_byok_purposes, min_length=1
    )
    validation_model: str = Field(default="gpt-5.4", min_length=1, max_length=64)
    default_chat_model: str = Field(default="gpt-5.4", min_length=1, max_length=64)
    # review #12：默认 None，由 admin 显式选填；不在 schema 写默认 image 模型，
    # 避免误把 chat-only supplier 当成支持 image 任务。
    default_image_model: str | None = Field(default=None, max_length=128)
    fast_chat_model: str | None = Field(default="gpt-5.4-mini", max_length=64)
    validation_timeout_ms: int = Field(default=15000, ge=1000, le=120000)
    proxy_name: str | None = Field(default=None, max_length=120)
    text_concurrency_per_key: int = Field(default=4, ge=1, le=100)
    image_concurrency_per_key: int = Field(default=1, ge=1, le=32)
    capabilities_jsonb: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str) -> str:
        return _validate_supplier_base_url(v)


class ApiSupplierTemplatePatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    slug: str | None = Field(default=None, max_length=80)
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None
    public_signup_enabled: bool | None = None
    user_bind_enabled: bool | None = None
    purposes: list[ByokPurpose] | None = Field(default=None, min_length=1)
    validation_model: str | None = Field(default=None, min_length=1, max_length=64)
    default_chat_model: str | None = Field(default=None, min_length=1, max_length=64)
    # review #12：patch 时 None 表示不变；显式传 "" 或具体 model id 才更新。
    default_image_model: str | None = Field(default=None, max_length=128)
    fast_chat_model: str | None = Field(default=None, max_length=64)
    validation_timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    proxy_name: str | None = Field(default=None, max_length=120)
    text_concurrency_per_key: int | None = Field(default=None, ge=1, le=100)
    image_concurrency_per_key: int | None = Field(default=None, ge=1, le=32)
    capabilities_jsonb: dict[str, Any] | None = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_supplier_base_url(v)


class ApiSupplierProbeIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        return _validate_byok_api_key(v)


class ApiKeyVerifyIn(BaseModel):
    supplier_id: str
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        return _validate_byok_api_key(v)


class ApiKeyVerifyOut(BaseModel):
    ok: bool
    verification_token: str
    supplier_id: str
    key_hint: str
    verified_at: datetime


class SignupByokIn(BaseModel):
    email: EmailStr
    # review #18：与 SignupIn.password 一致的强度上下限；上限避免 bcrypt 撞 72-byte
    # 截断 + 阻挡明显恶意大体积 payload。下限 8 是登录类系统最低基线。
    password: str = Field(min_length=8, max_length=128)
    display_name: str = ""
    verification_token: str = Field(min_length=1)
    invite_token: str | None = None


class UserApiCredentialOut(BaseOut):
    id: str
    supplier_id: str
    supplier_name: str
    key_hint: str
    status: str
    last_verified_at: datetime | None = None
    last_failed_at: datetime | None = None
    last_error_code: str | None = None
    rate_limited_until: datetime | None = None
    created_at: datetime
    updated_at: datetime


class UserApiCredentialListOut(BaseModel):
    items: list[UserApiCredentialOut]


class UserApiCredentialUpdateIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, v: str) -> str:
        return _validate_byok_api_key(v)


class ApiSupplierStatsOut(BaseModel):
    supplier_id: str
    active_credentials: int = 0
    recent_success_rate: float | None = None
    recent_error_counts: dict[str, int] = Field(default_factory=dict)


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


# ---------- Poster Design Workflow ----------
# 实施决策（2026-05-12）：全 AI 出图（无文字层独立 Canvas），文字直接塞 prompt；
# inpaint 作为返修工具复用 Generation.mask_image_id 字段。schemas 风格 mirror apparel。
PosterAspectRatio = Literal["1:1", "9:16", "16:9", "3:4", "4:3", "2:3", "3:2", "4:5"]
PosterRevisionScope = Literal["background", "inpaint", "style"]


class PosterBrandAssetsIn(BaseModel):
    """可选品牌资产；前端不传时全部 None，prompt 里跳过该段。"""

    logo_image_id: str | None = None
    product_image_id: str | None = None
    primary_color: str | None = Field(default=None, max_length=24)
    font_family: str | None = Field(default=None, max_length=64)


class PosterDesignWorkflowCreateIn(BaseModel):
    """创建海报工作流入参（mirror ApparelWorkflowCreateIn）。"""

    conversation_id: str | None = None
    copy_text: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    style_id: str = Field(min_length=1, max_length=128)
    target_aspects: list[PosterAspectRatio] = Field(
        default_factory=lambda: ["1:1", "9:16", "16:9", "3:4"],
        min_length=1,
        max_length=8,
    )
    brand_assets: PosterBrandAssetsIn = Field(default_factory=PosterBrandAssetsIn)
    quality_mode: Literal["standard", "premium"] = "premium"
    title: str | None = Field(default=None, max_length=120)


class PosterDesignWorkflowCreateOut(BaseModel):
    workflow_run_id: str
    status: str
    current_step: str


class CopyAnalysisApproveIn(BaseModel):
    """文案分析阶段确认入参。corrections 字段为用户手工修正，None 表示沿用 AI 输出。"""

    corrections: dict[str, Any] = Field(default_factory=dict)


class PosterMastersCreateIn(BaseModel):
    """生成母版候选入参。固定 4 张，多于/少于 4 也支持但默认 4。"""

    candidate_count: int = Field(default=4, ge=1, le=8)
    size_mode: Literal["auto", "fixed"] = "fixed"
    # size 仅在 size_mode=fixed 时使用；不传则按 quality_mode 自动选 1:1 preset。
    size: str | None = Field(default=None, max_length=16)


class PosterMasterApproveIn(BaseModel):
    """选定母版入参。adjustments 给后续多尺寸 prompt 留口子，可空。"""

    adjustments: str = Field(default="", max_length=MAX_PROMPT_CHARS)


class PosterRendersCreateIn(BaseModel):
    """生成多尺寸成品入参；每个 aspect 独立 Generation 任务（stagger 入队）。"""

    aspects: list[PosterAspectRatio] = Field(
        default_factory=lambda: ["1:1", "9:16", "16:9", "3:4"],
        min_length=1,
        max_length=8,
    )
    use_master_as_reference: bool = True
    quality_mode: Literal["standard", "premium"] = "premium"


class PosterReviseIn(BaseModel):
    """单张返修入参。scope=background/inpaint/style；inpaint 需要 mask_image_id。"""

    scope: PosterRevisionScope = "background"
    instruction: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    # 仅 scope=inpaint 时使用；前端先 POST /images/upload 拿到 image_id 再带进来。
    mask_image_id: str | None = None

    @model_validator(mode="after")
    def _validate_inpaint_has_mask(self) -> "PosterReviseIn":
        if self.scope == "inpaint" and not self.mask_image_id:
            raise ValueError("inpaint scope requires mask_image_id")
        return self


class PosterInpaintIn(BaseModel):
    """inpaint 专用端点入参；mask_image_id 必填。"""

    instruction: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    mask_image_id: str = Field(min_length=1, max_length=36)


__all__ = [
    "SignupIn",
    "LoginIn",
    "NavigationVisibilityOut",
    "RuntimeDefaultsOut",
    "UserOut",
    "SystemPromptOut",
    "SystemPromptCreateIn",
    "SystemPromptPatchIn",
    "SystemPromptListOut",
    "ConversationOut",
    "ConversationPatchIn",
    "AttachmentRole",
    "MessageAttachmentIn",
    "PostMessageIn",
    "PostMessageOut",
    "MessageOut",
    "ImageParamsIn",
    "ChatParamsIn",
    "AdvancedIn",
    "GenerationOut",
    "CompletionOut",
    "TaskItemOut",
    "ActiveTasksOut",
    "ImageOut",
    "VideoOut",
    "VideoTemporaryDownloadOut",
    "VideoAction",
    "VideoCreateIn",
    "VideoReferenceMediaIn",
    "VideoReferenceMediaOut",
    "VideoPriceOptionOut",
    "VideoModelOptionOut",
    "VideoOptionsOut",
    "VideoGenerationOut",
    "VideoGenerationsOut",
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
    "VideoProviderKind",
    "VideoProviderItemOut",
    "VideoProvidersOut",
    "VideoProviderItemIn",
    "VideoProvidersUpdateIn",
    "MoneyOut",
    "WalletOut",
    "WalletTransactionOut",
    "WalletTransactionListOut",
    "BillingWindowOut",
    "BillingUsageByKindOut",
    "BillingSnapshotOut",
    "AdminBillingUsageOut",
    "AdminPricingBulkRatesIn",
    "AdminPricingBulkIn",
    "PricingRuleOut",
    "PricingRulesOut",
    "PricingRuleUpsertIn",
    "PricingRulesUpdateIn",
    "PricingImportIn",
    "RedemptionIn",
    "RedemptionOut",
    "RedemptionUsageOut",
    "RedemptionUsageListOut",
    "AdminRedemptionCodeOut",
    "AdminRedemptionCodeListOut",
    "AdminRedemptionUsageOut",
    "AdminRedemptionUsageListOut",
    "AdminRedemptionCodeCreateIn",
    "AdminRedemptionCodeCreateOut",
    "AdminWalletOut",
    "AdminWalletListOut",
    "AdminWalletDetailOut",
    "AdminBillingAuditEventOut",
    "AdminBillingOverviewOut",
    "AdminWalletAuditOut",
    "AdminOrphanHoldOut",
    "AdminBillingBootstrapIn",
    "AdminRedemptionBatchRedownloadOut",
    "AdminWalletAdjustIn",
    "AdminSetAccountModeIn",
    "StorageMountStatusOut",
    "StorageLocalConfigOut",
    "StorageSmbConfigOut",
    "StorageConfigOut",
    "StorageLocalConfigIn",
    "StorageSmbConfigIn",
    "StorageConfigUpdateIn",
    "StorageTestIn",
    "StorageTestResultOut",
    "StorageApplyResponseOut",
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
    "ByokSettingsOut",
    "ByokSettingsPatchIn",
    "ApiSupplierTemplateOut",
    "ApiSupplierTemplatePublicOut",
    "ApiSupplierTemplateListOut",
    "ApiSupplierTemplatePublicListOut",
    "ApiSupplierTemplateIn",
    "ApiSupplierTemplatePatchIn",
    "ApiSupplierProbeIn",
    "ApiKeyVerifyIn",
    "ApiKeyVerifyOut",
    "SignupByokIn",
    "UserApiCredentialOut",
    "UserApiCredentialListOut",
    "UserApiCredentialUpdateIn",
    "ApiSupplierStatsOut",
    "SessionOut",
    "SessionsOut",
    "RegenerateIn",
    "RegenerateOut",
    # Poster Design Workflow
    "PosterMasterOut",
    "PosterRenderOut",
    "PosterAspectRatio",
    "PosterRevisionScope",
    "PosterBrandAssetsIn",
    "PosterDesignWorkflowCreateIn",
    "PosterDesignWorkflowCreateOut",
    "CopyAnalysisApproveIn",
    "PosterMastersCreateIn",
    "PosterMasterApproveIn",
    "PosterRendersCreateIn",
    "PosterReviseIn",
    "PosterInpaintIn",
]


# ---------- Poster Style Library（V1.1 海报工作流） ----------
# 风格库的 schema 与 ApparelModelLibrary* 一一对应，但：
# - 没有 age_segment / gender / appearance_direction（人像专属字段）
# - 多 category：illustration / 3d / minimal / retro / traditional / photo / other
# - 多 prompt_template（注入海报生成约束）+ palette / mood / recommended_aspects
# - 1 个 cover_image_id + 0~N 个 sample_image_id（模特库每条只有 1 张 contact sheet）

PosterStyleSource = Literal["preset", "favorite", "user_upload", "generated"]
PosterStyleVisibilityScope = Literal["global_preset", "user_private"]
PosterStyleCategory = Literal[
    "user_favorites",
    "illustration",
    "3d",
    "minimal",
    "retro",
    "traditional",
    "photo",
    "other",
]
# list / filter 用：含 "all"
PosterStyleCategoryFilter = Literal[
    "all",
    "user_favorites",
    "illustration",
    "3d",
    "minimal",
    "retro",
    "traditional",
    "photo",
    "other",
]
# 用户生成样图允许的张数档位。与 POSTER_STYLE_GENERATE_MAX_COUNT 对齐。
PosterStyleGenerateCount = Literal[1, 2, 3, 4]


class PosterStyleSyncOut(BaseModel):
    status: Literal["ok", "failed", "skipped"] = "ok"
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    last_success_at: datetime | None = None
    last_error: str | None = None


class PosterStyleSyncStateOut(BaseModel):
    last_success_at: datetime | None = None
    last_error: str | None = None
    can_sync: bool = False
    github_contents_url: str | None = None


class PosterStyleSampleOut(BaseModel):
    """一条 PosterStyleItem 里的某张样图。索引 0 通常 = cover。"""

    index: int
    image_id: str | None = None
    image_url: str
    display_url: str | None = None
    thumb_url: str | None = None


class PosterStyleItemOut(BaseModel):
    id: str
    source: PosterStyleSource
    visibility_scope: PosterStyleVisibilityScope
    title: str
    category: PosterStyleCategory
    mood: str | None = None
    prompt_template: str | None = None
    palette: list[str] = Field(default_factory=list)
    recommended_aspects: list[str] = Field(default_factory=list)
    style_tags: list[str] = Field(default_factory=list)
    # cover_image_id 与 sample_image_ids[0] 通常相同；user item 走 image API，
    # preset 走 /poster-styles/{id}/binary。
    cover_image_url: str
    display_url: str | None = None
    thumb_url: str | None = None
    cover_image_id: str | None = None
    sample_image_ids: list[str] = Field(default_factory=list)
    samples: list[PosterStyleSampleOut] = Field(default_factory=list)
    preset_id: str | None = None
    version: int | None = None
    library_folder: str | None = None
    download_filename: str | None = None
    auto_tagged_at: datetime | None = None
    auto_tag_notes: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class PosterStyleListOut(BaseModel):
    items: list[PosterStyleItemOut]
    total: int = 0
    limit: int = 50
    offset: int = 0
    has_more: bool = False
    sync: PosterStyleSyncStateOut = Field(default_factory=PosterStyleSyncStateOut)


class PosterStyleCreateIn(BaseModel):
    """用户手动上传一条风格条目（基于已有 Image）。

    cover_image_id 必填，sample_image_ids 可选追加（不含 cover 重复）。
    """

    source: Literal["favorite", "user_upload", "generated"] = "user_upload"
    visibility_scope: Literal["user_private"] = "user_private"
    cover_image_id: str
    sample_image_ids: list[str] = Field(default_factory=list, max_length=8)
    title: str = Field(min_length=1, max_length=120)
    category: PosterStyleCategory = "user_favorites"
    mood: str | None = Field(default=None, max_length=120)
    prompt_template: str | None = Field(default=None, max_length=2000)
    palette: list[str] = Field(default_factory=list, max_length=12)
    recommended_aspects: list[str] = Field(default_factory=list, max_length=8)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    auto_tag: bool = True


class PosterStylePatchIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    category: PosterStyleCategory | None = None
    mood: str | None = Field(default=None, max_length=120)
    prompt_template: str | None = Field(default=None, max_length=2000)
    palette: list[str] | None = Field(default=None, max_length=12)
    recommended_aspects: list[str] | None = Field(default=None, max_length=8)
    style_tags: list[str] | None = Field(default=None, max_length=12)


class PosterStyleBatchDeleteIn(BaseModel):
    item_ids: list[str] = Field(min_length=1, max_length=1000)


class PosterStyleBatchDeleteOut(BaseModel):
    ok: bool = True
    deleted: int = 0
    not_found: list[str] = Field(default_factory=list)


class PosterStyleGenerateIn(BaseModel):
    """让用户输入 prompt（或基于已有风格的 prompt_template）生成 N 张样图入库。

    后端会创建一条隐藏的 WorkflowRun(type="poster_style_library_generate")
    + 一个 step + count 个 worker generation task。
    """

    title: str = Field(min_length=1, max_length=120)
    category: PosterStyleCategory = "user_favorites"
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    # 可选：已有 prompt_template 直接复用（与 prompt 合并）
    prompt_template: str | None = Field(default=None, max_length=2000)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    palette: list[str] = Field(default_factory=list, max_length=12)
    recommended_aspects: list[str] = Field(default_factory=list, max_length=8)
    mood: str | None = Field(default=None, max_length=120)
    aspect_ratio: AspectRatioLiteral = "1:1"
    count: PosterStyleGenerateCount = 2
    auto_tag: bool = True


class PosterStyleGenerateOut(BaseModel):
    """返回新创建的隐藏 workflow_run + 入队 task ids。前端再轮询 GET /poster-styles/jobs。"""

    job_id: str
    workflow_run_id: str
    status: Literal["queued", "running"]
    requested_count: int
    task_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class PosterStyleJobOut(BaseModel):
    """用户"我的生成任务"列表项。聚合：每个 hidden workflow_run 一行。"""

    job_id: str
    workflow_run_id: str
    title: str
    category: PosterStyleCategory
    status: Literal["queued", "running", "succeeded", "failed", "partial"]
    requested_count: int
    finished_count: int
    prompt: str | None = None
    style_tags: list[str] = Field(default_factory=list)
    image_ids: list[str] = Field(default_factory=list)
    saved_item_id: str | None = None  # 已存为 PosterStyleItem 时携带
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class PosterStyleJobsOut(BaseModel):
    items: list[PosterStyleJobOut]
    limit: int = 50
    offset: int = 0
    has_more: bool = False


class PosterStyleAutoTagOut(BaseModel):
    """vision 反推风格元数据返回值。"""

    item_id: str
    style_tags: list[str] = Field(default_factory=list)
    category: PosterStyleCategory | None = None
    mood: str | None = None
    palette: list[str] = Field(default_factory=list)
    notes: str | None = None


__all__ += [
    "normalize_asset_reference_url",
    "PosterStyleSource",
    "PosterStyleVisibilityScope",
    "PosterStyleCategory",
    "PosterStyleCategoryFilter",
    "PosterStyleGenerateCount",
    "PosterStyleSyncOut",
    "PosterStyleSyncStateOut",
    "PosterStyleSampleOut",
    "PosterStyleItemOut",
    "PosterStyleListOut",
    "PosterStyleCreateIn",
    "PosterStylePatchIn",
    "PosterStyleBatchDeleteIn",
    "PosterStyleBatchDeleteOut",
    "PosterStyleGenerateIn",
    "PosterStyleGenerateOut",
    "PosterStyleJobOut",
    "PosterStyleJobsOut",
    "PosterStyleAutoTagOut",
]
