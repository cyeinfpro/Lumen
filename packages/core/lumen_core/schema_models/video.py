"""Video Pydantic contracts."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..billing_schemas import MoneyOut
from ..constants import MAX_PROMPT_CHARS
from ..url_security import is_private_host
from ..video_providers import (
    seedance_20_allowed_resolutions,
    seedance_20_duration_is_valid,
)
from ..video_schema_validation import (
    validate_video_create,
    validate_video_reference_media,
)
from .common import BaseOut, normalize_asset_reference_url

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


class VideoUploadOut(VideoOut):
    created: bool


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
_VIDEO_REFERENCE_ANCHOR_CANDIDATE_RE = re.compile(
    r"\[\s*(ref:[^\]\r\n]*)\s*\]",
    re.IGNORECASE,
)


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
        return validate_video_reference_media(
            self,
            reference_id_re=_VIDEO_REFERENCE_ID_RE,
            normalize_asset_url=normalize_asset_reference_url,
            private_host=is_private_host,
        )


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
        return validate_video_create(
            self,
            reference_id_re=_VIDEO_REFERENCE_ID_RE,
            anchor_candidate_re=_VIDEO_REFERENCE_ANCHOR_CANDIDATE_RE,
            allowed_resolutions=seedance_20_allowed_resolutions,
            duration_is_valid=seedance_20_duration_is_valid,
        )


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
    reference_media_limits: dict[Literal["image", "video", "audio"], int] = Field(
        default_factory=dict
    )


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
    submission_epoch: int = 0
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
    submit_started_at: datetime | None = None
    submitted_at: datetime | None = None
    finished_at: datetime | None = None


class VideoGenerationsOut(BaseModel):
    items: list[VideoGenerationOut]
    next_cursor: str | None = None


__all__ = [
    "ImageOut",
    "VideoOut",
    "VideoUploadOut",
    "VideoTemporaryDownloadOut",
    "VideoAction",
    "VideoPricingVariant",
    "VideoResolution",
    "VideoAspectRatio",
    "VideoReferenceMediaIn",
    "VideoReferenceMediaOut",
    "VideoCreateIn",
    "VideoPriceOptionOut",
    "VideoModelOptionOut",
    "VideoOptionsOut",
    "VideoGenerationOut",
    "VideoGenerationsOut",
]
