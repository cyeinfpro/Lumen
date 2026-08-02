"""Synchronous durable file operations for Volcano video variants."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from lumen_core import volcano_asset_media as asset_media
from lumen_core.volcano_asset_media import (
    VolcanoAssetMediaError,
    VolcanoAssetVideoMp4,
)


def write_video_stage(path: Path, rendered: VolcanoAssetVideoMp4) -> None:
    if (
        len(rendered.data) != rendered.size_bytes
        or hashlib.sha256(rendered.data).hexdigest() != rendered.sha256
    ):
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "normalized asset video identity is invalid",
            503,
        )
    asset_media._mkdir_parents_durable(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(rendered.data)
            output.flush()
            os.fsync(output.fileno())
        asset_media._fsync_directory(path.parent)
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "normalized asset video could not be staged",
            503,
        ) from exc
    if not asset_media._file_matches(
        path,
        size_bytes=rendered.size_bytes,
        sha256=rendered.sha256,
    ):
        path.unlink(missing_ok=True)
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "staged normalized asset video identity is invalid",
            503,
        )


def install_video_stage_atomic(
    destination: Path,
    staged_path: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> bool:
    if not asset_media._file_matches(
        staged_path,
        size_bytes=size_bytes,
        sha256=sha256,
    ):
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "staged normalized asset video identity is invalid",
            503,
        )
    asset_media._mkdir_parents_durable(destination.parent)
    existed_before = destination.exists()
    if asset_media._file_matches(
        destination,
        size_bytes=size_bytes,
        sha256=sha256,
    ):
        staged_path.unlink(missing_ok=True)
        return False
    try:
        os.replace(staged_path, destination)
        asset_media._fsync_directory(destination.parent)
    except OSError as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "staged normalized asset video could not be stored",
            503,
        ) from exc
    if asset_media._file_matches(
        destination,
        size_bytes=size_bytes,
        sha256=sha256,
    ):
        return not existed_before
    raise VolcanoAssetMediaError(
        "volcano_asset_media_storage_conflict",
        "staged normalized asset video changed while it was being stored",
        503,
    )


__all__ = ["install_video_stage_atomic", "write_video_stage"]
