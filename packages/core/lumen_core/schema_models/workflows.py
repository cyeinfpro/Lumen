"""Workflows Pydantic contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..constants import MAX_PROMPT_CHARS
from ..sizing import AspectRatio as AspectRatioLiteral
from .common import BaseOut
from .messaging import GenerationOut
from .video import ImageOut

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


ShowcaseShot = Literal[
    "front_full_body",
    "natural_pose",
    "detail_half_body",
    "side_or_back",
]


def _default_showcase_shot_plan() -> list[ShowcaseShot]:
    return [
        "front_full_body",
        "natural_pose",
        "detail_half_body",
    ]


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
    shot_plan: list[ShowcaseShot] = Field(
        default_factory=_default_showcase_shot_plan,
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


__all__ = [
    "WorkflowType",
    "WorkflowRunStatus",
    "WorkflowStepStatus",
    "WorkflowStepKey",
    "WorkflowStepOut",
    "ModelCandidateOut",
    "QualityReportOut",
    "PosterMasterOut",
    "PosterRenderOut",
    "WorkflowRunOut",
    "WorkflowRunListItemOut",
    "WorkflowRunListOut",
    "WorkflowRunPatchIn",
    "ApparelWorkflowCreateIn",
    "ApparelWorkflowCreateOut",
    "ProductAnalysisApproveIn",
    "AccessoryPlanIn",
    "ModelCandidatesCreateIn",
    "ModelCandidateApproveIn",
    "AccessoryPreviewCreateIn",
    "AccessorySelectionIn",
    "AgeSegment",
    "ModelAgeSegment",
    "ModelLibrarySource",
    "ModelLibraryVisibilityScope",
    "ModelLibraryGenerateCount",
    "ApparelModelLibrarySyncOut",
    "ApparelModelLibrarySyncStateOut",
    "ApparelModelLibraryItemOut",
    "ApparelModelLibraryListOut",
    "ApparelModelLibraryItemCreateIn",
    "ApparelModelLibraryItemPatchIn",
    "ApparelModelLibraryBatchDeleteIn",
    "ApparelModelLibraryBatchDeleteOut",
    "ApparelModelLibrarySelectIn",
    "ApparelModelLibraryGenerateIn",
    "ApparelModelLibraryJobItemOut",
    "ApparelModelLibraryJobOut",
    "ApparelModelLibraryJobsOut",
    "ApparelModelLibraryJobsClearOut",
    "ApparelModelLibrarySaveJobItemIn",
    "ApparelModelLibraryAutoTagOut",
    "ModelCandidateSaveToLibraryIn",
    "ShowcaseShot",
    "ShowcaseImagesCreateIn",
    "ImageRevisionIn",
]
