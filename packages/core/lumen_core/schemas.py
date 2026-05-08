"""Pydantic I/O schemas——DESIGN §5 的请求/响应体。

约定：
- 对 API 与 Worker 都可见（Worker 通过 XADD payload 读取任务元信息）
- 字段保守：除非 DESIGN §5 里写了，否则不增加
- 前端 TypeScript 类型由 OpenAPI 生成（后续接 openapi-typescript）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .constants import (
    MAX_MESSAGE_ATTACHMENTS,
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


class RuntimeDefaultsOut(BaseModel):
    fast: bool = True


class UserOut(BaseOut):
    id: str
    email: str
    display_name: str
    role: str
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
    aspect_ratio: "AspectRatioLiteral" = "1:1"
    size_mode: Literal["auto", "fixed"] = "auto"
    fixed_size: str | None = None
    style_preset_id: str | None = None
    count: int = Field(default=1, ge=1, le=16)
    # Image Fast uses the lighter responses reasoning model for image_generation:
    # gpt-5.4-mini when enabled, gpt-5.4 when disabled.
    fast: bool | None = None
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


class PostMessageIn(BaseModel):
    """DESIGN §5.4 核心写入接口。"""
    idempotency_key: str = Field(min_length=1, max_length=64)
    # 上游 prompt 上限对齐：单条用户输入限制 10k 字符，避免恶意 / 误粘大文本撑爆 DB / 上游。
    text: str = Field(max_length=MAX_PROMPT_CHARS)
    attachment_image_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_MESSAGE_ATTACHMENTS,
    )
    # 局部 inpaint 用 mask（attachment 级别，不进 image_params）。
    # RGBA PNG，alpha=0 处即要重画区域。复用 POST /images/upload 上传后把返回
    # 的 image_id 填到这里；worker 侧用 PIL 自适应 resize 到第一张参考图尺寸。
    mask_image_id: str | None = None
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
    user_api_credential_id: str | None = None
    upstream_supplier_id: str | None = None
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


class CompletionOut(BaseOut):
    id: str
    message_id: str
    user_api_credential_id: str | None = None
    upstream_supplier_id: str | None = None
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


# ---------- Workflows ----------

WorkflowType = Literal["apparel_model_showcase"]
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
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
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

    age_segment: ModelAgeSegment
    gender: Literal["female", "male"] | None = None
    genders: list[Literal["female", "male"]] | None = Field(default=None, max_length=2)
    appearance_direction: str | None = Field(default=None, max_length=80)
    extra_requirements: str | None = Field(default=None, max_length=400)
    style_tags: list[str] = Field(default_factory=list, max_length=12)
    count: ModelLibraryGenerateCount = 4
    # 生成完是否对每张自动 vision 打标签（用户筛选/收藏前预填字段，默认开）。
    auto_tag: bool = True


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
            "side_or_back",
        ],
        min_length=1,
        max_length=4,
    )
    aspect_ratio: AspectRatioLiteral = "4:5"
    final_quality: Literal["standard", "high", "4k"] = "high"
    output_count: Literal[1, 2, 4, 8, 16] = 4
    scene_environment: Literal["indoor", "outdoor"] = "indoor"


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
    traffic_pct: float = 0.0   # 该 provider 占总请求的比例


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


class ByokSettingsPatchIn(BaseModel):
    mode_enabled: bool | None = None
    byok_signup_enabled: bool | None = None
    byok_signup_bypasses_allowlist: bool | None = None
    fallback_to_admin_provider: bool | None = None
    validation_model: str | None = Field(default=None, max_length=64)
    validation_timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    pending_token_ttl_seconds: int | None = Field(default=None, ge=60, le=3600)


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
    purposes: list[ByokPurpose] = Field(default_factory=_default_byok_purposes, min_length=1)
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


__all__ = [
    "SignupIn",
    "LoginIn",
    "RuntimeDefaultsOut",
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
]
