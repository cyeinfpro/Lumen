"""Video upstream image reference variants."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, ImageVariant


VIDEO_REFERENCE_IMAGE_KIND = "video_ref_2048_jpg"
VIDEO_REFERENCE_IMAGE_MAX_SIDE = 2048
VIDEO_REFERENCE_IMAGE_MIME = "image/jpeg"
VIDEO_REFERENCE_IMAGE_QUALITY = 88
MAX_IMAGE_PIXELS = 64_000_000
_LINK_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.EXDEV,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}

try:
    PILImage.MAX_IMAGE_PIXELS = max(
        int(PILImage.MAX_IMAGE_PIXELS or 0), MAX_IMAGE_PIXELS
    )
except Exception:
    PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class VideoReferenceImageError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class VideoReferenceJpeg:
    data: bytes
    width: int
    height: int
    sha256: str


def _storage_path(storage_root: str, storage_key: str) -> Path:
    root = Path(storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise VideoReferenceImageError("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise VideoReferenceImageError(
            "invalid_path", "absolute storage paths are not allowed", 400
        )
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise VideoReferenceImageError(
            "invalid_path", "storage path escapes root", 400
        ) from exc
    return path


def video_reference_image_key(image: Image) -> str:
    source = Path(image.storage_key)
    return str(source.with_name(f"{image.id}.{VIDEO_REFERENCE_IMAGE_KIND}.jpg"))


def _enforce_pixel_limit(size: tuple[int, int]) -> None:
    width, height = size
    if width * height > MAX_IMAGE_PIXELS:
        raise VideoReferenceImageError(
            "too_many_pixels",
            f"image exceeds safe pixel limit ({MAX_IMAGE_PIXELS} pixels)",
            413,
        )


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


def make_video_reference_jpeg(
    source_path: Path,
    *,
    max_side: int = VIDEO_REFERENCE_IMAGE_MAX_SIDE,
) -> VideoReferenceJpeg:
    try:
        opened = PILImage.open(source_path)
    except PILImage.DecompressionBombError as exc:
        raise VideoReferenceImageError(
            "too_many_pixels",
            f"image exceeds safe pixel limit ({MAX_IMAGE_PIXELS} pixels)",
            413,
        ) from exc
    except UnidentifiedImageError as exc:
        raise VideoReferenceImageError(
            "invalid_image", "unreadable image", 400
        ) from exc

    with opened:
        _enforce_pixel_limit(opened.size)
        opened.load()
        image = ImageOps.exif_transpose(opened)
        try:
            image.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                rgba = image.convert("RGBA")
                try:
                    background = PILImage.new("RGBA", rgba.size, (255, 255, 255, 255))
                    background.alpha_composite(rgba)
                    rgb = background.convert("RGB")
                finally:
                    rgba.close()
            else:
                rgb = image.convert("RGB")
            try:
                buf = io.BytesIO()
                try:
                    rgb.save(
                        buf,
                        format="JPEG",
                        quality=VIDEO_REFERENCE_IMAGE_QUALITY,
                        optimize=True,
                    )
                except OSError:
                    buf = io.BytesIO()
                    rgb.save(
                        buf,
                        format="JPEG",
                        quality=VIDEO_REFERENCE_IMAGE_QUALITY,
                    )
                data = buf.getvalue()
                return VideoReferenceJpeg(
                    data=data,
                    width=rgb.width,
                    height=rgb.height,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            finally:
                rgb.close()
        finally:
            if image is not opened:
                image.close()


async def ensure_video_reference_image_variant(
    db: AsyncSession,
    image: Image,
    *,
    storage_root: str,
) -> ImageVariant:
    image = (
        await db.execute(
            select(Image)
            .where(
                Image.id == image.id,
                Image.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none() or image

    existing = (
        await db.execute(
            select(ImageVariant)
            .where(
                ImageVariant.image_id == image.id,
                ImageVariant.kind == VIDEO_REFERENCE_IMAGE_KIND,
            )
            .order_by(ImageVariant.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        existing is not None
        and _storage_path(storage_root, existing.storage_key).is_file()
    ):
        return existing

    source_path = _storage_path(storage_root, image.storage_key)
    if not source_path.is_file():
        raise VideoReferenceImageError("not_found", "binary missing", 404)

    rendered = await asyncio.to_thread(make_video_reference_jpeg, source_path)
    key = video_reference_image_key(image)
    destination = _storage_path(storage_root, key)
    try:
        await asyncio.to_thread(_write_new_file_atomic, destination, rendered.data)
    except FileExistsError:
        pass

    if existing is not None:
        existing.storage_key = key
        existing.width = rendered.width
        existing.height = rendered.height
        return existing

    variant = ImageVariant(
        image_id=image.id,
        kind=VIDEO_REFERENCE_IMAGE_KIND,
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
            await db.execute(
                select(ImageVariant)
                .where(
                    ImageVariant.image_id == image.id,
                    ImageVariant.kind == VIDEO_REFERENCE_IMAGE_KIND,
                )
                .order_by(ImageVariant.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if winner is not None:
            return winner
        raise
    return variant
