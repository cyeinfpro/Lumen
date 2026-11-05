"""Strict image generation settings shared by Canvas node kinds."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .image_models import (
    DEFAULT_IMAGE_MODEL, ImageModel, LegacyImageModel, ImageRenderQuality,
    validate_image_model_quality,
)
from .svg_options import SvgOptions
from .image_resource_limits import MAX_GIF_REQUEST_FRAMES, validate_gif_request_budget


class ImageGenerateNodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Saved graphs keep their original model; execution upgrades retired models.
    model: ImageModel | LegacyImageModel | None = None
    aspect_ratio: str = Field(default="1:1", min_length=1, max_length=16)
    size: Literal["1K", "2K", "4K", "1k", "2k", "4k"] = "1K"
    quality: Literal["standard", "high", "1k", "2k", "4k"] = "standard"
    size_mode: Literal["auto", "fixed"] = "auto"
    fixed_size: str | None = Field(default=None, max_length=32)
    render_quality: ImageRenderQuality = "high"
    count: int = Field(default=1, ge=1, le=10)
    output_format: Literal["png", "jpeg", "webp", "svg", "gif"] | None = "webp"
    complexity: Literal["low", "medium", "high"] | None = None
    svg: SvgOptions | None = None
    frame_count: int | None = Field(default=None, ge=2, le=MAX_GIF_REQUEST_FRAMES)
    frame_duration_ms: int | None = Field(default=None, ge=20, le=2000, multiple_of=10)
    output_compression: int | None = Field(default=None, ge=0, le=100)
    background: Literal["auto", "opaque", "transparent"] = "auto"
    moderation: Literal["auto", "low"] = "low"

    @model_validator(mode="before")
    @classmethod
    def drop_legacy_fast(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "fast" not in value:
            return value
        normalized = dict(value)
        normalized.pop("fast", None)
        return normalized

    @model_validator(mode="after")
    def normalize_transparent_output(self) -> "ImageGenerateNodeConfig":
        validate_image_model_quality(
            self.model or DEFAULT_IMAGE_MODEL, self.render_quality, allow_legacy=True
        )
        if self.output_format in {"svg", "gif"}:
            if self.count != 1:
                raise ValueError("SVG/GIF output requires count=1")
            self.output_compression = None
        if self.svg is not None and self.output_format != "svg":
            raise ValueError("SVG author options require SVG output")
        if self.output_format == "gif":
            validate_gif_request_budget(self.fixed_size if self.size_mode == "fixed" else "auto", self.frame_count)
        if self.output_format != "gif" and (
            self.frame_count is not None or self.frame_duration_ms is not None
        ):
            raise ValueError("Animation parameters require GIF output")
        if self.background == "transparent" and self.output_format == "jpeg":
            self.output_format = "png"
            self.output_compression = None
        return self
