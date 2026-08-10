from __future__ import annotations

import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fastapi import HTTPException, UploadFile
from PIL import Image as PILImage
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_image_metadata import model_image_filename
from lumen_core.model_entities import Image
from lumen_core.schema_models import ImageOut

from ...domain.variants import deterministic_variant_key
from ...processing.model_metadata import MODEL_LIBRARY_METADATA_PROFILE
from ..create_variant import VariantError, make_display_variant
from ..upload import UploadCommandError, UploadCommandService, UploadPolicy


logger = logging.getLogger(__name__)

MAX_BYTES = 50 * 1024 * 1024
MAX_LONG_SIDE = 4096
VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE = 8192
VIDEO_REFERENCE_UPLOAD_PURPOSE = "video_reference"
VIDEO_REFERENCE_UPLOAD_MAX_BYTES = 30 * 1024 * 1024 - 1
VIDEO_REFERENCE_UPLOAD_MIN_SIDE = 300
VIDEO_REFERENCE_UPLOAD_MAX_LONG_SIDE = 6000
VIDEO_REFERENCE_UPLOAD_MIN_ASPECT_RATIO = 0.4
VIDEO_REFERENCE_UPLOAD_MAX_ASPECT_RATIO = 2.5
MAX_IMAGE_PIXELS = 64_000_000
PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/webp"})
EXT_BY_MIME = MappingProxyType(
    {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
)
NORMALIZABLE_UPLOAD_MIME = frozenset({"image/mpo", "image/x-mpo"})
VIDEO_REFERENCE_NORMALIZABLE_UPLOAD_MIME = frozenset(
    {
        *NORMALIZABLE_UPLOAD_MIME,
        "image/bmp",
        "image/tiff",
        "image/gif",
        "image/heic",
        "image/heif",
    }
)


def http_error(code: str, message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def too_many_pixels() -> HTTPException:
    return http_error(
        "too_many_pixels",
        f"image exceeds safe pixel limit ({MAX_IMAGE_PIXELS} pixels)",
        413,
    )


def enforce_pixel_limit(
    size: tuple[int, int],
    *,
    max_long_side: int | None = MAX_LONG_SIDE,
) -> None:
    width, height = size
    if width <= 0 or height <= 0:
        raise http_error("invalid_image", "invalid image size", 400)
    if width * height > MAX_IMAGE_PIXELS:
        raise too_many_pixels()
    if max_long_side is not None and max(width, height) > max_long_side:
        raise http_error(
            "too_large",
            f"image long side exceeds {max_long_side}px",
            413,
        )


def upload_requests_mask_preflight(
    purpose: str | None,
    filename: str | None,
) -> bool:
    purpose_norm = (purpose or "").strip().lower()
    if purpose_norm == VIDEO_REFERENCE_UPLOAD_PURPOSE:
        return False
    if purpose_norm in {"mask", "inpaint_mask", "inpaint-mask"}:
        return True
    name = Path(filename or "").name.lower()
    stem = Path(name).stem
    return stem == "mask" or stem.startswith("mask_")


def upload_allows_large_dimensions(purpose: str | None) -> bool:
    return (purpose or "").strip().lower() == "volcano_asset"


def build_upload_policy(
    *,
    purpose: str | None,
    filename: str | None,
    reference_size: tuple[int, int] | None,
) -> UploadPolicy:
    purpose_norm = (purpose or "").strip().lower()
    if purpose_norm == VIDEO_REFERENCE_UPLOAD_PURPOSE:
        return UploadPolicy(
            allowed_mime=ALLOWED_MIME,
            normalizable_mime=VIDEO_REFERENCE_NORMALIZABLE_UPLOAD_MIME,
            extensions=EXT_BY_MIME,
            max_bytes=VIDEO_REFERENCE_UPLOAD_MAX_BYTES,
            max_pixels=MAX_IMAGE_PIXELS,
            max_long_side=VIDEO_REFERENCE_UPLOAD_MAX_LONG_SIDE,
            mask_requested=False,
            reference_size=None,
            purpose=VIDEO_REFERENCE_UPLOAD_PURPOSE,
            min_side=VIDEO_REFERENCE_UPLOAD_MIN_SIDE,
            min_aspect_ratio=VIDEO_REFERENCE_UPLOAD_MIN_ASPECT_RATIO,
            max_aspect_ratio=VIDEO_REFERENCE_UPLOAD_MAX_ASPECT_RATIO,
        )
    return UploadPolicy(
        allowed_mime=ALLOWED_MIME,
        normalizable_mime=NORMALIZABLE_UPLOAD_MIME,
        extensions=EXT_BY_MIME,
        max_bytes=MAX_BYTES,
        max_pixels=MAX_IMAGE_PIXELS,
        max_long_side=(
            VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE
            if upload_allows_large_dimensions(purpose_norm)
            else MAX_LONG_SIDE
        ),
        mask_requested=upload_requests_mask_preflight(
            purpose_norm,
            filename,
        ),
        reference_size=reference_size,
        purpose=purpose_norm or None,
    )


def key_for_upload(user_id: str, image_id: str, ext: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.{ext}"


def key_for_normalized_ref(user_id: str, image_id: str) -> str:
    return f"u/{user_id}/uploads/{image_id}.ref.webp"


def variant_key_for_image(img: Image, kind: str) -> str:
    return deterministic_variant_key(
        image_id=img.id,
        source_key=img.storage_key,
        kind=kind,
    )


def make_route_display_variant(
    path: Path,
    max_side: int = 2048,
) -> tuple[bytes, tuple[int, int]]:
    try:
        return make_display_variant(
            path,
            max_pixels=MAX_IMAGE_PIXELS,
            max_side=max_side,
        )
    except VariantError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc


def upload_metadata_finalizer(
    image_id: str,
    extension: str,
    metadata: dict[str, Any],
) -> None:
    model_payload = metadata.get("model_library")
    if not isinstance(model_payload, dict):
        return
    metadata["suggested_filename"] = model_image_filename(
        image_id=image_id,
        ext=extension,
        age_segment=model_payload.get("age_segment"),
        gender=model_payload.get("gender"),
        appearance_direction=model_payload.get("appearance_direction"),
        style_tags=model_payload.get("style_tags") or [],
    )


async def _rollback_request_session(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        logger.exception("failed to roll back image upload request session")


async def upload_image_impl(
    user: Any,
    db: AsyncSession,
    *,
    file: UploadFile,
    purpose: str | None,
    reference_width: int | None,
    reference_height: int | None,
    check_upload_rate_limit: Any,
    ensure_storage_free_space: Any,
    upload_command_service: UploadCommandService,
    image_out: Any,
    session_id: str | None = None,
) -> ImageOut:
    try:
        await check_upload_rate_limit(user.id)
        ensure_storage_free_space(0)
        reference_size = (
            (reference_width, reference_height)
            if isinstance(reference_width, int) and isinstance(reference_height, int)
            else None
        )
        execute_kwargs = {
            "user_id": user.id,
            "upload_file": file,
            "filename": file.filename,
            "policy": build_upload_policy(
                purpose=purpose,
                filename=file.filename,
                reference_size=reference_size,
            ),
            "metadata_profile": MODEL_LIBRARY_METADATA_PROFILE,
            "metadata_finalizer": upload_metadata_finalizer,
            "storage_guard": ensure_storage_free_space,
        }
        if session_id:
            execute_kwargs["session_id"] = session_id
        image = await upload_command_service.execute(
            **execute_kwargs,
        )
        return await image_out(db, image)
    except UploadCommandError as exc:
        await _rollback_request_session(db)
        raise http_error(exc.code, exc.message, exc.status_code) from exc
    except Exception:
        await _rollback_request_session(db)
        raise
