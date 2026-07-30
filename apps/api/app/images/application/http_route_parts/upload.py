from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any

from fastapi import HTTPException, UploadFile
from PIL import Image as PILImage
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_image_metadata import model_image_filename
from lumen_core.models import Image
from lumen_core.schemas import ImageOut

from ...domain.variants import deterministic_variant_key
from ...processing.model_metadata import MODEL_LIBRARY_METADATA_PROFILE
from ..create_variant import VariantError, make_display_variant
from ..upload import UploadCommandError, UploadCommandService, UploadPolicy


MAX_BYTES = 50 * 1024 * 1024
MAX_LONG_SIDE = 4096
VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE = 8192
MAX_IMAGE_PIXELS = 64_000_000
PILImage.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/webp"})
EXT_BY_MIME = MappingProxyType(
    {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
)
NORMALIZABLE_UPLOAD_MIME = frozenset({"image/mpo", "image/x-mpo"})


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
    if purpose_norm in {"mask", "inpaint_mask", "inpaint-mask"}:
        return True
    name = Path(filename or "").name.lower()
    stem = Path(name).stem
    return stem == "mask" or stem.startswith("mask_")


def upload_allows_large_dimensions(purpose: str | None) -> bool:
    return (purpose or "").strip().lower() == "volcano_asset"


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
) -> ImageOut:
    await check_upload_rate_limit(user.id)
    try:
        ensure_storage_free_space(0)
        reference_size = (
            (reference_width, reference_height)
            if isinstance(reference_width, int) and isinstance(reference_height, int)
            else None
        )
        image = await upload_command_service.execute(
            user_id=user.id,
            upload_file=file,
            filename=file.filename,
            policy=UploadPolicy(
                allowed_mime=ALLOWED_MIME,
                normalizable_mime=NORMALIZABLE_UPLOAD_MIME,
                extensions=EXT_BY_MIME,
                max_bytes=MAX_BYTES,
                max_pixels=MAX_IMAGE_PIXELS,
                max_long_side=(
                    VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE
                    if upload_allows_large_dimensions(purpose)
                    else MAX_LONG_SIDE
                ),
                mask_requested=upload_requests_mask_preflight(
                    purpose,
                    file.filename,
                ),
                reference_size=reference_size,
            ),
            metadata_profile=MODEL_LIBRARY_METADATA_PROFILE,
            metadata_finalizer=upload_metadata_finalizer,
            storage_guard=ensure_storage_free_space,
        )
    except UploadCommandError as exc:
        raise http_error(exc.code, exc.message, exc.status_code) from exc

    return await image_out(db, image)
