"""Reference-video upload plan integrity checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from ..services.video_storage_lifecycle import VideoStorageLifecycle


class ReferenceInventoryChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class ReferenceUploadPlan:
    created: bool
    video_id: str
    storage_key: str
    path: Path | None


class ReferenceUploadDependencies(Protocol):
    fs_path: Callable[[str], Path]
    storage_lifecycle: VideoStorageLifecycle


async def verify_reusable_reference_upload(
    *,
    plan: ReferenceUploadPlan,
    user_id: str,
    size: int,
    sha256: str,
    deps: ReferenceUploadDependencies,
) -> ReferenceUploadPlan:
    if plan.path is not None:
        return plan
    matches = await deps.storage_lifecycle.upload_artifact_matches(
        video_id=plan.video_id,
        user_id=user_id,
        storage_key=plan.storage_key,
        sha256=sha256,
        size_bytes=size,
    )
    if matches:
        return plan
    return replace(plan, path=deps.fs_path(plan.storage_key))
