"""Pydantic schemas for Volcano video asset management."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class VideoAssetQuotaLimitsOut(BaseModel):
    max_assets: int = 50
    max_asset_groups: int = 50
    create_asset_qpm: int = 3
    create_asset_window_seconds: int = 60


class VideoAssetQuotaUsageOut(BaseModel):
    assets_used: int = Field(ge=0)
    asset_groups_used: int = Field(ge=0)


class VideoAssetCapabilitiesOut(BaseModel):
    enabled: bool
    reason: str | None = None
    provider_name: str | None = None
    project_name: str | None = None
    region: str | None = None
    public_base_url: str | None = None
    quotas: VideoAssetQuotaLimitsOut = Field(default_factory=VideoAssetQuotaLimitsOut)


class VideoAssetGroupOut(BaseModel):
    id: str
    name: str
    title: str
    description: str = ""
    group_type: str = "AIGC"
    project_name: str
    create_time: str | None = None
    update_time: str | None = None


class VideoAssetGroupListOut(BaseModel):
    items: list[VideoAssetGroupOut] = Field(default_factory=list)
    total_count: int = 0
    page_number: int = 1
    page_size: int = 20


class VideoAssetGroupCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=300)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value


class VideoAssetGroupUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def validate_update(self) -> "VideoAssetGroupUpdateIn":
        if self.name is None and self.description is None:
            raise ValueError("name or description is required")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name must not be empty")
        return self


class VideoAssetOut(BaseModel):
    id: str
    group_id: str
    name: str = ""
    asset_type: str
    status: str = ""
    url: str | None = None
    preview_url: str | None = None
    project_name: str
    create_time: str | None = None
    update_time: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class VideoAssetOperationErrorOut(BaseModel):
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: int | None = None


VideoAssetOperationAction = Literal[
    "create_group",
    "update_group",
    "delete_group",
    "create_asset",
    "update_asset",
    "delete_asset",
]


class VideoAssetDeleteResultOut(BaseModel):
    id: str
    deleted: Literal[True]
    resource_type: Literal["group", "asset"] | None = None
    group_id: str | None = None
    asset_id: str | None = None
    deleted_asset_ids: list[str] = Field(default_factory=list)
    already_deleted: bool = False
    cascade_assets: bool | None = None


VideoAssetOperationResultOut = (
    VideoAssetGroupOut | VideoAssetOut | VideoAssetDeleteResultOut
)


class VideoAssetOperationOut(BaseModel):
    id: str
    action: VideoAssetOperationAction
    status: str
    progress_stage: str
    attempt: int = 1
    delivery_generation: int = 0
    retryable: bool = False
    retry_after_seconds: int | None = None
    result: VideoAssetOperationResultOut | None = None
    error: VideoAssetOperationErrorOut | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None

    @model_validator(mode="after")
    def validate_result_for_action(self) -> "VideoAssetOperationOut":
        if self.result is None:
            return self
        expected_type: type[BaseModel]
        if self.action in {"create_group", "update_group"}:
            expected_type = VideoAssetGroupOut
        elif self.action in {"create_asset", "update_asset"}:
            expected_type = VideoAssetOut
        else:
            expected_type = VideoAssetDeleteResultOut
        if not isinstance(self.result, expected_type):
            raise ValueError(f"result does not match action={self.action}")
        if isinstance(self.result, VideoAssetDeleteResultOut):
            expected_resource_type = (
                "group" if self.action == "delete_group" else "asset"
            )
            if (
                self.result.resource_type is not None
                and self.result.resource_type != expected_resource_type
            ):
                raise ValueError(f"result does not match action={self.action}")
        return self


class VideoAssetCreateAcceptedOut(VideoAssetOut):
    operation_id: str
    operation_status: str
    progress_stage: str
    retryable: bool = False
    retry_after_seconds: int | None = None


class VideoAssetListOut(BaseModel):
    items: list[VideoAssetOut] = Field(default_factory=list)
    total_count: int = 0
    page_number: int = 1
    page_size: int = 20


class VideoAssetCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=128)
    name: str = Field(default="", max_length=64)
    image_id: str | None = Field(default=None, max_length=36)
    video_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def validate_source(self) -> "VideoAssetCreateIn":
        self.group_id = self.group_id.strip()
        self.name = self.name.strip()
        if not self.group_id:
            raise ValueError("group_id must not be empty")
        if (self.image_id is None) == (self.video_id is None):
            raise ValueError("exactly one of image_id or video_id is required")
        if self.image_id is not None:
            self.image_id = self.image_id.strip()
            if not self.image_id:
                raise ValueError("image_id must not be empty")
        if self.video_id is not None:
            self.video_id = self.video_id.strip()
            if not self.video_id:
                raise ValueError("video_id must not be empty")
        return self


class VideoAssetUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value


__all__ = [
    "VideoAssetCapabilitiesOut",
    "VideoAssetCreateAcceptedOut",
    "VideoAssetCreateIn",
    "VideoAssetDeleteResultOut",
    "VideoAssetGroupCreateIn",
    "VideoAssetGroupListOut",
    "VideoAssetGroupOut",
    "VideoAssetGroupUpdateIn",
    "VideoAssetListOut",
    "VideoAssetOperationAction",
    "VideoAssetOperationErrorOut",
    "VideoAssetOperationOut",
    "VideoAssetOperationResultOut",
    "VideoAssetOut",
    "VideoAssetQuotaLimitsOut",
    "VideoAssetQuotaUsageOut",
    "VideoAssetUpdateIn",
]
