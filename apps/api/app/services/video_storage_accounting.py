"""Video artifact accounting and cleanup metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


VIDEO_STORAGE_CLEANUP_METADATA_KEY = "video_storage_cleanup"
VIDEO_VARIANT_METADATA_KEYS = (
    "upstream_reference_video_variant",
    "volcano_asset_video_variant",
)
VIDEO_STORAGE_MAX_ISSUES = 20

# Compatibility aliases for older same-domain callers. New cross-module code
# should use the public contract above.
_VIDEO_VARIANT_METADATA_KEYS = VIDEO_VARIANT_METADATA_KEYS
_MAX_ISSUES = VIDEO_STORAGE_MAX_ISSUES


@dataclass(frozen=True)
class VideoArtifactInspection:
    artifact_count: int
    bytes_on_disk: int
    primary_present: bool
    primary_size_bytes: int
    issues: tuple[str, ...] = ()

    @property
    def retained(self) -> bool:
        return self.artifact_count > 0 or bool(self.issues)


@dataclass(frozen=True)
class VideoArtifactCleanupResult:
    complete: bool
    deleted_artifacts: int
    remaining: VideoArtifactInspection
    errors: tuple[str, ...] = ()


def storage_key_parts(storage_key: str) -> tuple[str, ...] | None:
    if (
        not storage_key
        or "\x00" in storage_key
        or "\\" in storage_key
        or storage_key.startswith("/")
    ):
        return None
    parts = tuple(storage_key.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def storage_key_starts_with(
    parts: tuple[str, ...],
    prefix: tuple[str, ...],
) -> bool:
    return len(parts) > len(prefix) and parts[: len(prefix)] == prefix


# Compatibility aliases for older same-domain callers.
_storage_key_parts = storage_key_parts
_starts_with = storage_key_starts_with


def video_reference_quota_contribution(
    video: Any,
    inspection: VideoArtifactInspection,
) -> tuple[int, int]:
    declared_size = max(0, int(getattr(video, "size_bytes", 0) or 0))
    if getattr(video, "deleted_at", None) is None:
        missing_primary_bytes = max(
            0,
            declared_size - inspection.primary_size_bytes,
        )
        accounted_bytes = inspection.bytes_on_disk + missing_primary_bytes
        if inspection.issues:
            accounted_bytes = max(accounted_bytes, declared_size)
        return 1, accounted_bytes
    metadata = (
        getattr(video, "metadata_jsonb", None)
        if isinstance(getattr(video, "metadata_jsonb", None), dict)
        else {}
    )
    cleanup = metadata.get(VIDEO_STORAGE_CLEANUP_METADATA_KEY)
    cleanup_remaining_count = (
        max(0, int(cleanup.get("remaining_artifact_count") or 0))
        if isinstance(cleanup, dict)
        else 0
    )
    cleanup_remaining_bytes = (
        max(0, int(cleanup.get("remaining_bytes") or 0))
        if isinstance(cleanup, dict)
        else 0
    )
    if not inspection.retained and cleanup_remaining_count <= 0:
        return 0, 0
    accounted_bytes = max(inspection.bytes_on_disk, cleanup_remaining_bytes)
    if inspection.issues:
        accounted_bytes = max(accounted_bytes, declared_size)
    return 1, accounted_bytes


def _video_cleanup_is_complete(video: Any) -> bool:
    metadata = (
        getattr(video, "metadata_jsonb", None)
        if isinstance(getattr(video, "metadata_jsonb", None), dict)
        else {}
    )
    cleanup = metadata.get(VIDEO_STORAGE_CLEANUP_METADATA_KEY)
    return (
        getattr(video, "deleted_at", None) is not None
        and isinstance(cleanup, dict)
        and cleanup.get("state") == "complete"
    )


def video_reference_variant_quota_bytes(
    video: Any,
    metadata_key: str,
) -> int:
    if _video_cleanup_is_complete(video):
        return 0
    metadata = (
        getattr(video, "metadata_jsonb", None)
        if isinstance(getattr(video, "metadata_jsonb", None), dict)
        else {}
    )
    raw = metadata.get(metadata_key)
    if not isinstance(raw, dict):
        return 0
    try:
        return max(0, int(raw.get("size_bytes") or 0)) + max(
            0,
            int(raw.get("poster_size_bytes") or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def video_reference_derived_variant_bytes(video: Any) -> int:
    return sum(
        video_reference_variant_quota_bytes(video, metadata_key)
        for metadata_key in VIDEO_VARIANT_METADATA_KEYS
    )


def video_reference_declared_quota_contribution(video: Any) -> tuple[int, int]:
    if _video_cleanup_is_complete(video):
        return 0, 0
    video_id = str(getattr(video, "id", "") or "")
    user_id = str(getattr(video, "user_id", "") or "")
    storage_key = str(getattr(video, "storage_key", "") or "")
    parts = storage_key_parts(storage_key)
    reference_prefix = ("u", user_id, "vref", video_id)
    primary_is_reference = (
        bool(video_id)
        and bool(user_id)
        and parts is not None
        and storage_key_starts_with(parts, reference_prefix)
    )
    primary_count = 1 if primary_is_reference else 0
    primary_bytes = (
        max(0, int(getattr(video, "size_bytes", 0) or 0)) if primary_is_reference else 0
    )
    return (
        primary_count,
        primary_bytes + video_reference_derived_variant_bytes(video),
    )


def clear_video_storage_cleanup_state(video: Any) -> None:
    metadata = dict(getattr(video, "metadata_jsonb", None) or {})
    metadata.pop(VIDEO_STORAGE_CLEANUP_METADATA_KEY, None)
    video.metadata_jsonb = metadata


def record_video_storage_cleanup(
    video: Any,
    result: VideoArtifactCleanupResult,
) -> None:
    metadata = dict(getattr(video, "metadata_jsonb", None) or {})
    attempted_at = datetime.now(timezone.utc).isoformat()
    if result.complete:
        compacted: dict[str, Any] = {}
        source = metadata.get("source")
        if isinstance(source, str) and source:
            compacted["source"] = source[:80]
        compacted[VIDEO_STORAGE_CLEANUP_METADATA_KEY] = {
            "state": "complete",
            "attempted_at": attempted_at,
        }
        video.poster_storage_key = None
        video.metadata_jsonb = compacted
        return
    cleanup: dict[str, Any] = {
        "state": "pending",
        "attempted_at": attempted_at,
        "remaining_artifact_count": result.remaining.artifact_count,
        "remaining_bytes": result.remaining.bytes_on_disk,
    }
    errors = list(dict.fromkeys((*result.errors, *result.remaining.issues)))
    if errors:
        cleanup["errors"] = errors[:VIDEO_STORAGE_MAX_ISSUES]
    metadata[VIDEO_STORAGE_CLEANUP_METADATA_KEY] = cleanup
    video.metadata_jsonb = metadata


__all__ = (
    "VIDEO_STORAGE_CLEANUP_METADATA_KEY",
    "VIDEO_STORAGE_MAX_ISSUES",
    "VIDEO_VARIANT_METADATA_KEYS",
    "VideoArtifactCleanupResult",
    "VideoArtifactInspection",
    "clear_video_storage_cleanup_state",
    "record_video_storage_cleanup",
    "storage_key_parts",
    "storage_key_starts_with",
    "video_reference_declared_quota_contribution",
    "video_reference_derived_variant_bytes",
    "video_reference_quota_contribution",
    "video_reference_variant_quota_bytes",
)
