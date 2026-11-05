"""Public shared GIF workflow helpers; never import route aggregators."""

from __future__ import annotations
import hashlib
import logging
import re
from fastapi import UploadFile
from lumen_core.gif_editing import MAX_GIF_BYTES
from ...deps import durable_session_id
from ...ratelimit import UPLOADS_LIMITER
from ...redis_client import get_redis
from ...services.active_user import (
    ensure_active_user,
    ActiveUserFenceError,
    active_user_fence_http_error,
)
from .http_route_parts.storage import (
    storage_path,
    open_regular_file_no_symlink,
    ensure_storage_free_space,
)
from .http_route_parts.upload import http_error

logger = logging.getLogger(__name__)


async def check_upload_rate_limit(user_id: str) -> None:
    await UPLOADS_LIMITER.check(get_redis(), f"rl:upload:{user_id}")


async def check_active_user(db, user, request) -> None:
    try:
        await ensure_active_user(db, user.id, session_id=durable_session_id(request))
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc


def validate_operation_key(value: str | None) -> str:
    if value is None or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
        raise http_error(
            "invalid_idempotency_key",
            "Idempotency-Key must contain 8–128 letters, digits, dots, colons, underscores or hyphens",
            400,
        )
    return value


async def upload_digest(file: UploadFile) -> str:
    """Fingerprint a spooled upload without retaining its full contents."""
    digest = hashlib.sha256()
    size = 0
    await file.seek(0)
    while chunk := await file.read(256 * 1024):
        size += len(chunk)
        if size > MAX_GIF_BYTES:
            raise http_error("too_large", "file exceeds 32 MiB", 413)
        digest.update(chunk)
    await file.seek(0)
    return digest.hexdigest()


def read_gif_source(image) -> bytes:
    path = storage_path(image.storage_key, error_factory=http_error)
    file, size = open_regular_file_no_symlink(path)
    with file:
        if size > MAX_GIF_BYTES:
            raise ValueError("GIF exceeds 32 MiB")
        data = file.read(MAX_GIF_BYTES + 1)
    if len(data) > MAX_GIF_BYTES:
        raise ValueError("GIF exceeds 32 MiB")
    if hashlib.sha256(data).hexdigest() != image.sha256:
        raise ValueError("GIF source integrity check failed")
    return data


def ensure_gif_storage_space(incoming_bytes: int) -> None:
    ensure_storage_free_space(incoming_bytes, error_factory=http_error, logger=logger)
