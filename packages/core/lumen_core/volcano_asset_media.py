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
import weakref
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.capacity_leases import (
    CapacityLeaseGuard,
    CapacityLeaseLost,
    maintained_capacity_lease,
    race_with_capacity_lease,
)
from lumen_core.models import Image, ImageVariant, User, Video
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


async def _wait_for_started_install(task: asyncio.Task[bool]) -> bool:
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


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
    except OSError:
        logger.warning(
            "Volcano asset media cleanup failed key=%s",
            None if receipt is None else receipt.storage_key,
            exc_info=True,
        )


async def _install_rendered_media(
    *,
    storage_root: str,
    storage_key: str,
    data: bytes,
    sha256: str,
    guard: CapacityLeaseGuard,
) -> VolcanoAssetInstallReceipt | None:
    destination = _storage_path(storage_root, storage_key)
    install_task = asyncio.create_task(
        asyncio.to_thread(
            _install_file_atomic,
            destination,
            data,
            sha256=sha256,
        )
    )
    created = False
    try:
        created = bool(
            await race_with_capacity_lease(
                asyncio.shield(install_task),
                guard,
            )
        )
    except BaseException:
        try:
            created = await _wait_for_started_install(install_task)
        except BaseException:
            created = False
        if created:
            await _cleanup_install_best_effort(
                storage_root,
                VolcanoAssetInstallReceipt(
                    storage_key=storage_key,
                    size_bytes=len(data),
                    sha256=sha256,
                ),
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


async def ensure_volcano_asset_image_variant(
    db: AsyncSession,
    image: Image,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
) -> tuple[ImageVariant, VolcanoAssetInstallReceipt | None]:
    current_image = (
        await db.execute(
            select(Image).where(
                Image.id == image.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if current_image is None:
        raise VolcanoAssetMediaError("not_found", "image was deleted", 404)
    image = current_image
    existing = (
        await db.execute(
            select(ImageVariant)
            .where(
                ImageVariant.image_id == image.id,
                ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
            )
            .order_by(ImageVariant.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_path = _storage_path(storage_root, existing.storage_key)
        if await asyncio.to_thread(
            _image_variant_file_is_valid,
            existing_path,
            width=existing.width,
            height=existing.height,
        ):
            return existing, None

    source_path = _storage_path(storage_root, image.storage_key)
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "image binary is missing", 404)
    rendered = await asyncio.to_thread(
        make_volcano_asset_image_jpeg,
        source_path,
    )
    try:
        storage_lease = await _reserve_media_capacity(
            storage_capacity,
            rendered.size_bytes,
        )
        async with maintained_capacity_lease(
            storage_lease,
            ttl_seconds=storage_lease_ttl_seconds,
        ) as guard:
            current_image = (
                await race_with_capacity_lease(
                    db.execute(
                        select(Image)
                        .where(
                            Image.id == image.id,
                            Image.deleted_at.is_(None),
                        )
                        .with_for_update()
                    ),
                    guard,
                )
            ).scalar_one_or_none()
            if current_image is None:
                raise VolcanoAssetMediaError("not_found", "image was deleted", 404)
            image = current_image
            existing = (
                await race_with_capacity_lease(
                    db.execute(
                        select(ImageVariant)
                        .where(
                            ImageVariant.image_id == image.id,
                            ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
                        )
                        .order_by(ImageVariant.created_at.desc())
                        .limit(1)
                        .with_for_update()
                    ),
                    guard,
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing_path = _storage_path(storage_root, existing.storage_key)
                is_valid = await race_with_capacity_lease(
                    asyncio.to_thread(
                        _image_variant_file_is_valid,
                        existing_path,
                        width=existing.width,
                        height=existing.height,
                    ),
                    guard,
                )
                if is_valid:
                    return existing, None

            key = volcano_asset_image_key(image)
            receipt = await _install_rendered_media(
                storage_root=storage_root,
                storage_key=key,
                data=rendered.data,
                sha256=rendered.sha256,
                guard=guard,
            )
            try:
                if existing is not None:
                    existing.storage_key = key
                    existing.width = rendered.width
                    existing.height = rendered.height
                    await guard.assert_owned()
                    return existing, receipt

                variant = ImageVariant(
                    image_id=image.id,
                    kind=VOLCANO_ASSET_IMAGE_KIND,
                    storage_key=key,
                    width=rendered.width,
                    height=rendered.height,
                )
                try:
                    async with db.begin_nested():
                        db.add(variant)
                        await db.flush()
                except IntegrityError:
                    if variant in db:
                        db.expunge(variant)
                    winner = (
                        await race_with_capacity_lease(
                            db.execute(
                                select(ImageVariant)
                                .where(
                                    ImageVariant.image_id == image.id,
                                    ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
                                )
                                .order_by(ImageVariant.created_at.desc())
                                .limit(1)
                            ),
                            guard,
                        )
                    ).scalar_one_or_none()
                    if winner is not None:
                        winner.storage_key = key
                        winner.width = rendered.width
                        winner.height = rendered.height
                        await guard.assert_owned()
                        return winner, receipt
                    raise
                await guard.assert_owned()
                return variant, receipt
            except BaseException:
                await _cleanup_install_best_effort(storage_root, receipt)
                raise
    except CapacityLeaseLost as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_capacity",
            "normalized asset media storage capacity lease was lost",
            503,
        ) from exc


def make_volcano_asset_video_mp4(source_path: Path) -> VolcanoAssetVideoMp4:
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
                    timeout=300,
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
            return VolcanoAssetVideoMp4(
                data=data,
                width=int(output_metadata["width"]),
                height=int(output_metadata["height"]),
                duration_ms=int(output_metadata["duration_ms"]),
                fps=float(output_metadata["fps"]),
                has_audio=bool(output_metadata["has_audio"]),
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
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
    try:
        return max(0, int(raw.get("size_bytes") or 0))
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


async def _locked_reference_storage_usage(
    db: AsyncSession,
    *,
    user_id: str,
    guard: CapacityLeaseGuard,
) -> int:
    cleanup_state = Video.metadata_jsonb[_VIDEO_STORAGE_CLEANUP_METADATA_KEY][
        "state"
    ].as_string()
    upstream_variant_key = Video.metadata_jsonb["upstream_reference_video_variant"][
        "storage_key"
    ].as_string()
    volcano_variant_key = Video.metadata_jsonb[VOLCANO_ASSET_VIDEO_METADATA_KEY][
        "storage_key"
    ].as_string()
    rows = _result_rows(
        await race_with_capacity_lease(
            db.execute(
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
            ),
            guard,
        )
    )
    return sum(_video_reference_declared_bytes(row) for row in rows)


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


async def ensure_volcano_asset_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
    storage_capacity: StorageCapacityPort,
    storage_lease_ttl_seconds: float,
) -> tuple[dict[str, Any], VolcanoAssetInstallReceipt | None]:
    current_video = (
        await db.execute(
            select(Video)
            .where(
                Video.id == video.id,
                Video.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if current_video is None:
        raise VolcanoAssetMediaError("not_found", "video was deleted", 404)
    video = current_video
    existing = volcano_asset_video_variant_metadata(video)
    if existing is not None:
        existing_path = _storage_path(
            storage_root,
            str(existing["storage_key"]),
        )
        if await asyncio.to_thread(
            _video_variant_file_is_valid,
            existing_path,
            existing,
        ):
            return existing, None

    source_path = _storage_path(storage_root, video.storage_key)
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "video binary is missing", 404)
    source_video_id = str(video.id)
    source_user_id = str(video.user_id)
    source_storage_key = str(video.storage_key)
    source_sha256 = str(video.sha256)
    async with _video_transcode_semaphore():
        rendered = await asyncio.to_thread(
            make_volcano_asset_video_mp4,
            source_path,
        )
    try:
        storage_lease = await _reserve_media_capacity(
            storage_capacity,
            rendered.size_bytes,
        )
        async with maintained_capacity_lease(
            storage_lease,
            ttl_seconds=storage_lease_ttl_seconds,
        ) as guard:
            await race_with_capacity_lease(
                db.execute(
                    select(User.id).where(User.id == source_user_id).with_for_update()
                ),
                guard,
            )
            current_video = (
                await race_with_capacity_lease(
                    db.execute(
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
                    ),
                    guard,
                )
            ).scalar_one_or_none()
            if current_video is None:
                raise VolcanoAssetMediaError(
                    "video_reference_changed",
                    "video changed or was deleted during asset preparation",
                    409,
                )
            video = current_video
            existing = volcano_asset_video_variant_metadata(video)
            if existing is not None:
                existing_path = _storage_path(
                    storage_root,
                    str(existing["storage_key"]),
                )
                is_valid = await race_with_capacity_lease(
                    asyncio.to_thread(
                        _video_variant_file_is_valid,
                        existing_path,
                        existing,
                    ),
                    guard,
                )
                if is_valid:
                    return existing, None

            current_bytes = await _locked_reference_storage_usage(
                db,
                user_id=source_user_id,
                guard=guard,
            )
            replaced_bytes = _video_variant_quota_bytes(
                video,
                VOLCANO_ASSET_VIDEO_METADATA_KEY,
            )
            projected_bytes = (
                current_bytes - min(current_bytes, replaced_bytes)
            ) + rendered.size_bytes
            if (
                projected_bytes > VIDEO_REFERENCE_STORAGE_QUOTA_BYTES
                and projected_bytes > current_bytes
            ):
                raise VolcanoAssetMediaError(
                    "reference_video_quota_exceeded",
                    "reference video storage quota exceeded",
                    429,
                )

            key = volcano_asset_video_key(video)
            receipt = await _install_rendered_media(
                storage_root=storage_root,
                storage_key=key,
                data=rendered.data,
                sha256=rendered.sha256,
                guard=guard,
            )
            try:
                variant = {
                    "kind": VOLCANO_ASSET_VIDEO_KIND,
                    "storage_key": key,
                    "mime": VOLCANO_ASSET_VIDEO_MIME,
                    "width": rendered.width,
                    "height": rendered.height,
                    "duration_ms": rendered.duration_ms,
                    "fps": rendered.fps,
                    "has_audio": rendered.has_audio,
                    "size_bytes": rendered.size_bytes,
                    "sha256": rendered.sha256,
                }
                metadata = dict(video.metadata_jsonb or {})
                metadata[VOLCANO_ASSET_VIDEO_METADATA_KEY] = variant
                video.metadata_jsonb = metadata
                await guard.assert_owned()
                return variant, receipt
            except BaseException:
                await _cleanup_install_best_effort(storage_root, receipt)
                raise
    except CapacityLeaseLost as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_capacity",
            "normalized asset media storage capacity lease was lost",
            503,
        ) from exc


__all__ = [
    "VOLCANO_ASSET_IMAGE_KIND",
    "VOLCANO_ASSET_IMAGE_MAX_BYTES",
    "VOLCANO_ASSET_IMAGE_MAX_SIDE",
    "VOLCANO_ASSET_IMAGE_MIME",
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
