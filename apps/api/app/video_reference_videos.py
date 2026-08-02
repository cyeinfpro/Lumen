"""Video upstream video reference variants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.capacity_leases import CapacityLeaseLost
from lumen_core.models import User, Video
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityUnavailable,
)

from .services.video_storage_capacity import (
    VideoReferenceStorageQuotaExceeded,
    VideoStorageCapacityManager,
    VideoTranscodeCapacityManager,
    VideoTranscodeCapacityUnavailable,
    build_video_storage_capacity_manager,
    build_video_transcode_capacity_manager,
    enforce_video_reference_storage_quota,
)
from .services.video_storage_lifecycle import (
    VIDEO_STORAGE_CLEANUP_METADATA_KEY,
    video_reference_declared_quota_contribution,
    video_reference_variant_quota_bytes,
)


VIDEO_REFERENCE_VIDEO_KIND = "video_ref_seedance_r2v_mp4"
VIDEO_REFERENCE_VIDEO_MIME = "video/mp4"
VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT = 2_086_876
VIDEO_REFERENCE_VIDEO_MAX_SIDE = 1920
VIDEO_REFERENCE_VIDEO_MAX_BYTES = 50 * 1024 * 1024
VIDEO_REFERENCE_VIDEO_TARGET_FPS = 30
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS = 16_777_216
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE = 4096
VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS = 2_000
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS = 15_000
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS = 60.0
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE = 300_000_000.0
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS = 4_000_000_000.0
VIDEO_REFERENCE_VIDEO_FFPROBE_TIMEOUT_SECONDS = 15
VIDEO_REFERENCE_VIDEO_FFMPEG_TIMEOUT_SECONDS = 120
VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES = 256 * 1024 * 1024
_VIDEO_REFERENCE_VIDEO_DURATION_TOLERANCE_MS = 500
_VIDEO_REFERENCE_VIDEO_PROBE_OUTPUT_MAX_BYTES = 64 * 1024
_VIDEO_REFERENCE_VIDEO_HASH_CHUNK_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)


class VideoReferenceVideoError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class VideoReferenceMp4:
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


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_parents_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            _fsync_directory(directory.parent)


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _int_or_zero(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


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
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                (
                    "stream=codec_type,codec_name,width,height,duration,"
                    "avg_frame_rate,r_frame_rate:format=duration,size"
                ),
                "-show_streams",
                "-show_format",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=VIDEO_REFERENCE_VIDEO_FFPROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoReferenceVideoError(
            "invalid_video",
            "reference video inspection timed out",
            422,
        ) from exc
    except OSError as exc:
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "reference video inspection could not start",
            503,
        ) from exc
    if proc.returncode != 0:
        logger.info(
            "reference video ffprobe rejected media stderr=%r",
            proc.stderr.decode("utf-8", "replace")[-500:],
        )
        raise VideoReferenceVideoError(
            "invalid_video",
            "reference video could not be inspected",
            400,
        )
    if len(proc.stdout) > _VIDEO_REFERENCE_VIDEO_PROBE_OUTPUT_MAX_BYTES:
        raise VideoReferenceVideoError(
            "invalid_video",
            "reference video metadata is too large",
            422,
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
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video_stream, dict):
        raise VideoReferenceVideoError(
            "invalid_video", "reference video has no video stream", 400
        )
    audio_stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
        None,
    )
    duration = _float_or_none(video_stream.get("duration")) or _float_or_none(
        (raw.get("format") or {}).get("duration") if isinstance(raw, dict) else None
    )
    audio_codec = (
        str(audio_stream.get("codec_name") or "")
        if isinstance(audio_stream, dict)
        else ""
    )
    return {
        "width": _int_or_zero(video_stream.get("width")),
        "height": _int_or_zero(video_stream.get("height")),
        "duration_ms": int(duration * 1000) if duration is not None else 0,
        "fps": _fps(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        ),
        "has_audio": audio_stream is not None,
        "video_codec": str(video_stream.get("codec_name") or ""),
        "audio_codec": audio_codec,
        "size_bytes": max(
            _int_or_zero(
                (raw.get("format") or {}).get("size") if isinstance(raw, dict) else None
            ),
            path.stat().st_size,
        ),
    }


def _validate_source_video(metadata: dict[str, Any]) -> None:
    width = _int_or_zero(metadata.get("width"))
    height = _int_or_zero(metadata.get("height"))
    pixels = width * height
    if (
        width <= 0
        or height <= 0
        or max(width, height) > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE
        or pixels > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS
    ):
        raise VideoReferenceVideoError(
            "too_many_video_pixels",
            "reference video exceeds the safe source resolution limit",
            413,
        )
    duration_ms = _int_or_zero(metadata.get("duration_ms"))
    if not (
        VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS
        <= duration_ms
        <= VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS
    ):
        raise VideoReferenceVideoError(
            "invalid_video_duration",
            "reference video duration must be between 2 and 15 seconds",
            422,
        )
    fps = _float_or_none(metadata.get("fps"))
    if fps is None or fps <= 0 or fps > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS:
        raise VideoReferenceVideoError(
            "invalid_video_fps",
            "reference video frame rate is invalid or exceeds the safe limit",
            422,
        )
    pixel_rate = pixels * fps
    decoded_pixels = pixel_rate * (duration_ms / 1000)
    if (
        pixel_rate > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE
        or decoded_pixels > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS
    ):
        raise VideoReferenceVideoError(
            "video_decode_budget_exceeded",
            "reference video exceeds the safe decode workload limit",
            413,
        )


def _validate_output_video(
    metadata: dict[str, Any],
    *,
    expected_width: int,
    expected_height: int,
    expected_duration_ms: int,
) -> None:
    width = _int_or_zero(metadata.get("width"))
    height = _int_or_zero(metadata.get("height"))
    if (
        width != expected_width
        or height != expected_height
        or width * height > VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT
        or max(width, height) > VIDEO_REFERENCE_VIDEO_MAX_SIDE
    ):
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video has invalid dimensions",
            503,
        )
    duration_ms = _int_or_zero(metadata.get("duration_ms"))
    if not (
        VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS
        <= duration_ms
        <= (
            VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS
            + _VIDEO_REFERENCE_VIDEO_DURATION_TOLERANCE_MS
        )
    ) or abs(duration_ms - expected_duration_ms) > (
        _VIDEO_REFERENCE_VIDEO_DURATION_TOLERANCE_MS
    ):
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video has invalid duration",
            503,
        )
    fps = _float_or_none(metadata.get("fps"))
    if fps is None or fps <= 0 or fps > VIDEO_REFERENCE_VIDEO_TARGET_FPS + 0.5:
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video has invalid frame rate",
            503,
        )
    if metadata.get("video_codec") != "h264":
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video must use H.264",
            503,
        )
    if metadata.get("has_audio") and metadata.get("audio_codec") != "aac":
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video audio must use AAC",
            503,
        )
    size_bytes = _int_or_zero(metadata.get("size_bytes"))
    if size_bytes <= 0 or size_bytes > VIDEO_REFERENCE_VIDEO_MAX_BYTES:
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video exceeds the output size limit",
            503,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_VIDEO_REFERENCE_VIDEO_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(path: Path, *, size_bytes: int, sha256: str) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == max(0, int(size_bytes))
            and _sha256_file(path) == sha256.lower()
        )
    except OSError:
        return False


def _install_staged_variant(
    staged: Path,
    destination: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> None:
    if not _file_matches(staged, size_bytes=size_bytes, sha256=sha256):
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video identity changed before storage",
            503,
        )
    _mkdir_parents_durable(destination.parent)
    os.replace(staged, destination)
    _fsync_directory(destination.parent)
    if not _file_matches(destination, size_bytes=size_bytes, sha256=sha256):
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "stored reference video identity could not be verified",
            503,
        )


def _discard_orphaned_variant_on_rollback(
    db: AsyncSession,
    destination: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> None:
    """Remove an installed variant if its metadata is not durably committed.

    The variant file reaches its final path before the video metadata that
    references it is committed by the caller's transaction. If that
    transaction rolls back or is closed without commit, the file would
    survive as an orphan: no row references it, so it is neither counted
    against the user's storage quota nor reclaimed by the storage
    lifecycle. Register one-shot session listeners that delete the file --
    only while it still matches the identity this transaction installed --
    once the transaction it lives in ends without a durable commit.
    Savepoint releases and unrelated later transactions are ignored.
    """
    sync = getattr(db, "sync_session", None)
    if sync is None:
        return
    root_transaction = sync.get_transaction()
    nested_transaction = sync.get_nested_transaction()
    committed = False
    nested_rolled_back = False
    done = False

    def _on_commit(session: Any) -> None:
        nonlocal committed
        if not session.in_nested_transaction():
            committed = True

    def _on_rollback(session: Any) -> None:
        nonlocal nested_rolled_back
        if (
            nested_transaction is not None
            and session.get_nested_transaction() is nested_transaction
        ):
            nested_rolled_back = True

    def _on_transaction_end(_session: Any, transaction: Any) -> None:
        nonlocal done
        if root_transaction is not None:
            if transaction is not root_transaction:
                return
        elif transaction.parent is not None:
            return
        if done:
            return
        done = True
        if not committed or nested_rolled_back:
            if _file_matches(
                destination,
                size_bytes=size_bytes,
                sha256=sha256,
            ):
                destination.unlink(missing_ok=True)

    event.listen(sync, "after_commit", _on_commit)
    event.listen(sync, "after_rollback", _on_rollback)
    event.listen(sync, "after_transaction_end", _on_transaction_end)


def make_video_reference_mp4(
    source_path: Path,
    destination: Path,
) -> VideoReferenceMp4:
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
    _validate_source_video(source_meta)
    target_width, target_height = _fit_even_dimensions(
        int(source_meta.get("width") or 0),
        int(source_meta.get("height") or 0),
    )
    _mkdir_parents_durable(destination.parent)
    with tempfile.TemporaryDirectory(
        prefix=".lumen-video-ref-",
        dir=destination.parent,
    ) as tmp:
        dst = Path(tmp) / "reference.mp4"
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
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-sn",
            "-dn",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            "-vf",
            (
                f"scale={target_width}:{target_height}:flags=lanczos,"
                f"fps={VIDEO_REFERENCE_VIDEO_TARGET_FPS}"
            ),
            "-c:v",
            "libx264",
            "-threads",
            "1",
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
            "-t",
            f"{source_meta['duration_ms'] / 1000:.3f}",
            "-fs",
            str(VIDEO_REFERENCE_VIDEO_MAX_BYTES),
            "-max_muxing_queue_size",
            "1024",
            "-movflags",
            "+faststart",
            str(dst),
        ]
        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=VIDEO_REFERENCE_VIDEO_FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VideoReferenceVideoError(
                "video_reference_transcode_failed",
                "reference video transcoding timed out",
                503,
            ) from exc
        except OSError as exc:
            raise VideoReferenceVideoError(
                "video_reference_transcode_failed",
                "reference video transcoding could not start",
                503,
            ) from exc
        if proc.returncode != 0 or not dst.is_file():
            logger.warning(
                "reference video ffmpeg failed returncode=%s stderr=%r",
                proc.returncode,
                proc.stderr.decode("utf-8", "replace")[-500:],
            )
            raise VideoReferenceVideoError(
                "video_reference_transcode_failed",
                "reference video transcode failed",
                503,
            )
        metadata = _probe_video(ffprobe, dst)
        _validate_output_video(
            metadata,
            expected_width=target_width,
            expected_height=target_height,
            expected_duration_ms=int(source_meta["duration_ms"]),
        )
        size_bytes = dst.stat().st_size
        sha256 = _sha256_file(dst)
        os.replace(dst, destination)
        _fsync_directory(destination.parent)
    return VideoReferenceMp4(
        width=target_width,
        height=target_height,
        duration_ms=int(metadata.get("duration_ms") or 0),
        fps=metadata.get("fps") if isinstance(metadata.get("fps"), float) else None,
        has_audio=bool(metadata.get("has_audio")),
        size_bytes=size_bytes,
        sha256=sha256,
    )


async def _wait_for_started_task(task: asyncio.Future[Any]) -> Any:
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _render_reference_variant(
    source_path: Path,
    destination: Path,
) -> VideoReferenceMp4:
    task = asyncio.ensure_future(
        asyncio.to_thread(
            make_video_reference_mp4,
            source_path,
            destination,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await _wait_for_started_task(task)
        except BaseException:
            pass
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise


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


def _result_rows(result: Any) -> list[Any]:
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars().all())
    value = result.scalar_one_or_none()
    return [] if value is None else [value]


async def _locked_reference_storage_usage(
    db: AsyncSession,
    *,
    user_id: str,
) -> int:
    cleanup_state = Video.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY][
        "state"
    ].as_string()
    upstream_variant_key = Video.metadata_jsonb["upstream_reference_video_variant"][
        "storage_key"
    ].as_string()
    volcano_variant_key = Video.metadata_jsonb["volcano_asset_video_variant"][
        "storage_key"
    ].as_string()
    rows = _result_rows(
        await db.execute(
            select(Video)
            .where(
                Video.user_id == user_id,
                or_(
                    Video.storage_key.like(f"u/{user_id}/vref/%"),
                    upstream_variant_key.is_not(None),
                    volcano_variant_key.is_not(None),
                ),
                or_(
                    Video.deleted_at.is_(None),
                    cleanup_state.is_(None),
                    cleanup_state != "complete",
                ),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    return sum(video_reference_declared_quota_contribution(row)[1] for row in rows)


async def ensure_video_reference_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
    storage_capacity: VideoStorageCapacityManager | None = None,
    transcode_capacity: VideoTranscodeCapacityManager | None = None,
) -> dict[str, Any]:
    source_video = (
        await db.execute(
            select(Video)
            .where(
                Video.id == video.id,
                Video.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source_video is None:
        raise VideoReferenceVideoError("not_found", "video was deleted", 404)
    video = source_video

    existing = video_reference_variant_metadata(video)
    if existing is not None and _file_matches(
        _storage_path(storage_root, str(existing["storage_key"])),
        size_bytes=int(existing.get("size_bytes") or 0),
        sha256=str(existing["sha256"]),
    ):
        return existing

    source_video_id = str(video.id)
    source_user_id = str(video.user_id)
    source_storage_key = str(video.storage_key)
    source_sha256 = str(video.sha256)
    source_path = _storage_path(storage_root, video.storage_key)
    key = video_reference_video_key(video)
    destination = _storage_path(storage_root, key)
    staged = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.stage")
    installed = False
    try:
        async with (
            transcode_capacity or build_video_transcode_capacity_manager()
        ).hold(user_id=str(video.user_id)):
            async with (
                storage_capacity or build_video_storage_capacity_manager()
            ).reserve(2 * VIDEO_REFERENCE_VIDEO_MAX_BYTES):
                rendered = await _render_reference_variant(source_path, staged)
                await db.execute(
                    select(User.id).where(User.id == source_user_id).with_for_update()
                )
                current_video = (
                    await db.execute(
                        select(Video)
                        .where(
                            Video.id == source_video_id,
                            Video.user_id == source_user_id,
                            Video.storage_key == source_storage_key,
                            Video.sha256 == source_sha256,
                            Video.deleted_at.is_(None),
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if current_video is None:
                    raise VideoReferenceVideoError(
                        "video_reference_changed",
                        "video changed or was deleted during reference preparation",
                        409,
                    )
                video = current_video
                existing = video_reference_variant_metadata(video)
                if existing is not None and _file_matches(
                    _storage_path(
                        storage_root,
                        str(existing["storage_key"]),
                    ),
                    size_bytes=int(existing.get("size_bytes") or 0),
                    sha256=str(existing["sha256"]),
                ):
                    return existing
                current_bytes = await _locked_reference_storage_usage(
                    db,
                    user_id=source_user_id,
                )
                replaced_bytes = video_reference_variant_quota_bytes(
                    video,
                    "upstream_reference_video_variant",
                )
                enforce_video_reference_storage_quota(
                    current_bytes=current_bytes,
                    replaced_bytes=replaced_bytes,
                    added_bytes=rendered.size_bytes,
                )
                _discard_orphaned_variant_on_rollback(
                    db,
                    destination,
                    size_bytes=rendered.size_bytes,
                    sha256=rendered.sha256,
                )
                install_task = asyncio.ensure_future(
                    asyncio.to_thread(
                        _install_staged_variant,
                        staged,
                        destination,
                        size_bytes=rendered.size_bytes,
                        sha256=rendered.sha256,
                    )
                )
                try:
                    await asyncio.shield(install_task)
                except asyncio.CancelledError:
                    await _wait_for_started_task(install_task)
                    raise
                installed = True

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
                    "max_bytes": VIDEO_REFERENCE_VIDEO_MAX_BYTES,
                    "target_fps": VIDEO_REFERENCE_VIDEO_TARGET_FPS,
                }
                metadata = dict(video.metadata_jsonb or {})
                metadata["upstream_reference_video_variant"] = variant
                video.metadata_jsonb = metadata
                return variant
    except VideoTranscodeCapacityUnavailable as exc:
        raise VideoReferenceVideoError(
            "video_reference_transcode_capacity",
            "reference video transcoding capacity is temporarily unavailable",
            503,
        ) from exc
    except StorageCapacityExceeded as exc:
        raise VideoReferenceVideoError(
            "storage_insufficient_space",
            "not enough free storage to transcode the reference video",
            507,
        ) from exc
    except (StorageCapacityUnavailable, CapacityLeaseLost) as exc:
        if installed and _file_matches(
            destination,
            size_bytes=rendered.size_bytes,
            sha256=rendered.sha256,
        ):
            await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise VideoReferenceVideoError(
            "storage_capacity_unavailable",
            "reference video storage capacity is temporarily unavailable",
            503,
        ) from exc
    except VideoReferenceStorageQuotaExceeded as exc:
        raise VideoReferenceVideoError(
            "reference_video_quota_exceeded",
            "reference video storage quota exceeded",
            429,
        ) from exc
    finally:
        await asyncio.to_thread(staged.unlink, missing_ok=True)
