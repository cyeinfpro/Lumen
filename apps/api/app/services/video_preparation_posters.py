"""Bounded, owner-scoped poster artifacts for durable video preparation.

The committed slot accounts for the maximum bytes before any file is installed.
Its deterministic identity survives process death between file and DB commits.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from ..media_process import run_media_process

from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from lumen_core.volcano_asset_media_types import (
    VOLCANO_ASSET_VIDEO_POSTER_MAX_BYTES as POSTER_MAX_BYTES,
    VOLCANO_ASSET_VIDEO_POSTER_MAX_SIDE as POSTER_MAX_SIDE,
)

from ..video_reference_videos import (
    VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES,
    VideoReferenceVideoError,
    reference_video_file_matches,
)
from .video_storage_accounting import storage_key_parts
from .video_file_durability import fsync_directory

POSTER_KEY = "canvas_preparation_poster"
POSTER_TIMEOUT_SECONDS = 60
POSTER_FORMAT_REVISION = 1


def owned_path(
    storage_root: str, storage_key: str, *, user_id: str, video_id: str
) -> Path:
    parts = storage_key_parts(storage_key)
    if not parts or len(parts) != 5 or parts[:4] != ("u", user_id, "vref", video_id):
        raise VideoReferenceVideoError(
            "invalid_path", "unowned reference storage path", 409
        )
    root = Path(storage_root).resolve()
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise VideoReferenceVideoError("invalid_path", "symlink storage path", 409)
    if not current.parent.is_dir():
        raise VideoReferenceVideoError(
            "video_original_changed", "original directory missing", 409
        )
    return current


def poster_slot(claim: Any) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{64}", claim.source_sha256):
        raise VideoReferenceVideoError("invalid_video", "invalid original hash", 409)
    filename = (
        f"poster.{claim.source_sha256}.r{POSTER_FORMAT_REVISION}."
        f"p{claim.artifact_revision}.jpg"
    )
    return {
        "kind": POSTER_KEY,
        "storage_key": str(Path(claim.storage_key).with_name(filename)),
        "source_sha256": claim.source_sha256,
        "preparation_revision": claim.artifact_revision,
        "format_revision": POSTER_FORMAT_REVISION,
        "mime": "image/jpeg",
        "size_bytes": POSTER_MAX_BYTES,
        "state": "reserved",
    }


def validate_poster(data: bytes) -> dict[str, Any]:
    try:
        if not data or len(data) > POSTER_MAX_BYTES:
            raise ValueError("invalid poster size")
        with PILImage.open(io.BytesIO(data)) as poster:
            if (
                poster.format != "JPEG"
                or min(poster.size) <= 0
                or max(poster.size) > POSTER_MAX_SIDE
            ):
                raise ValueError("invalid poster dimensions")
            poster.load()
            width, height = poster.size
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        PILImage.DecompressionBombError,
    ) as exc:
        raise VideoReferenceVideoError(
            "video_poster_failed",
            "prepared poster is invalid",
            503,
        ) from exc
    return {
        "width": width,
        "height": height,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def read_poster(path: Path) -> bytes | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise VideoReferenceVideoError("invalid_path", "invalid poster file", 409)
    with path.open("rb") as source:
        data = source.read(POSTER_MAX_BYTES + 1)
    validate_poster(data)
    return data


def verified_original(claim: Any, storage_root: str) -> Path:
    path = owned_path(
        storage_root, claim.storage_key, user_id=claim.user_id, video_id=claim.video_id
    )
    if not reference_video_file_matches(
        path, size_bytes=claim.size_bytes, sha256=claim.source_sha256
    ):
        raise VideoReferenceVideoError(
            "video_original_changed",
            "video original changed or is missing",
            409,
        )
    return path


def render_preparation_poster(
    claim: Any, slot: dict[str, Any], *, storage_root: str, cancel_event=None
) -> bytes:
    source = verified_original(claim, storage_root)
    destination = owned_path(
        storage_root,
        slot["storage_key"],
        user_id=claim.user_id,
        video_id=claim.video_id,
    )
    existing = read_poster(destination)
    if existing is not None:
        if hashlib.sha256(existing).hexdigest() != slot.get("sha256") or len(
            existing
        ) != slot.get("rendered_size_bytes"):
            raise VideoReferenceVideoError(
                "video_poster_changed", "prepared poster identity changed", 409
            )
        return existing
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VideoReferenceVideoError(
            "video_poster_unavailable",
            "ffmpeg is required for video preparation",
            503,
        )
    with tempfile.TemporaryDirectory(prefix=".poster-", dir=source.parent) as directory:
        staged = Path(directory) / "poster.jpg"
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-xerror",
            "-max_alloc",
            str(VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES),
            "-threads",
            "1",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            "0",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map_metadata",
            "-1",
            "-an",
            "-sn",
            "-dn",
            "-frames:v",
            "1",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            "-vf",
            f"scale={POSTER_MAX_SIDE}:{POSTER_MAX_SIDE}:force_original_aspect_ratio=decrease",
            "-c:v",
            "mjpeg",
            "-threads",
            "1",
            "-q:v",
            "3",
            "-fs",
            str(POSTER_MAX_BYTES),
            str(staged),
        ]
        try:
            proc = run_media_process(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cancel_event=cancel_event,
                timeout=POSTER_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VideoReferenceVideoError(
                "video_poster_failed",
                "video poster generation failed",
                503,
            ) from exc
        if proc.returncode != 0:
            raise VideoReferenceVideoError(
                "video_poster_failed",
                "video poster generation failed",
                503,
            )
        data = read_poster(staged)
        if data is None:
            raise VideoReferenceVideoError("video_poster_failed", "poster missing", 503)
        verified_original(claim, storage_root)
        return data


def install_preparation_poster(
    claim: Any,
    slot: dict[str, Any],
    data: bytes,
    *,
    storage_root: str,
) -> dict[str, Any]:
    """Atomically install without replacing any existing deterministic artifact."""
    verified_original(claim, storage_root)
    info = validate_poster(data)
    if info["sha256"] != slot.get("sha256") or len(data) != slot.get(
        "rendered_size_bytes"
    ):
        raise VideoReferenceVideoError(
            "video_poster_changed", "poster manifest mismatch", 409
        )
    destination = owned_path(
        storage_root,
        slot["storage_key"],
        user_id=claim.user_id,
        video_id=claim.video_id,
    )
    existing = read_poster(destination)
    if existing is not None:
        if not reference_video_file_matches(
            destination, size_bytes=len(data), sha256=info["sha256"]
        ):
            raise VideoReferenceVideoError(
                "video_poster_changed", "prepared poster identity changed", 409
            )
        return {**slot, **info, "state": "ready"}
    descriptor, name = tempfile.mkstemp(
        prefix=".poster-install-", dir=destination.parent
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(staged, destination)
        except FileExistsError:
            existing = read_poster(destination)
            if existing is None:
                raise VideoReferenceVideoError(
                    "video_poster_failed", "poster changed", 409
                )
            if hashlib.sha256(existing).hexdigest() != info["sha256"]:
                raise VideoReferenceVideoError(
                    "video_poster_changed", "prepared poster identity changed", 409
                )
        fsync_directory(destination.parent)
    finally:
        staged.unlink(missing_ok=True)
    return {**slot, **info, "state": "ready"}
