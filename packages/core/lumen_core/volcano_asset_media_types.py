"""Shared Volcano asset media constants, errors, and value objects."""

from __future__ import annotations

from dataclasses import dataclass

VOLCANO_ASSET_IMAGE_KIND = "volcano_asset_img_v1"
VOLCANO_ASSET_IMAGE_MIME = "image/jpeg"
VOLCANO_ASSET_VIDEO_KIND = "volcano_asset_video_v1"
VOLCANO_ASSET_VIDEO_MIME = "video/mp4"
VOLCANO_ASSET_VIDEO_METADATA_KEY = "volcano_asset_video_variant"

VOLCANO_ASSET_MIN_SIDE = 300
VOLCANO_ASSET_MIN_ASPECT_RATIO = 0.4
VOLCANO_ASSET_MAX_ASPECT_RATIO = 2.5
VOLCANO_ASSET_IMAGE_MAX_SIDE = 2048
VOLCANO_ASSET_IMAGE_MAX_BYTES = 30 * 1024 * 1024
VOLCANO_ASSET_SOURCE_MAX_PIXELS = 64_000_000

VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE = 1280
VOLCANO_ASSET_VIDEO_MAX_SIDE = 1920
VOLCANO_ASSET_VIDEO_MIN_PIXELS = 409_600
VOLCANO_ASSET_VIDEO_MAX_PIXELS = 2_086_876
VOLCANO_ASSET_VIDEO_FPS = 30.0
VOLCANO_ASSET_VIDEO_MIN_FPS = 24.0
VOLCANO_ASSET_VIDEO_MAX_FPS = 60.0
VOLCANO_ASSET_VIDEO_MIN_DURATION_MS = 2_000
VOLCANO_ASSET_VIDEO_MAX_DURATION_MS = 15_000
VOLCANO_ASSET_VIDEO_MAX_BYTES = 50 * 1024 * 1024
VOLCANO_ASSET_VIDEO_POSTER_MAX_BYTES = 5 * 1024 * 1024
VOLCANO_ASSET_VIDEO_POSTER_MAX_SIDE = 640
VOLCANO_ASSET_VIDEO_POSTER_MIME = "image/jpeg"

VOLCANO_ASSET_NEUTRAL_RGB = (245, 245, 245)
VOLCANO_ASSET_JPEG_QUALITIES = (90, 82, 74, 66, 58, 50, 42, 34)
VOLCANO_ASSET_VIDEO_PROFILES = (
    {"crf": "22", "maxrate": "8M", "bufsize": "16M"},
    {"crf": "26", "maxrate": "5M", "bufsize": "10M"},
    {"crf": "30", "maxrate": "3M", "bufsize": "6M"},
)


class VolcanoAssetMediaError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class VolcanoAssetImageJpeg:
    data: bytes
    width: int
    height: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class VolcanoAssetVideoMp4:
    data: bytes
    width: int
    height: int
    duration_ms: int
    fps: float
    has_audio: bool
    size_bytes: int
    sha256: str
    poster_bytes: bytes | None = None


@dataclass(frozen=True)
class VolcanoAssetInstallReceipt:
    storage_key: str
    size_bytes: int
    sha256: str
