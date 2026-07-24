"""Bounded, temporary-file-backed image upload processing."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from PIL import Image as PILImage, ImageOps, UnidentifiedImageError

from lumen_core.image_reference import (
    DEFAULT_REFERENCE_MAX_SIDE,
    ImageReferenceError,
    MaskPreflight,
    MaskPreflightError,
    validate_mask_preflight,
)

logger = logging.getLogger(__name__)

UPLOAD_CHUNK_SIZE = 256 * 1024
DEFAULT_UPLOAD_MAX_CONCURRENCY = 4
DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES = 200 * 1024 * 1024
DEFAULT_UPLOAD_MAX_INFLIGHT_PIXELS = 128_000_000
REFERENCE_VARIANT_MIME = "image/webp"

_LINK_UNSUPPORTED_ERRNOS = {
    errno.EXDEV,
    errno.EPERM,
    errno.EACCES,
    getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    errno.EOPNOTSUPP,
}


class UploadPipelineError(ValueError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("non-positive %s=%r; using default %d", name, raw, default)
        return default
    return value


@dataclass(frozen=True)
class UploadBudgetLimits:
    max_concurrency: int
    max_inflight_bytes: int
    max_inflight_pixels: int

    @classmethod
    def from_env(cls) -> "UploadBudgetLimits":
        return cls(
            max_concurrency=_positive_env(
                "LUMEN_IMAGE_UPLOAD_MAX_CONCURRENCY",
                DEFAULT_UPLOAD_MAX_CONCURRENCY,
            ),
            max_inflight_bytes=_positive_env(
                "LUMEN_IMAGE_UPLOAD_MAX_INFLIGHT_BYTES",
                DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES,
            ),
            max_inflight_pixels=_positive_env(
                "LUMEN_IMAGE_UPLOAD_MAX_INFLIGHT_PIXELS",
                DEFAULT_UPLOAD_MAX_INFLIGHT_PIXELS,
            ),
        )


@dataclass(frozen=True)
class UploadBudgetSnapshot:
    active_uploads: int
    inflight_bytes: int
    inflight_pixels: int


class UploadBudget:
    """Fail-fast process-local admission and weighted resource accounting."""

    def __init__(self, limits: UploadBudgetLimits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._active_uploads = 0
        self._inflight_bytes = 0
        self._inflight_pixels = 0

    def acquire(self) -> "UploadLease":
        with self._lock:
            if self._active_uploads >= self.limits.max_concurrency:
                raise UploadPipelineError(
                    "upload_capacity_exceeded",
                    "image upload capacity is temporarily exhausted",
                    503,
                )
            self._active_uploads += 1
        return UploadLease(self)

    def _reserve_bytes(self, amount: int) -> None:
        with self._lock:
            if self._inflight_bytes + amount > self.limits.max_inflight_bytes:
                raise UploadPipelineError(
                    "upload_bytes_capacity_exceeded",
                    "image upload byte capacity is temporarily exhausted",
                    503,
                )
            self._inflight_bytes += amount

    def _reserve_pixels(self, amount: int) -> None:
        with self._lock:
            if self._inflight_pixels + amount > self.limits.max_inflight_pixels:
                raise UploadPipelineError(
                    "upload_pixels_capacity_exceeded",
                    "image upload pixel capacity is temporarily exhausted",
                    503,
                )
            self._inflight_pixels += amount

    def _release(self, *, reserved_bytes: int, reserved_pixels: int) -> None:
        with self._lock:
            self._active_uploads -= 1
            self._inflight_bytes -= reserved_bytes
            self._inflight_pixels -= reserved_pixels

    def snapshot(self) -> UploadBudgetSnapshot:
        with self._lock:
            return UploadBudgetSnapshot(
                active_uploads=self._active_uploads,
                inflight_bytes=self._inflight_bytes,
                inflight_pixels=self._inflight_pixels,
            )


class UploadLease:
    def __init__(self, budget: UploadBudget) -> None:
        self._budget = budget
        self.reserved_bytes = 0
        self.reserved_pixels = 0
        self._released = False

    def reserve_bytes(self, amount: int) -> None:
        if amount <= 0:
            return
        if self._released:
            raise RuntimeError("upload lease has been released")
        self._budget._reserve_bytes(amount)
        self.reserved_bytes += amount

    def reserve_pixels(self, amount: int) -> None:
        if amount <= 0:
            return
        if self._released:
            raise RuntimeError("upload lease has been released")
        self._budget._reserve_pixels(amount)
        self.reserved_pixels += amount

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._budget._release(
            reserved_bytes=self.reserved_bytes,
            reserved_pixels=self.reserved_pixels,
        )


_PROCESS_UPLOAD_BUDGET = UploadBudget(UploadBudgetLimits.from_env())


def process_upload_budget() -> UploadBudget:
    return _PROCESS_UPLOAD_BUDGET


def _secure_temp_dir(storage_root: str | Path) -> Path:
    root = Path(storage_root).resolve()
    temp_dir = root / ".upload-tmp"
    temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = temp_dir.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UploadPipelineError(
            "upload_temp_unavailable",
            "image upload temporary storage is unavailable",
            503,
        )
    return temp_dir


def _new_temp_file(temp_dir: Path, *, suffix: str) -> tuple[int, Path]:
    fd, name = tempfile.mkstemp(
        prefix="lumen-image-upload-",
        suffix=suffix,
        dir=str(temp_dir),
    )
    os.fchmod(fd, 0o600)
    return fd, Path(name)


def _write_fd_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while staging image upload")
        view = view[written:]


@dataclass
class StagedUpload:
    path: Path
    size_bytes: int
    sha256: str
    lease: UploadLease
    temp_dir: Path
    _owned_paths: set[Path]

    def new_temp_path(self, *, suffix: str) -> Path:
        fd, path = _new_temp_file(self.temp_dir, suffix=suffix)
        os.close(fd)
        self._owned_paths.add(path)
        return path


@asynccontextmanager
async def stage_upload(
    upload_file: Any,
    *,
    storage_root: str | Path,
    max_bytes: int,
    budget: UploadBudget | None = None,
) -> AsyncIterator[StagedUpload]:
    import asyncio

    active_budget = budget or process_upload_budget()
    lease = active_budget.acquire()
    owned_paths: set[Path] = set()
    fd: int | None = None
    try:
        temp_dir = await asyncio.to_thread(_secure_temp_dir, storage_root)
        fd, path = await asyncio.to_thread(
            _new_temp_file,
            temp_dir,
            suffix=".source",
        )
        owned_paths.add(path)
        size = 0
        digest = hashlib.sha256()
        while True:
            chunk = await upload_file.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            next_size = size + len(chunk)
            if next_size > max_bytes:
                raise UploadPipelineError(
                    "too_large",
                    f"file exceeds {max_bytes // (1024 * 1024)}MB",
                    413,
                )
            lease.reserve_bytes(len(chunk))
            digest.update(chunk)
            await asyncio.to_thread(_write_fd_all, fd, chunk)
            size = next_size
        if size == 0:
            raise UploadPipelineError("empty_file", "empty file", 400)
        await asyncio.to_thread(os.fsync, fd)
        os.close(fd)
        fd = None
        yield StagedUpload(
            path=path,
            size_bytes=size,
            sha256=digest.hexdigest(),
            lease=lease,
            temp_dir=temp_dir,
            _owned_paths=owned_paths,
        )
    finally:
        if fd is not None:
            os.close(fd)
        for path in tuple(owned_paths):
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                logger.warning(
                    "failed to clean image upload temporary file path=%s",
                    path,
                    exc_info=True,
                )
        lease.release()


@dataclass(frozen=True)
class PreparedUpload:
    original_path: Path
    mime: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    metadata: dict[str, Any]
    normalized_ref_path: Path
    normalized_ref_meta: dict[str, Any]


def _image_mime_type(image: PILImage.Image) -> str:
    custom_mimetype = getattr(image, "custom_mimetype", None)
    if isinstance(custom_mimetype, str) and custom_mimetype:
        return custom_mimetype.lower()
    image_format = image.format
    if not isinstance(image_format, str):
        return ""
    mime = PILImage.MIME.get(image_format.upper())
    return mime.lower() if isinstance(mime, str) else ""


def _too_many_pixels(max_pixels: int) -> UploadPipelineError:
    return UploadPipelineError(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({max_pixels} pixels)",
        413,
    )


def _enforce_dimensions(
    size: tuple[int, int],
    *,
    max_pixels: int,
    max_long_side: int,
) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise UploadPipelineError("invalid_image", "invalid image size", 400)
    if width * height > max_pixels:
        raise _too_many_pixels(max_pixels)
    if max(width, height) > max_long_side:
        raise UploadPipelineError(
            "too_large",
            f"image long side exceeds {max_long_side}px",
            413,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(UPLOAD_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _reserve_file_bytes(staged: StagedUpload, path: Path, *, max_bytes: int) -> int:
    size = path.stat().st_size
    if size > max_bytes:
        raise UploadPipelineError(
            "too_large",
            f"file exceeds {max_bytes // (1024 * 1024)}MB",
            413,
        )
    staged.lease.reserve_bytes(size)
    return size


def _normalize_to_jpeg(
    source_path: Path,
    output_path: Path,
) -> tuple[int, int]:
    with PILImage.open(source_path) as original:
        normalized = ImageOps.exif_transpose(original)
        width, height = normalized.size
        if "A" in normalized.getbands() or "transparency" in normalized.info:
            rgba = normalized.convert("RGBA")
            flattened = PILImage.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.getchannel("A"))
            flattened.save(output_path, format="JPEG", quality=95)
        else:
            rgb = normalized if normalized.mode == "RGB" else normalized.convert("RGB")
            rgb.save(output_path, format="JPEG", quality=95)
    return width, height


def _has_transparency(image: PILImage.Image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _make_reference_variant_file(
    source_path: Path,
    output_path: Path,
    *,
    max_side: int,
) -> tuple[int, int]:
    with PILImage.open(source_path) as original:
        image = ImageOps.exif_transpose(original)
        width, height = image.size
        if max(width, height) > max_side:
            ratio = max_side / max(width, height)
            image = image.resize(
                (
                    max(1, int(round(width * ratio))),
                    max(1, int(round(height * ratio))),
                ),
                PILImage.Resampling.LANCZOS,
            )
        target_mode = "RGBA" if _has_transparency(image) else "RGB"
        if image.mode != target_mode:
            image = image.convert(target_mode)
        image.save(output_path, format="WEBP", quality=90, method=4)
    with PILImage.open(output_path) as check:
        return check.size


def _analyze_mask_file(
    path: Path,
    *,
    reference_size: tuple[int, int] | None,
) -> MaskPreflight:
    with PILImage.open(path) as image:
        mime = (image.get_format_mimetype() or "").lower() or None
        width, height = image.size
        mode = image.mode
        bands = image.getbands()
        has_alpha = "A" in bands or "transparency" in image.info
        alpha_min: int | None = None
        alpha_max: int | None = None
        repaint_ratio: float | None = None
        alpha_is_binary: bool | None = None
        is_empty: bool | None = None
        is_full: bool | None = None
        if has_alpha:
            alpha_source = image.convert("RGBA") if "A" not in bands else image
            alpha = alpha_source.getchannel("A")
            extrema = alpha.getextrema()
            if extrema is not None:
                alpha_min, alpha_max = int(extrema[0]), int(extrema[1])
            histogram = alpha.histogram()
            total = max(1, width * height)
            transparent = int(histogram[0])
            opaque = int(histogram[255])
            repaint_ratio = transparent / total
            alpha_is_binary = transparent + opaque == total
            is_empty = transparent == 0
            is_full = transparent == total

        luminance = image.convert("L")
        luminance_extrema = luminance.getextrema()
        luminance_min, luminance_max = (
            (
                int(luminance_extrema[0]),
                int(luminance_extrema[1]),
            )
            if luminance_extrema is not None
            else (None, None)
        )
        reference_width: int | None = None
        reference_height: int | None = None
        size_matches_reference: bool | None = None
        if reference_size is not None:
            reference_width, reference_height = reference_size
            size_matches_reference = (width, height) == reference_size

    return MaskPreflight(
        width=width,
        height=height,
        mime=mime,
        mode=mode,
        has_alpha=has_alpha,
        has_luminance=luminance_min != luminance_max,
        alpha_min=alpha_min,
        alpha_max=alpha_max,
        luminance_min=luminance_min,
        luminance_max=luminance_max,
        repaint_ratio=repaint_ratio,
        alpha_is_binary=alpha_is_binary,
        is_empty=is_empty,
        is_full=is_full,
        reference_width=reference_width,
        reference_height=reference_height,
        size_matches_reference=size_matches_reference,
    )


def prepare_image_upload(
    staged: StagedUpload,
    filename: str | None,
    *,
    allowed_mime: set[str],
    normalizable_mime: set[str],
    max_bytes: int,
    max_pixels: int,
    max_long_side: int,
    mask_requested: bool = False,
    reference_size: tuple[int, int] | None = None,
    metadata_reader: Callable[[PILImage.Image, str | None], dict[str, Any]]
    | None = None,
) -> PreparedUpload:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(staged.path) as probe:
                width, height = probe.size
                _enforce_dimensions(
                    (width, height),
                    max_pixels=max_pixels,
                    max_long_side=max_long_side,
                )
                staged.lease.reserve_pixels(width * height)
                probe.verify()

            with PILImage.open(staged.path) as image:
                mime = _image_mime_type(image)
                metadata = (
                    metadata_reader(image, filename)
                    if metadata_reader is not None
                    else {}
                )

            source_mime = mime
            original_path = staged.path
            size_bytes = staged.size_bytes
            sha256 = staged.sha256
            if mime in allowed_mime:
                pass
            elif mime in normalizable_mime:
                original_path = staged.new_temp_path(suffix=".normalized.jpg")
                width, height = _normalize_to_jpeg(staged.path, original_path)
                size_bytes = _reserve_file_bytes(
                    staged,
                    original_path,
                    max_bytes=max_bytes,
                )
                sha256 = _sha256_file(original_path)
                mime = "image/jpeg"
                metadata["upload_normalized"] = {
                    "source_mime": source_mime,
                    "target_mime": mime,
                    "reason": "unsupported_upload_mime",
                }
            else:
                raise UploadPipelineError(
                    "unsupported_mime",
                    f"mime not allowed: {mime}",
                    400,
                )

            normalized_ref_path = staged.new_temp_path(suffix=".ref.webp")
            normalized_width, normalized_height = _make_reference_variant_file(
                original_path,
                normalized_ref_path,
                max_side=DEFAULT_REFERENCE_MAX_SIDE,
            )
            normalized_size = _reserve_file_bytes(
                staged,
                normalized_ref_path,
                max_bytes=max_bytes,
            )
            normalized_sha256 = _sha256_file(normalized_ref_path)
            mask_preflight = _analyze_mask_file(
                original_path,
                reference_size=reference_size,
            )
            if mask_requested:
                validate_mask_preflight(mask_preflight)
    except UploadPipelineError:
        raise
    except (PILImage.DecompressionBombError, PILImage.DecompressionBombWarning) as exc:
        raise _too_many_pixels(max_pixels) from exc
    except MaskPreflightError as exc:
        raise UploadPipelineError(exc.code, exc.message, exc.status_code) from exc
    except ImageReferenceError as exc:
        raise UploadPipelineError(exc.code, exc.message, exc.status_code) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UploadPipelineError("invalid_image", "unreadable image", 400) from exc

    metadata = {
        **metadata,
        "mask_preflight": mask_preflight.to_metadata(),
    }
    return PreparedUpload(
        original_path=original_path,
        mime=mime,
        width=width,
        height=height,
        size_bytes=size_bytes,
        sha256=sha256,
        metadata=metadata,
        normalized_ref_path=normalized_ref_path,
        normalized_ref_meta={
            "mime": REFERENCE_VARIANT_MIME,
            "width": normalized_width,
            "height": normalized_height,
            "sha256": normalized_sha256,
            "bytes": normalized_size,
            "max_side": DEFAULT_REFERENCE_MAX_SIDE,
        },
    )


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


def _unlink_if_same_file(path: Path, expected: os.stat_result) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
        path.unlink(missing_ok=True)


def publish_temp_file(source: Path, destination: Path) -> None:
    """Publish without overwrite; prefer a no-copy hard link on the same volume."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
        _fsync_directory(destination.parent)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
            raise

    fd = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    created = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            with source.open("rb") as source_handle:
                shutil.copyfileobj(source_handle, output, length=UPLOAD_CHUNK_SIZE)
                output.flush()
                os.fsync(output.fileno())
        _fsync_directory(destination.parent)
    except Exception:
        if fd >= 0:
            os.close(fd)
        _unlink_if_same_file(destination, created)
        raise
