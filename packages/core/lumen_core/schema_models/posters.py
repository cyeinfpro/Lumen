"""Posters Pydantic contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..constants import MAX_PROMPT_CHARS
from ..sizing import AspectRatio as AspectRatioLiteral

# ---------- Poster Design Workflow ----------
# 实施决策（2026-05-12）：全 AI 出图（无文字层独立 Canvas），文字直接塞 prompt；
# inpaint 作为返修工具复用 Generation.mask_image_id 字段。schemas 风格 mirror apparel。
PosterAspectRatio = Literal["1:1", "9:16", "16:9", "3:4", "4:3", "2:3", "3:2", "4:5"]
PosterRevisionScope = Literal["background", "inpaint", "style"]


def _default_poster_aspects() -> list[PosterAspectRatio]:
    return ["1:1", "9:16", "16:9", "3:4"]


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
        default_factory=_default_poster_aspects,
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
        default_factory=_default_poster_aspects,
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


__all__ = [
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
