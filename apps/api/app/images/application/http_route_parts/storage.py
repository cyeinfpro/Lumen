from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from fastapi import HTTPException, Request, Response

from ....config import settings
from ....services import storage_files
from .._file_delivery import (
    etag_matches_if_none_match,
    internal_redirect_enabled,
    iter_open_file_and_close as iter_delivery_file_and_close,
    open_regular_file_no_symlink,
    storage_streaming_response as build_storage_streaming_response,
)
from ..deliver import DeliverySpec, deliver_artifact


LINK_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.EPERM,
        errno.EACCES,
        errno.EXDEV,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        errno.EOPNOTSUPP,
    }
)
MIN_STORAGE_FREE_BYTES = 512 * 1024 * 1024


def storage_path(
    storage_key: str,
    *,
    error_factory: Callable[..., HTTPException],
) -> Path:
    return storage_files.resolve_storage_path(
        settings.storage_root,
        storage_key,
        error_factory=error_factory,
    )


def storage_usage_path(root: Path) -> Path:
    current = root
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def minimum_storage_free_bytes(logger: logging.Logger) -> int:
    raw = os.environ.get("LUMEN_MIN_STORAGE_FREE_BYTES", "").strip()
    if not raw:
        return MIN_STORAGE_FREE_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("invalid LUMEN_MIN_STORAGE_FREE_BYTES=%r; using default", raw)
        return MIN_STORAGE_FREE_BYTES


def ensure_storage_free_space(
    incoming_bytes: int,
    *,
    error_factory: Callable[..., HTTPException],
    logger: logging.Logger,
) -> None:
    root = Path(settings.storage_root).resolve()
    usage = shutil.disk_usage(storage_usage_path(root))
    required = max(0, incoming_bytes) + minimum_storage_free_bytes(logger)
    if usage.free < required:
        raise error_factory(
            "storage_insufficient_space",
            "not enough free storage to accept this upload",
            507,
        )


def fsync_directory(path: Path) -> None:
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


def write_new_file_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(tmp, path)
            fsync_directory(path.parent)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise
            if exc.errno not in LINK_UNSUPPORTED_ERRNOS:
                raise
            write_new_file_exclusive(path, data)
    finally:
        tmp.unlink(missing_ok=True)


def write_new_file_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        fsync_directory(path.parent)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def unlink_file_if_exists(path: Path, *, logger: logging.Logger) -> None:
    try:
        path.unlink(missing_ok=True)
        fsync_directory(path.parent)
    except OSError:
        logger.warning(
            "failed to remove orphan upload file path=%s", path, exc_info=True
        )


def iter_open_file_and_close(file: BinaryIO) -> Iterator[bytes]:
    yield from iter_delivery_file_and_close(file)


def storage_streaming_response(
    path: Path,
    *,
    media_type: str,
    etag: str,
    cache_control: str,
    validate_storage_key: Callable[[str], Path],
    storage_key: str | None = None,
    request: Request | None = None,
    inline_filename: str | None = None,
) -> Response:
    return deliver_artifact(
        DeliverySpec(
            path=path,
            storage_key=storage_key,
            media_type=media_type,
            etag=etag,
            cache_control=cache_control,
            inline_filename=inline_filename,
        ),
        request=request,
        response_builder=lambda delivery_path, **kwargs: (
            build_storage_streaming_response(
                delivery_path,
                **kwargs,
                etag_matches=etag_matches_if_none_match,
                validate_storage_key=validate_storage_key,
                open_file=open_regular_file_no_symlink,
                iter_file=iter_open_file_and_close,
                redirect_enabled=internal_redirect_enabled,
            )
        ),
    )
