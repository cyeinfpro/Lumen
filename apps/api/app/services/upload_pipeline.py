"""Compatibility facade for bounded image upload staging and processing."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil as _shutil
import stat
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from PIL import Image as PILImage

from ..images.adapters.filesystem_store import publish_file_sync
from ..images.processing.metadata import image_mime_type as _default_image_mime_type
from ..images.processing.service import (
    ImageProcessor,
    PreparedUpload,
    ProcessingError,
)


logger = logging.getLogger(__name__)
shutil = _shutil  # compatibility: callers monkeypatch the copy fallback

UPLOAD_CHUNK_SIZE = 256 * 1024
DEFAULT_UPLOAD_MAX_CONCURRENCY = 4
DEFAULT_UPLOAD_MAX_INFLIGHT_BYTES = 200 * 1024 * 1024
DEFAULT_UPLOAD_MAX_INFLIGHT_PIXELS = 128_000_000


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
    """Second-layer process-local guard retained for compatibility and defense."""

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


_PROCESSOR = ImageProcessor()
_image_mime_type = _default_image_mime_type


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
        return _PROCESSOR.process(
            staged,
            filename,
            allowed_mime=allowed_mime,
            normalizable_mime=normalizable_mime,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            max_long_side=max_long_side,
            mask_requested=mask_requested,
            reference_size=reference_size,
            metadata_reader=metadata_reader,
            mime_resolver=_image_mime_type,
        )
    except ProcessingError as exc:
        raise UploadPipelineError(exc.code, exc.message, exc.status_code) from exc


def publish_temp_file(source: Path, destination: Path) -> None:
    publish_file_sync(source, destination)
