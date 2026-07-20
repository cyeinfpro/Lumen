"""Contracts shared by video provider adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from ..video_artifacts import DownloadedVideo

VideoProviderStatus = Literal[
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "expired",
]


@dataclass(frozen=True)
class VideoReferenceMedia:
    kind: Literal["image", "video", "audio"]
    data: bytes | None = None
    mime: str | None = None
    url: str | None = None
    label: str | None = None
    ref_id: str | None = None


@dataclass(frozen=True)
class VideoSubmitRequest:
    task_id: str
    user_id: str
    action: Literal["t2v", "i2v", "reference"]
    model: str
    upstream_model: str
    prompt: str
    duration_s: int
    resolution: str
    aspect_ratio: str
    generate_audio: bool = True
    seed: int | None = None
    watermark: bool = False
    input_image_url: str | None = None
    input_image_bytes: bytes | None = None
    input_image_mime: str | None = None
    reference_media: list[VideoReferenceMedia] = field(default_factory=list)
    callback_url: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class SubmitResult:
    provider_task_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class PollResult:
    status: VideoProviderStatus
    progress: int | None = None
    video_url: str | None = None
    failure_class: str | None = None
    usage_total_tokens: int | None = None
    upstream_billable: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CancelResult:
    accepted: bool
    raw: dict[str, Any] = field(default_factory=dict)


class VideoUpstreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "upstream_unknown",
        status_code: int | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.status_code = status_code
        self.raw = raw or {}
        super().__init__(message)


class VideoProviderAdapter(Protocol):
    async def submit(self, req: VideoSubmitRequest) -> SubmitResult: ...

    async def poll(self, provider_task_id: str) -> PollResult: ...

    async def download_result(
        self,
        video_url: str,
        *,
        ensure_active: Callable[[], None] | None = None,
    ) -> DownloadedVideo: ...

    async def fetch_result(self, video_url: str) -> bytes: ...

    async def cancel(self, provider_task_id: str) -> CancelResult | None: ...
