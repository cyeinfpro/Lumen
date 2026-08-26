"""Owned, decoded Agent reference-image validation."""

from __future__ import annotations

import asyncio
import io
import os
import stat
from collections.abc import Awaitable, Callable
from types import MappingProxyType

from fastapi import HTTPException
from PIL import Image as PILImage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Image, User

from ...config import settings
from .. import storage_files
from .common import http_error
from .repository import retention_filter


_REFERENCE_MIME_FORMATS = MappingProxyType(
    {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
    }
)
_REFERENCE_MAX_BYTES = 32 * 1024 * 1024
_REFERENCE_MAX_PIXELS = 50_000_000


def _read_reference_bytes(image: Image) -> bytes:
    path = storage_files.resolve_storage_path(
        settings.storage_root,
        image.storage_key,
        error_factory=lambda code, message, status: http_error(
            "invalid_attachment", message, status, reason=code
        ),
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise http_error(
                "invalid_attachment",
                "reference image artifact is not a regular file",
                422,
            )
        if metadata.st_size > _REFERENCE_MAX_BYTES:
            raise http_error("invalid_attachment", "reference image is too large", 422)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(_REFERENCE_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > _REFERENCE_MAX_BYTES:
        raise http_error("invalid_attachment", "reference image is too large", 422)
    return raw


async def validate_reference_artifact(image: Image) -> None:
    if image.artifact_status != "ready":
        raise http_error(
            "agent_reference_not_ready",
            "reference image is not ready",
            409,
        )
    if (
        image.mime not in _REFERENCE_MIME_FORMATS
        or image.size_bytes < 1
        or image.size_bytes > _REFERENCE_MAX_BYTES
        or image.width < 1
        or image.height < 1
        or image.width * image.height > _REFERENCE_MAX_PIXELS
    ):
        raise http_error(
            "invalid_attachment",
            "reference image metadata is invalid",
            422,
        )
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_read_reference_bytes, image),
            timeout=10,
        )
        with PILImage.open(io.BytesIO(raw)) as decoded:
            if decoded.format != _REFERENCE_MIME_FORMATS[image.mime]:
                raise ValueError("reference image format does not match its MIME type")
            if decoded.width * decoded.height > _REFERENCE_MAX_PIXELS:
                raise ValueError("reference image exceeds the pixel limit")
            decoded.verify()
    except HTTPException:
        raise
    except Exception as exc:
        raise http_error(
            "invalid_attachment",
            "reference image could not be decoded",
            422,
        ) from exc


async def validate_reference_images(
    db: AsyncSession,
    *,
    user: User,
    image_ids: list[str],
    artifact_validator: Callable[
        [Image], Awaitable[None]
    ] = validate_reference_artifact,
) -> None:
    if not image_ids:
        return
    statement = select(Image).where(
        Image.id.in_(image_ids),
        Image.user_id == user.id,
        Image.deleted_at.is_(None),
    )
    visible = await retention_filter(db, user, Image.created_at)
    if visible is not None:
        statement = statement.where(visible)
    rows = list((await db.execute(statement)).scalars().all())
    if {image.id for image in rows} != set(image_ids):
        raise http_error(
            "invalid_attachment",
            "one or more reference images are not owned or were deleted",
            400,
        )
    by_id = {image.id: image for image in rows}
    for image_id in image_ids:
        await artifact_validator(by_id[image_id])


__all__ = ["validate_reference_artifact", "validate_reference_images"]
