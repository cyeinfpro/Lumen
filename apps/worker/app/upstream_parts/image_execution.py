"""Shared request contracts for image dispatch, failover, and races."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .transport import ImageProgressCallback

ImageResult = tuple[str, str | None]


@dataclass(frozen=True)
class ImageExecutionRequest:
    action: str
    prompt: str
    size: str
    images: list[bytes] | None
    mask: bytes | None
    n: int
    quality: str
    output_format: str | None
    output_compression: int | None
    background: str | None
    moderation: str | None
    model: str | None
    progress_callback: ImageProgressCallback | None
    provider_override: Any | None
    user_id: str | None

    def with_progress(
        self,
        progress_callback: ImageProgressCallback | None,
    ) -> ImageExecutionRequest:
        return replace(self, progress_callback=progress_callback)

    def with_mask(self, mask: bytes | None) -> ImageExecutionRequest:
        return replace(self, mask=mask)

    def action_kwargs(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "prompt": self.prompt,
            "size": self.size,
            "images": self.images,
            "mask": self.mask,
            "n": self.n,
            "quality": self.quality,
            "output_format": self.output_format,
            "output_compression": self.output_compression,
            "background": self.background,
            "moderation": self.moderation,
            "model": self.model,
            "progress_callback": self.progress_callback,
            "provider_override": self.provider_override,
            "user_id": self.user_id,
        }

    def job_run_kwargs(self) -> dict[str, Any]:
        kwargs = self.action_kwargs()
        kwargs.pop("provider_override")
        return kwargs

    def responses_kwargs(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "size": self.size,
            "action": self.action,
            "images": self.images,
            "quality": self.quality,
            "output_format": self.output_format,
            "output_compression": self.output_compression,
            "background": self.background,
            "moderation": self.moderation,
            "model": self.model,
            "progress_callback": self.progress_callback,
            "provider_override": self.provider_override,
            "user_id": self.user_id,
        }

    def direct_edit_kwargs(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "size": self.size,
            "images": self.images,
            "mask": self.mask,
            "n": self.n,
            "quality": self.quality,
            "output_format": self.output_format,
            "output_compression": self.output_compression,
            "background": self.background,
            "moderation": self.moderation,
            "progress_callback": self.progress_callback,
            "provider_override": self.provider_override,
        }

    def direct_generate_kwargs(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "size": self.size,
            "n": self.n,
            "quality": self.quality,
            "output_format": self.output_format,
            "output_compression": self.output_compression,
            "background": self.background,
            "moderation": self.moderation,
            "progress_callback": self.progress_callback,
            "provider_override": self.provider_override,
        }


@dataclass(frozen=True)
class ImageProviderRoute:
    channel: str
    engine: str
    use_jobs: bool
    provider_name: str


__all__ = [
    "ImageExecutionRequest",
    "ImageProviderRoute",
    "ImageResult",
]
