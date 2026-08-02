"""Runtime contract and errors for Volcano asset variant workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .volcano_asset_media_types import VolcanoAssetMediaError


@dataclass(frozen=True, slots=True)
class VariantWorkflowRuntime:
    cleanup_install_best_effort: Callable[..., Awaitable[None]]
    cleanup_timeout_seconds: float
    rollback_timeout_seconds: float
    prepare_timeout_seconds: float
    finalize_timeout_seconds: float
    convergence_attempts: int
    reference_scan_limit: int
    video_reference_storage_quota_bytes: int
    storage_path: Callable[..., Path]
    image_variant_file_is_valid: Callable[..., bool]
    make_image_jpeg: Callable[..., Any]
    reserve_media_capacity: Callable[..., Awaitable[Any]]
    install_rendered_media: Callable[..., Awaitable[Any]]
    video_variant_metadata: Callable[..., dict[str, Any] | None]
    video_variant_file_is_valid: Callable[..., bool]
    video_transcode_semaphore: Callable[[], Any]
    make_video_mp4: Callable[..., Any]
    result_rows: Callable[[Any], list[Any]]
    video_reference_declared_bytes: Callable[[Any], int]
    video_variant_quota_bytes: Callable[[Any, str], int]


def database_timeout() -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        "volcano_asset_media_database_timeout",
        "normalized asset media database convergence timed out",
        503,
    )


def prepare_timeout() -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        "volcano_asset_media_prepare_timeout",
        "normalized asset media preparation timed out",
        503,
    )


def source_changed(asset_type: str) -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        f"{asset_type}_reference_changed",
        f"{asset_type} changed or was deleted during asset preparation",
        409,
    )


def quota_exceeded() -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        "reference_video_quota_exceeded",
        "reference video storage quota exceeded",
        429,
    )


__all__ = [
    "VariantWorkflowRuntime",
    "database_timeout",
    "prepare_timeout",
    "quota_exceeded",
    "source_changed",
]
