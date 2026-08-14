"""Video upstream video reference variants."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import and_, case, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.capacity_leases import CapacityLeaseLost
from lumen_core.models import User, Video
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityUnavailable,
)

from . import video_reference_probe as reference_probe
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
    video_reference_variant_quota_bytes,
)

_STARTED_TASK_CONFIRMATION_TIMEOUT_SECONDS = 30.0
_VIDEO_REFERENCE_VIDEO_HASH_CHUNK_BYTES = 1024 * 1024

logger = logging.getLogger(__name__)

VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES = (
    reference_probe.VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES
)
VIDEO_REFERENCE_VIDEO_FFMPEG_TIMEOUT_SECONDS = (
    reference_probe.VIDEO_REFERENCE_VIDEO_FFMPEG_TIMEOUT_SECONDS
)
VIDEO_REFERENCE_VIDEO_KIND = reference_probe.VIDEO_REFERENCE_VIDEO_KIND
VIDEO_REFERENCE_VIDEO_MAX_BYTES = reference_probe.VIDEO_REFERENCE_VIDEO_MAX_BYTES
VIDEO_REFERENCE_VIDEO_MAX_SIDE = reference_probe.VIDEO_REFERENCE_VIDEO_MAX_SIDE
VIDEO_REFERENCE_VIDEO_MIME = reference_probe.VIDEO_REFERENCE_VIDEO_MIME
VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT = reference_probe.VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS
)
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS
)
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS
)
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE
)
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS
)
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE
)
VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS = (
    reference_probe.VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS
)
VIDEO_REFERENCE_VIDEO_TARGET_FPS = reference_probe.VIDEO_REFERENCE_VIDEO_TARGET_FPS
VideoReferenceVideoError = reference_probe.VideoReferenceVideoError
_fit_even_dimensions = reference_probe._fit_even_dimensions
_float_or_none = reference_probe._float_or_none
_fps = reference_probe._fps
_int_or_zero = reference_probe._int_or_zero
_probe_video = reference_probe._probe_video
_validate_output_video = reference_probe._validate_output_video
_validate_source_video = reference_probe._validate_source_video


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


def _discard_replaced_variant_sync(
    *,
    storage_root: str,
    replaced: dict[str, Any],
    replacement_key: str,
) -> None:
    storage_key = replaced.get("storage_key")
    if (
        not isinstance(storage_key, str)
        or not storage_key
        or storage_key == replacement_key
    ):
        return
    try:
        size_bytes = int(replaced.get("size_bytes") or 0)
        sha256 = str(replaced["sha256"])
        path = _storage_path(storage_root, storage_key)
        if _file_matches(path, size_bytes=size_bytes, sha256=sha256):
            path.unlink(missing_ok=True)
    except Exception:
        logger.warning(
            "replaced reference video variant cleanup failed key=%s",
            storage_key,
            exc_info=True,
        )


def _discard_replaced_variant_after_commit(
    db: AsyncSession,
    *,
    storage_root: str,
    replaced: dict[str, Any] | None,
    replacement_key: str,
) -> None:
    if not isinstance(replaced, dict):
        return
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
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _discard_replaced_variant_sync(
                storage_root=storage_root,
                replaced=replaced,
                replacement_key=replacement_key,
            )
            return
        future = loop.run_in_executor(
            None,
            lambda: _discard_replaced_variant_sync(
                storage_root=storage_root,
                replaced=replaced,
                replacement_key=replacement_key,
            ),
        )
        future.add_done_callback(
            lambda completed: (
                completed.exception() if not completed.cancelled() else None
            )
        )

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
    deadline = time.monotonic() + _STARTED_TASK_CONFIRMATION_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("video reference operation confirmation timed out")
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
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
    except asyncio.CancelledError as cancellation:
        try:
            await _wait_for_started_task(task)
        except BaseException:
            task.add_done_callback(lambda _done: destination.unlink(missing_ok=True))
        await asyncio.to_thread(destination.unlink, missing_ok=True)
        raise cancellation


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


@dataclass(frozen=True)
class _ReferenceSourceSnapshot:
    id: str
    user_id: str
    storage_key: str
    sha256: str
    metadata_jsonb: dict[str, Any]

    @classmethod
    def from_video(cls, video: Video) -> _ReferenceSourceSnapshot:
        return cls(
            str(video.id),
            str(video.user_id),
            str(video.storage_key),
            str(video.sha256),
            metadata_jsonb=dict(video.metadata_jsonb or {}),
        )


async def _reference_storage_usage(db: AsyncSession, *, user_id: str) -> int:
    cleanup_state = Video.metadata_jsonb[VIDEO_STORAGE_CLEANUP_METADATA_KEY][
        "state"
    ].as_string()
    cleanup_complete = and_(Video.deleted_at.is_not(None), cleanup_state == "complete")
    primary_bytes = case(
        (Video.storage_key.like(f"u/{user_id}/vref/%"), Video.size_bytes),
        else_=0,
    )
    upstream_bytes = func.coalesce(
        Video.metadata_jsonb["upstream_reference_video_variant"][
            "size_bytes"
        ].as_integer(),
        0,
    )
    volcano_bytes = func.coalesce(
        Video.metadata_jsonb["volcano_asset_video_variant"]["size_bytes"].as_integer(),
        0,
    )
    volcano_poster_bytes = func.coalesce(
        Video.metadata_jsonb["volcano_asset_video_variant"][
            "poster_size_bytes"
        ].as_integer(),
        0,
    )
    contribution = case(
        (cleanup_complete, 0),
        else_=primary_bytes + upstream_bytes + volcano_bytes + volcano_poster_bytes,
    )
    raw = (
        await db.execute(
            select(func.coalesce(func.sum(contribution), 0)).where(
                Video.user_id == user_id
            )
        )
    ).scalar_one()
    return max(0, int(raw or 0))


async def _snapshot_reference_source(
    db: AsyncSession,
    *,
    video_id: str,
    manage_transaction: bool,
) -> _ReferenceSourceSnapshot:
    source = (
        await db.execute(
            select(Video)
            .where(Video.id == video_id, Video.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source is None:
        if manage_transaction:
            await db.rollback()
        raise VideoReferenceVideoError("not_found", "video was deleted", 404)
    snapshot = _ReferenceSourceSnapshot.from_video(source)
    if manage_transaction:
        await db.commit()
    return snapshot


async def _variant_file_matches(
    storage_root: str,
    variant: dict[str, Any],
) -> bool:
    return await asyncio.to_thread(
        _file_matches,
        _storage_path(storage_root, str(variant["storage_key"])),
        size_bytes=int(variant.get("size_bytes") or 0),
        sha256=str(variant["sha256"]),
    )


def _variant_payload(
    rendered: VideoReferenceMp4,
    *,
    storage_key: str,
) -> dict[str, Any]:
    return {
        "kind": VIDEO_REFERENCE_VIDEO_KIND,
        "storage_key": storage_key,
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


async def _discard_installed_variant(
    path: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> None:
    if await asyncio.to_thread(
        _file_matches,
        path,
        size_bytes=size_bytes,
        sha256=sha256,
    ):
        await asyncio.to_thread(path.unlink, missing_ok=True)


async def _discard_replaced_variant_best_effort(
    *,
    storage_root: str,
    replaced: dict[str, Any] | None,
    replacement_key: str,
) -> None:
    if not isinstance(replaced, dict):
        return
    storage_key = replaced.get("storage_key")
    if (
        not isinstance(storage_key, str)
        or not storage_key
        or storage_key == replacement_key
    ):
        return
    try:
        size_bytes = int(replaced.get("size_bytes") or 0)
        sha256 = str(replaced["sha256"])
        await _discard_installed_variant(
            _storage_path(storage_root, storage_key),
            size_bytes=size_bytes,
            sha256=sha256,
        )
    except Exception:
        logger.warning(
            "replaced reference video variant cleanup failed key=%s",
            storage_key,
            exc_info=True,
        )


async def _adopt_reference_variant(
    db: AsyncSession,
    *,
    source: _ReferenceSourceSnapshot,
    variant: dict[str, Any],
    destination: Path,
    observed_existing: dict[str, Any] | None,
    storage_root: str,
    manage_transaction: bool,
) -> tuple[dict[str, Any], bool]:
    observed = observed_existing
    for _attempt in range(3):
        try:
            await db.execute(
                select(User.id).where(User.id == source.user_id).with_for_update()
            )
            current = (
                await db.execute(
                    select(Video)
                    .where(
                        Video.id == source.id,
                        Video.user_id == source.user_id,
                        Video.storage_key == source.storage_key,
                        Video.sha256 == source.sha256,
                        Video.deleted_at.is_(None),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if current is None:
                raise VideoReferenceVideoError(
                    "video_reference_changed",
                    "video changed or was deleted during reference preparation",
                    409,
                )
            existing = video_reference_variant_metadata(current)
            if existing is not None and existing != observed:
                candidate = dict(existing)
                if manage_transaction:
                    await db.commit()
                if await _variant_file_matches(storage_root, candidate):
                    return candidate, False
                observed = candidate
                continue
            current_bytes = await _reference_storage_usage(
                db,
                user_id=source.user_id,
            )
            enforce_video_reference_storage_quota(
                current_bytes=current_bytes,
                replaced_bytes=video_reference_variant_quota_bytes(
                    current,
                    "upstream_reference_video_variant",
                ),
                added_bytes=int(variant["size_bytes"]),
            )
            _discard_orphaned_variant_on_rollback(
                db,
                destination,
                size_bytes=int(variant["size_bytes"]),
                sha256=str(variant["sha256"]),
            )
            metadata = dict(current.metadata_jsonb or {})
            metadata["upstream_reference_video_variant"] = variant
            current.metadata_jsonb = metadata
            if manage_transaction:
                await db.commit()
                await _discard_replaced_variant_best_effort(
                    storage_root=storage_root,
                    replaced=existing,
                    replacement_key=str(variant["storage_key"]),
                )
            else:
                await db.flush()
                _discard_replaced_variant_after_commit(
                    db,
                    storage_root=storage_root,
                    replaced=existing,
                    replacement_key=str(variant["storage_key"]),
                )
            return variant, True
        except BaseException:
            if manage_transaction:
                await db.rollback()
            raise
    raise VideoReferenceVideoError(
        "video_reference_changed",
        "reference video variant changed concurrently; retry",
        409,
    )


async def ensure_video_reference_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
    storage_capacity: VideoStorageCapacityManager | None = None,
    transcode_capacity: VideoTranscodeCapacityManager | None = None,
) -> dict[str, Any]:
    nested_checker = getattr(db, "in_nested_transaction", None)
    manage_transaction = not (callable(nested_checker) and bool(nested_checker()))
    source = await _snapshot_reference_source(
        db,
        video_id=str(video.id),
        manage_transaction=manage_transaction,
    )
    existing_raw = source.metadata_jsonb.get("upstream_reference_video_variant")
    existing = dict(existing_raw) if isinstance(existing_raw, dict) else None
    if existing is not None and await _variant_file_matches(storage_root, existing):
        return existing
    source_path = _storage_path(storage_root, source.storage_key)
    staged = source_path.with_name(
        f".{source.id}.{VIDEO_REFERENCE_VIDEO_KIND}.{secrets.token_hex(8)}.stage"
    )
    destination: Path | None = None
    rendered: VideoReferenceMp4 | None = None
    installed = False
    adopted = False
    try:
        async with (
            transcode_capacity or build_video_transcode_capacity_manager()
        ).hold(user_id=source.user_id):
            refreshed = await _snapshot_reference_source(
                db,
                video_id=source.id,
                manage_transaction=manage_transaction,
            )
            if (
                refreshed.user_id != source.user_id
                or refreshed.storage_key != source.storage_key
                or refreshed.sha256 != source.sha256
            ):
                raise VideoReferenceVideoError(
                    "video_reference_changed",
                    "video changed or was deleted during reference preparation",
                    409,
                )
            refreshed_existing_raw = refreshed.metadata_jsonb.get(
                "upstream_reference_video_variant"
            )
            refreshed_existing = (
                dict(refreshed_existing_raw)
                if isinstance(refreshed_existing_raw, dict)
                else None
            )
            if refreshed_existing is not None and await _variant_file_matches(
                storage_root,
                refreshed_existing,
            ):
                return refreshed_existing
            source = refreshed
            existing = refreshed_existing
            async with (
                storage_capacity or build_video_storage_capacity_manager()
            ).reserve(2 * VIDEO_REFERENCE_VIDEO_MAX_BYTES):
                rendered = await _render_reference_variant(source_path, staged)
                key = str(
                    Path(source.storage_key).with_name(
                        f"{source.id}.{VIDEO_REFERENCE_VIDEO_KIND}."
                        f"{secrets.token_hex(12)}.mp4"
                    )
                )
                destination = _storage_path(storage_root, key)
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
                except asyncio.CancelledError as cancellation:
                    try:
                        await _wait_for_started_task(install_task)
                    except BaseException:
                        install_task.add_done_callback(
                            lambda _done: destination.unlink(missing_ok=True)
                        )
                    raise cancellation
                installed = True
                variant = _variant_payload(rendered, storage_key=key)
                result, adopted = await _adopt_reference_variant(
                    db,
                    source=source,
                    variant=variant,
                    destination=destination,
                    observed_existing=existing,
                    storage_root=storage_root,
                    manage_transaction=manage_transaction,
                )
                if not adopted:
                    await _discard_installed_variant(
                        destination,
                        size_bytes=rendered.size_bytes,
                        sha256=rendered.sha256,
                    )
                    installed = False
                return result
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
        if (
            installed
            and not adopted
            and destination is not None
            and rendered is not None
        ):
            await _discard_installed_variant(
                destination,
                size_bytes=rendered.size_bytes,
                sha256=rendered.sha256,
            )
