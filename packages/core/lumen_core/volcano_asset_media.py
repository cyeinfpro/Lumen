"""Shared dedicated media normalization for Volcano AIGC assets."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    race_with_capacity_lease,
)
from lumen_core.models import Image, ImageVariant, Video
from lumen_core.storage_capacity import (
    StorageCapacityExceeded,
    StorageCapacityPort,
    StorageCapacityUnavailable,
)

from . import volcano_asset_media_transcode as _transcode
from .volcano_asset_media_types import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_IMAGE_MAX_BYTES,
    VOLCANO_ASSET_IMAGE_MAX_SIDE,
    VOLCANO_ASSET_IMAGE_MIME,
    VOLCANO_ASSET_MAX_ASPECT_RATIO,
    VOLCANO_ASSET_MIN_ASPECT_RATIO,
    VOLCANO_ASSET_MIN_SIDE,
    VOLCANO_ASSET_SOURCE_MAX_PIXELS,
    VOLCANO_ASSET_VIDEO_FPS,
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_MAX_BYTES,
    VOLCANO_ASSET_VIDEO_MAX_DURATION_MS,
    VOLCANO_ASSET_VIDEO_MAX_PIXELS,
    VOLCANO_ASSET_VIDEO_MAX_SIDE,
    VOLCANO_ASSET_VIDEO_METADATA_KEY,
    VOLCANO_ASSET_VIDEO_MIME,
    VOLCANO_ASSET_VIDEO_MIN_DURATION_MS,
    VOLCANO_ASSET_VIDEO_MIN_PIXELS,
    VOLCANO_ASSET_VIDEO_PROFILES,
    VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE,
    VolcanoAssetImageJpeg,
    VolcanoAssetInstallReceipt,
    VolcanoAssetMediaError,
    VolcanoAssetVideoMp4,
)


logger = logging.getLogger(__name__)
VIDEO_REFERENCE_STORAGE_QUOTA_BYTES = 1024 * 1024 * 1024
VOLCANO_ASSET_PREPARE_TIMEOUT_SECONDS = 360.0
VOLCANO_ASSET_FINALIZE_TIMEOUT_SECONDS = 15.0
VOLCANO_ASSET_ROLLBACK_TIMEOUT_SECONDS = 3.0
VOLCANO_ASSET_CLEANUP_TIMEOUT_SECONDS = 3.0
VOLCANO_ASSET_CONVERGENCE_ATTEMPTS = 3
VOLCANO_ASSET_REFERENCE_SCAN_LIMIT = 2_048
_VIDEO_STORAGE_CLEANUP_METADATA_KEY = "video_storage_cleanup"
_VIDEO_REFERENCE_VARIANT_METADATA_KEYS = (
    "upstream_reference_video_variant",
    VOLCANO_ASSET_VIDEO_METADATA_KEY,
)

_even = _transcode._even
_padded_canvas_size = _transcode._padded_canvas_size
_image_layout = _transcode._image_layout
_flatten_image = _transcode._flatten_image
_validate_image_output = _transcode._validate_image_output
make_volcano_asset_image_jpeg = _transcode.make_volcano_asset_image_jpeg
volcano_asset_image_key = _transcode.volcano_asset_image_key
_image_variant_file_is_valid = _transcode._image_variant_file_is_valid
_float_or_none = _transcode._float_or_none
_fps = _transcode._fps
_probe_video = _transcode._probe_video
_video_target_dimensions = _transcode._video_target_dimensions
_video_target_duration_seconds = _transcode._video_target_duration_seconds
_validate_video_output = _transcode._validate_video_output
_ffmpeg_command = _transcode._ffmpeg_command
_make_video_poster_jpeg = _transcode._make_video_poster_jpeg


class _VideoTranscodeRuntime:
    def __init__(self) -> None:
        self.semaphores: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop,
            asyncio.Semaphore,
        ] = weakref.WeakKeyDictionary()
        self.lock = threading.Lock()

    def semaphore_for_running_loop(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self.lock:
            semaphore = self.semaphores.get(loop)
            if semaphore is None:
                semaphore = asyncio.Semaphore(2)
                self.semaphores[loop] = semaphore
            return semaphore

    def reset(self) -> None:
        with self.lock:
            self.semaphores.clear()


_VIDEO_TRANSCODE_RUNTIME = _VideoTranscodeRuntime()


def _storage_path(storage_root: str, storage_key: str) -> Path:
    root = Path(storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise VolcanoAssetMediaError("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise VolcanoAssetMediaError(
            "invalid_path",
            "absolute storage paths are not allowed",
            400,
        )
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise VolcanoAssetMediaError(
            "invalid_path",
            "storage path escapes root",
            400,
        ) from exc
    return path


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(
    path: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == size_bytes
            and _file_sha256(path) == sha256
        )
    except OSError:
        return False


def _write_temp_file(path: Path, data: bytes) -> Path:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _install_file_atomic(
    path: Path,
    data: bytes,
    *,
    sha256: str,
) -> bool:
    size_bytes = len(data)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "normalized asset media checksum is invalid",
            503,
        )
    _mkdir_parents_durable(path.parent)
    existed_before = path.exists()
    for _attempt in range(3):
        if _file_matches(path, size_bytes=size_bytes, sha256=sha256):
            return False
        tmp: Path | None = None
        try:
            tmp = _write_temp_file(path, data)
            os.replace(tmp, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise VolcanoAssetMediaError(
                "volcano_asset_media_storage_failed",
                "normalized asset media could not be stored",
                503,
            ) from exc
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        if _file_matches(path, size_bytes=size_bytes, sha256=sha256):
            return not existed_before
    raise VolcanoAssetMediaError(
        "volcano_asset_media_storage_conflict",
        "normalized asset media changed while it was being stored",
        503,
    )


def _delete_install_if_unchanged(
    storage_root: str,
    receipt: VolcanoAssetInstallReceipt,
) -> bool:
    path = _storage_path(storage_root, receipt.storage_key)
    if not _file_matches(
        path,
        size_bytes=receipt.size_bytes,
        sha256=receipt.sha256,
    ):
        return False
    path.unlink()
    _fsync_directory(path.parent)
    return True


async def delete_volcano_asset_install(
    storage_root: str,
    receipt: VolcanoAssetInstallReceipt | None,
) -> bool:
    if receipt is None:
        return False
    return await asyncio.to_thread(
        _delete_install_if_unchanged,
        storage_root,
        receipt,
    )


async def _cleanup_install_best_effort(
    storage_root: str,
    receipt: VolcanoAssetInstallReceipt | None,
) -> None:
    try:
        await delete_volcano_asset_install(storage_root, receipt)
    except (OSError, VolcanoAssetMediaError):
        logger.warning(
            "Volcano asset media cleanup failed key=%s",
            None if receipt is None else receipt.storage_key,
            exc_info=True,
        )


def _install_file_for_attempt(
    path: Path,
    data: bytes,
    *,
    sha256: str,
    abandoned: threading.Event,
    storage_root: str,
    storage_key: str,
) -> bool:
    created = _install_file_atomic(path, data, sha256=sha256)
    if not created or not abandoned.is_set():
        return created
    try:
        _delete_install_if_unchanged(
            storage_root,
            VolcanoAssetInstallReceipt(
                storage_key=storage_key,
                size_bytes=len(data),
                sha256=sha256,
            ),
        )
    except (OSError, VolcanoAssetMediaError):
        logger.warning(
            "Abandoned Volcano asset install cleanup failed key=%s",
            storage_key,
            exc_info=True,
        )
    return False


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except BaseException:
        pass


def _consume_abandoned_install(
    task: asyncio.Task[bool],
    *,
    abandoned: threading.Event,
    storage_root: str,
    storage_key: str,
    size_bytes: int,
    sha256: str,
) -> None:
    try:
        created = task.result()
    except BaseException:
        return
    if not created or not abandoned.is_set():
        return
    cleanup = asyncio.create_task(
        _cleanup_install_best_effort(
            storage_root,
            VolcanoAssetInstallReceipt(
                storage_key=storage_key,
                size_bytes=size_bytes,
                sha256=sha256,
            ),
        )
    )
    cleanup.add_done_callback(_consume_background_task)


async def _install_rendered_media(
    *,
    storage_root: str,
    storage_key: str,
    data: bytes,
    sha256: str,
    guard: CapacityLeaseGuard,
) -> VolcanoAssetInstallReceipt | None:
    destination = _storage_path(storage_root, storage_key)
    abandoned = threading.Event()
    install_task = asyncio.create_task(
        asyncio.to_thread(
            _install_file_for_attempt,
            destination,
            data,
            sha256=sha256,
            abandoned=abandoned,
            storage_root=storage_root,
            storage_key=storage_key,
        )
    )
    try:
        created = bool(
            await race_with_capacity_lease(
                asyncio.shield(install_task),
                guard,
            )
        )
    except BaseException:
        abandoned.set()
        install_task.add_done_callback(
            lambda done: _consume_abandoned_install(
                done,
                abandoned=abandoned,
                storage_root=storage_root,
                storage_key=storage_key,
                size_bytes=len(data),
                sha256=sha256,
            )
        )
        raise
    if not created:
        return None
    return VolcanoAssetInstallReceipt(
        storage_key=storage_key,
        size_bytes=len(data),
        sha256=sha256,
    )


async def _reserve_media_capacity(
    storage_capacity: StorageCapacityPort,
    size_bytes: int,
) -> Any:
    try:
        return await storage_capacity.reserve(2 * max(0, size_bytes))
    except (StorageCapacityExceeded, StorageCapacityUnavailable) as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_capacity",
            "normalized asset media storage capacity is unavailable",
            503,
        ) from exc


def _video_transcode_semaphore() -> asyncio.Semaphore:
    return _VIDEO_TRANSCODE_RUNTIME.semaphore_for_running_loop()


def make_volcano_asset_video_mp4(
    source_path: Path,
    *,
    timeout_seconds: float = 300.0,
) -> VolcanoAssetVideoMp4:
    timeout_budget = float(timeout_seconds)
    if timeout_budget <= 60.0:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "asset video transcoding timed out",
            503,
        )
    deadline = time.monotonic() + timeout_budget - 60.0

    def remaining_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VolcanoAssetMediaError(
                "volcano_asset_video_transcode_failed",
                "asset video transcoding timed out",
                503,
            )
        return remaining

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcoder_missing",
            "ffmpeg and ffprobe are required for Volcano asset videos",
            503,
        )
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "video binary is missing", 404)

    source_metadata = _probe_video(ffprobe, source_path)
    source_width = int(source_metadata.get("width") or 0)
    source_height = int(source_metadata.get("height") or 0)
    source_duration_ms = int(source_metadata.get("duration_ms") or 0)
    if source_width <= 0 or source_height <= 0 or source_duration_ms <= 0:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_decode_failed",
            "asset video metadata is invalid",
            422,
        )
    if source_width * source_height > VOLCANO_ASSET_SOURCE_MAX_PIXELS:
        raise VolcanoAssetMediaError(
            "too_many_pixels",
            "video exceeds the safe pixel limit",
            413,
        )
    target_width, target_height = _video_target_dimensions(
        source_width,
        source_height,
    )
    target_duration_s = _video_target_duration_seconds(source_duration_ms)
    last_error: VolcanoAssetMediaError | None = None
    with tempfile.TemporaryDirectory(prefix="lumen-volcano-asset-") as tmp:
        destination = Path(tmp) / "asset.mp4"
        for profile in VOLCANO_ASSET_VIDEO_PROFILES:
            command = _ffmpeg_command(
                ffmpeg=ffmpeg,
                source_path=source_path,
                destination=destination,
                source_has_audio=bool(source_metadata.get("has_audio")),
                width=target_width,
                height=target_height,
                duration_s=target_duration_s,
                profile=profile,
            )
            try:
                proc = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=remaining_timeout(),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise VolcanoAssetMediaError(
                    "volcano_asset_video_transcode_failed",
                    "asset video transcoding timed out",
                    503,
                ) from exc
            except OSError as exc:
                raise VolcanoAssetMediaError(
                    "volcano_asset_video_transcode_failed",
                    "asset video transcoding could not start",
                    503,
                ) from exc
            if proc.returncode != 0 or not destination.is_file():
                raise VolcanoAssetMediaError(
                    "volcano_asset_video_transcode_failed",
                    "asset video transcoding failed",
                    503,
                )
            output_metadata = _probe_video(ffprobe, destination)
            try:
                _validate_video_output(output_metadata)
            except VolcanoAssetMediaError as exc:
                last_error = exc
                if (
                    int(output_metadata.get("size_bytes") or 0)
                    > VOLCANO_ASSET_VIDEO_MAX_BYTES
                ):
                    destination.unlink(missing_ok=True)
                    continue
                raise
            data = destination.read_bytes()
            poster_bytes = _make_video_poster_jpeg(
                ffmpeg,
                destination,
                Path(tmp) / "poster.jpg",
                timeout_seconds=remaining_timeout(),
            )
            return VolcanoAssetVideoMp4(
                data=data,
                width=int(output_metadata["width"]),
                height=int(output_metadata["height"]),
                duration_ms=int(output_metadata["duration_ms"]),
                fps=float(output_metadata["fps"]),
                has_audio=bool(output_metadata["has_audio"]),
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                poster_bytes=poster_bytes,
            )
    raise last_error or VolcanoAssetMediaError(
        "volcano_asset_video_transcode_failed",
        "asset video could not be compressed to the required size",
        503,
    )


def volcano_asset_video_key(video: Video) -> str:
    source = Path(video.storage_key)
    return str(source.with_name(f"{video.id}.{VOLCANO_ASSET_VIDEO_KIND}.mp4"))


def volcano_asset_video_variant_metadata(video: Video) -> dict[str, Any] | None:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    raw = metadata.get(VOLCANO_ASSET_VIDEO_METADATA_KEY)
    if not isinstance(raw, dict) or raw.get("kind") != VOLCANO_ASSET_VIDEO_KIND:
        return None
    storage_key = raw.get("storage_key")
    sha256 = raw.get("sha256")
    if not isinstance(storage_key, str) or not storage_key:
        return None
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        return None
    normalized = dict(raw)
    normalized["sha256"] = sha256.lower()
    return normalized


def _video_cleanup_is_complete(video: Any) -> bool:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    cleanup = metadata.get(_VIDEO_STORAGE_CLEANUP_METADATA_KEY)
    return (
        getattr(video, "deleted_at", None) is not None
        and isinstance(cleanup, dict)
        and cleanup.get("state") == "complete"
    )


def _video_variant_quota_bytes(video: Any, metadata_key: str) -> int:
    if _video_cleanup_is_complete(video):
        return 0
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    raw = metadata.get(metadata_key)
    if not isinstance(raw, dict):
        return 0
    return _video_variant_payload_bytes(raw)


def _video_variant_payload_bytes(raw: dict[str, Any]) -> int:
    try:
        return max(0, int(raw.get("size_bytes") or 0)) + max(
            0,
            int(raw.get("poster_size_bytes") or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def _video_reference_declared_bytes(video: Any) -> int:
    if _video_cleanup_is_complete(video):
        return 0
    video_id = str(getattr(video, "id", "") or "")
    user_id = str(getattr(video, "user_id", "") or "")
    storage_key = str(getattr(video, "storage_key", "") or "")
    primary_bytes = (
        max(0, int(getattr(video, "size_bytes", 0) or 0))
        if storage_key.startswith(f"u/{user_id}/vref/{video_id}/")
        else 0
    )
    return primary_bytes + sum(
        _video_variant_quota_bytes(video, metadata_key)
        for metadata_key in _VIDEO_REFERENCE_VARIANT_METADATA_KEYS
    )


def _result_rows(result: Any) -> list[Any]:
    scalars = getattr(result, "scalars", None)
    if callable(scalars):
        return list(scalars().all())
    value = result.scalar_one_or_none()
    return [] if value is None else [value]


def _video_variant_file_is_valid(
    path: Path,
    metadata: dict[str, Any],
) -> bool:
    if metadata.get("mime") != VOLCANO_ASSET_VIDEO_MIME:
        return False
    validation_metadata = {
        **metadata,
        "video_codec": "h264",
        "audio_codec": "aac" if metadata.get("has_audio") else "",
    }
    try:
        _validate_video_output(validation_metadata)
        size_bytes = int(metadata.get("size_bytes") or 0)
        sha256 = str(metadata["sha256"])
    except (KeyError, OverflowError, TypeError, ValueError, VolcanoAssetMediaError):
        return False
    return _file_matches(
        path,
        size_bytes=size_bytes,
        sha256=sha256,
    )


def _variant_workflow_runtime() -> Any:
    from .volcano_asset_variant_contracts import VariantWorkflowRuntime

    return VariantWorkflowRuntime(
        cleanup_install_best_effort=_cleanup_install_best_effort,
        cleanup_timeout_seconds=VOLCANO_ASSET_CLEANUP_TIMEOUT_SECONDS,
        rollback_timeout_seconds=VOLCANO_ASSET_ROLLBACK_TIMEOUT_SECONDS,
        prepare_timeout_seconds=VOLCANO_ASSET_PREPARE_TIMEOUT_SECONDS,
        finalize_timeout_seconds=VOLCANO_ASSET_FINALIZE_TIMEOUT_SECONDS,
        convergence_attempts=VOLCANO_ASSET_CONVERGENCE_ATTEMPTS,
        reference_scan_limit=VOLCANO_ASSET_REFERENCE_SCAN_LIMIT,
        video_reference_storage_quota_bytes=VIDEO_REFERENCE_STORAGE_QUOTA_BYTES,
        storage_path=_storage_path,
        image_variant_file_is_valid=_image_variant_file_is_valid,
        make_image_jpeg=make_volcano_asset_image_jpeg,
        reserve_media_capacity=_reserve_media_capacity,
        install_rendered_media=_install_rendered_media,
        video_variant_metadata=volcano_asset_video_variant_metadata,
        video_variant_file_is_valid=_video_variant_file_is_valid,
        video_transcode_semaphore=_video_transcode_semaphore,
        make_video_mp4=make_volcano_asset_video_mp4,
        result_rows=_result_rows,
        video_reference_declared_bytes=_video_reference_declared_bytes,
        video_variant_quota_bytes=_video_variant_quota_bytes,
    )


async def ensure_volcano_asset_image_variant(
    db: AsyncSession,
    image: Image,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
) -> tuple[ImageVariant, VolcanoAssetInstallReceipt | None]:
    from .volcano_asset_variant_workflow import ensure_image_variant

    return await ensure_image_variant(
        db,
        image,
        storage_root=storage_root,
        storage_capacity=storage_capacity,
        storage_lease_ttl_seconds=storage_lease_ttl_seconds,
        runtime=_variant_workflow_runtime(),
    )


async def ensure_volcano_asset_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
) -> tuple[dict[str, Any], VolcanoAssetInstallReceipt | None]:
    from .volcano_asset_variant_workflow import ensure_video_variant

    return await ensure_video_variant(
        db,
        video,
        storage_root=storage_root,
        storage_capacity=storage_capacity,
        storage_lease_ttl_seconds=storage_lease_ttl_seconds,
        runtime=_variant_workflow_runtime(),
    )


__all__ = [
    "VOLCANO_ASSET_IMAGE_KIND",
    "VOLCANO_ASSET_IMAGE_MAX_BYTES",
    "VOLCANO_ASSET_IMAGE_MAX_SIDE",
    "VOLCANO_ASSET_IMAGE_MIME",
    "VOLCANO_ASSET_PREPARE_TIMEOUT_SECONDS",
    "VOLCANO_ASSET_FINALIZE_TIMEOUT_SECONDS",
    "VOLCANO_ASSET_ROLLBACK_TIMEOUT_SECONDS",
    "VOLCANO_ASSET_CLEANUP_TIMEOUT_SECONDS",
    "VOLCANO_ASSET_CONVERGENCE_ATTEMPTS",
    "VOLCANO_ASSET_REFERENCE_SCAN_LIMIT",
    "VOLCANO_ASSET_MAX_ASPECT_RATIO",
    "VOLCANO_ASSET_MIN_ASPECT_RATIO",
    "VOLCANO_ASSET_MIN_SIDE",
    "VOLCANO_ASSET_SOURCE_MAX_PIXELS",
    "VOLCANO_ASSET_VIDEO_FPS",
    "VOLCANO_ASSET_VIDEO_KIND",
    "VOLCANO_ASSET_VIDEO_MAX_BYTES",
    "VOLCANO_ASSET_VIDEO_MAX_DURATION_MS",
    "VOLCANO_ASSET_VIDEO_MAX_PIXELS",
    "VOLCANO_ASSET_VIDEO_MAX_SIDE",
    "VOLCANO_ASSET_VIDEO_METADATA_KEY",
    "VOLCANO_ASSET_VIDEO_MIME",
    "VOLCANO_ASSET_VIDEO_MIN_DURATION_MS",
    "VOLCANO_ASSET_VIDEO_MIN_PIXELS",
    "VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE",
    "VolcanoAssetImageJpeg",
    "VolcanoAssetInstallReceipt",
    "VolcanoAssetMediaError",
    "VolcanoAssetVideoMp4",
    "delete_volcano_asset_install",
    "ensure_volcano_asset_image_variant",
    "ensure_volcano_asset_video_variant",
    "make_volcano_asset_image_jpeg",
    "make_volcano_asset_video_mp4",
    "volcano_asset_image_key",
    "volcano_asset_video_key",
    "volcano_asset_video_variant_metadata",
]
