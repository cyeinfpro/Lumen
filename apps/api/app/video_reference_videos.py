"""Video upstream video reference variants."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Video


VIDEO_REFERENCE_VIDEO_KIND = "video_ref_seedance_r2v_mp4"
VIDEO_REFERENCE_VIDEO_MIME = "video/mp4"
VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT = 2_086_876
VIDEO_REFERENCE_VIDEO_MAX_SIDE = 1920
_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.EXDEV,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}


class VideoReferenceVideoError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class VideoReferenceMp4:
    data: bytes
    width: int
    height: int
    duration_ms: int
    fps: float | None
    has_audio: bool
    size_bytes: int
    sha256: str


def _storage_path(storage_root: str, storage_key: str) -> Path:
    root = Path(storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise VideoReferenceVideoError("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise VideoReferenceVideoError(
            "invalid_path", "absolute storage paths are not allowed", 400
        )
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise VideoReferenceVideoError(
            "invalid_path", "storage path escapes root", 400
        ) from exc
    return path


def video_reference_video_key(video: Video) -> str:
    source = Path(video.storage_key)
    return str(source.with_name(f"{video.id}.{VIDEO_REFERENCE_VIDEO_KIND}.mp4"))


def _write_new_file_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                raise
            _write_new_file_exclusive(path, data)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise FileExistsError(str(path)) from exc
        raise
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _write_new_file_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _fps(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _float_or_none(value)
    left, right = value.split("/", 1)
    try:
        denominator = float(right)
        if denominator == 0:
            return None
        return float(left) / denominator
    except (TypeError, ValueError):
        return None


def _fit_even_dimensions(
    width: int,
    height: int,
    *,
    max_pixels: int = VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT,
    max_side: int = VIDEO_REFERENCE_VIDEO_MAX_SIDE,
) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise VideoReferenceVideoError(
            "invalid_video", "reference video has invalid dimensions", 400
        )
    scale = min(
        1.0,
        max_side / max(width, height),
        math.sqrt(max_pixels / (width * height)),
    )
    target_width = max(2, int(width * scale) // 2 * 2)
    target_height = max(2, int(height * scale) // 2 * 2)
    while target_width * target_height > max_pixels:
        if target_width >= target_height and target_width > 2:
            target_width -= 2
        elif target_height > 2:
            target_height -= 2
        else:
            break
    return target_width, target_height


def _probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise VideoReferenceVideoError(
            "invalid_video",
            proc.stderr.decode("utf-8", "replace")[-500:] or "unreadable video",
            400,
        )
    try:
        raw = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise VideoReferenceVideoError(
            "invalid_video", "invalid ffprobe output", 400
        ) from exc
    raw_streams = raw.get("streams") if isinstance(raw, dict) else None
    streams = raw_streams if isinstance(raw_streams, list) else []
    video_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"),
        None,
    )
    if not isinstance(video_stream, dict):
        raise VideoReferenceVideoError(
            "invalid_video", "reference video has no video stream", 400
        )
    audio_stream = next(
        (item for item in streams if isinstance(item, dict) and item.get("codec_type") == "audio"),
        None,
    )
    duration = _float_or_none(video_stream.get("duration")) or _float_or_none(
        (raw.get("format") or {}).get("duration") if isinstance(raw, dict) else None
    )
    return {
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "duration_ms": int(duration * 1000) if duration is not None else 0,
        "fps": _fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")),
        "has_audio": audio_stream is not None,
    }


def make_video_reference_mp4(source_path: Path) -> VideoReferenceMp4:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VideoReferenceVideoError(
            "video_reference_transcoder_missing",
            "ffmpeg and ffprobe are required for uploaded video references",
            503,
        )
    if not source_path.is_file():
        raise VideoReferenceVideoError("not_found", "binary missing", 404)

    source_meta = _probe_video(ffprobe, source_path)
    target_width, target_height = _fit_even_dimensions(
        int(source_meta.get("width") or 0),
        int(source_meta.get("height") or 0),
    )
    with tempfile.TemporaryDirectory(prefix="lumen-video-ref-") as tmp:
        dst = Path(tmp) / "reference.mp4"
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                f"scale={target_width}:{target_height}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(dst),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0 or not dst.is_file():
            raise VideoReferenceVideoError(
                "video_reference_transcode_failed",
                proc.stderr.decode("utf-8", "replace")[-500:]
                or "reference video transcode failed",
                503,
            )
        metadata = _probe_video(ffprobe, dst)
        width = int(metadata.get("width") or 0)
        height = int(metadata.get("height") or 0)
        if width <= 0 or height <= 0:
            raise VideoReferenceVideoError(
                "video_reference_transcode_failed",
                "transcoded reference video has invalid dimensions",
                503,
            )
        if width * height > VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT:
            raise VideoReferenceVideoError(
                "too_many_video_pixels",
                "transcoded reference video exceeds upstream pixel limit",
                503,
            )
        data = dst.read_bytes()
    return VideoReferenceMp4(
        data=data,
        width=width,
        height=height,
        duration_ms=int(metadata.get("duration_ms") or 0),
        fps=metadata.get("fps") if isinstance(metadata.get("fps"), float) else None,
        has_audio=bool(metadata.get("has_audio")),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def video_reference_variant_metadata(video: Video) -> dict[str, Any] | None:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    raw = metadata.get("upstream_reference_video_variant")
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") != VIDEO_REFERENCE_VIDEO_KIND:
        return None
    storage_key = raw.get("storage_key")
    sha256 = raw.get("sha256")
    if not isinstance(storage_key, str) or not storage_key:
        return None
    if not isinstance(sha256, str) or not sha256:
        return None
    return raw


async def ensure_video_reference_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
) -> dict[str, Any]:
    video = (
        await db.execute(
            select(Video)
            .where(
                Video.id == video.id,
                Video.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none() or video

    existing = video_reference_variant_metadata(video)
    if existing is not None and _storage_path(
        storage_root, str(existing["storage_key"])
    ).is_file():
        return existing

    source_path = _storage_path(storage_root, video.storage_key)
    rendered = await asyncio.to_thread(make_video_reference_mp4, source_path)
    key = video_reference_video_key(video)
    destination = _storage_path(storage_root, key)
    try:
        await asyncio.to_thread(_write_new_file_atomic, destination, rendered.data)
    except FileExistsError:
        pass

    variant = {
        "kind": VIDEO_REFERENCE_VIDEO_KIND,
        "storage_key": key,
        "mime": VIDEO_REFERENCE_VIDEO_MIME,
        "width": rendered.width,
        "height": rendered.height,
        "duration_ms": rendered.duration_ms,
        "fps": rendered.fps,
        "has_audio": rendered.has_audio,
        "size_bytes": rendered.size_bytes,
        "sha256": rendered.sha256,
        "pixel_limit": VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT,
        "max_side": VIDEO_REFERENCE_VIDEO_MAX_SIDE,
    }
    metadata = dict(video.metadata_jsonb or {})
    metadata["upstream_reference_video_variant"] = variant
    video.metadata_jsonb = metadata
    return variant
