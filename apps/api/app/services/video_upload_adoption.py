"""Reference-video upload adoption state and recovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from sqlalchemy import select


class VideoUploadAdoption(str, Enum):
    ADOPTED = "adopted"
    NOT_ADOPTED = "not_adopted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VideoUploadAdoptionProbe:
    outcome: VideoUploadAdoption
    video: Any | None = None


async def clear_adoption_marker_best_effort(
    *,
    marker: Any | None,
    deps: Any,
) -> None:
    if marker is None:
        return
    try:
        await deps.storage_lifecycle.clear_upload_adoption_marker(marker)
    except Exception:
        deps.logger.warning(
            "failed to clear reference video adoption marker video_id=%s",
            marker.video_id,
            exc_info=True,
        )


async def probe_reference_video_adoption(
    *,
    video_id: str,
    user_id: str,
    storage_key: str,
    sha256: str,
    size_bytes: int,
    session_factory: Any,
    video_model: Any,
    lifecycle_factory: Callable[[], Any],
    logger: Any,
    adoption_type: Any = VideoUploadAdoption,
    probe_type: Any = VideoUploadAdoptionProbe,
) -> VideoUploadAdoptionProbe:
    try:
        async with session_factory() as recovery:
            row = await recovery.get(video_model, video_id)
            if (
                row is not None
                and row.user_id == user_id
                and row.deleted_at is None
                and row.storage_key == storage_key
                and row.sha256 == sha256
                and int(row.size_bytes or 0) == int(size_bytes)
            ):
                matches = await lifecycle_factory().upload_artifact_matches(
                    video_id=video_id,
                    user_id=user_id,
                    storage_key=storage_key,
                    sha256=sha256,
                    size_bytes=size_bytes,
                )
                if not matches:
                    return probe_type(adoption_type.UNKNOWN)
                return probe_type(adoption_type.ADOPTED, row)
            conflicting = (
                await recovery.execute(
                    select(video_model.id).where(
                        video_model.storage_key == storage_key,
                        video_model.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conflicting is not None:
                return probe_type(adoption_type.UNKNOWN)
    except Exception:
        logger.warning(
            "reference video adoption probe failed video_id=%s storage_key=%s",
            video_id,
            storage_key,
            exc_info=True,
        )
        return probe_type(adoption_type.UNKNOWN)
    return probe_type(adoption_type.NOT_ADOPTED)
