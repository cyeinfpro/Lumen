"""Image HTTP route aggregation and compatibility exports.

The endpoint implementation lives under ``app.images.application`` so this
module remains a stable, low-complexity integration surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..audit import write_audit
from ..deps import CurrentUser
from ..images.application import http_routes as _endpoints
from ..images.application.storage_maintenance import sweep_orphan_image_files


__all__ = [
    "ALLOWED_MIME",
    "ALLOWED_VARIANTS",
    "DISPLAY_VARIANT",
    "EXT_BY_MIME",
    "MAX_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_LONG_SIDE",
    "NORMALIZABLE_UPLOAD_MIME",
    "PILImage",
    "UPLOADS_LIMITER",
    "VARIANT_MEDIA_TYPE",
    "VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE",
    "delete_image",
    "get_image_binary",
    "get_image_by_key",
    "get_image_meta",
    "get_image_signed",
    "get_image_variant",
    "os",
    "reference_image_binary",
    "reference_image_binary_named",
    "router",
    "settings",
    "shutil",
    "sweep_orphan_image_files",
    "upload_image",
    "upload_image_impl",
]

router = _endpoints.router

# Stable constants and utility exports used by operational scripts and tests.
ALLOWED_MIME = _endpoints.ALLOWED_MIME
ALLOWED_VARIANTS = _endpoints.ALLOWED_VARIANTS
DISPLAY_VARIANT = _endpoints.DISPLAY_VARIANT
EXT_BY_MIME = _endpoints.EXT_BY_MIME
MAX_BYTES = _endpoints.MAX_BYTES
MAX_IMAGE_PIXELS = _endpoints.MAX_IMAGE_PIXELS
MAX_LONG_SIDE = _endpoints.MAX_LONG_SIDE
NORMALIZABLE_UPLOAD_MIME = _endpoints.NORMALIZABLE_UPLOAD_MIME
PILImage = _endpoints.PILImage
UPLOADS_LIMITER = _endpoints.UPLOADS_LIMITER
VARIANT_MEDIA_TYPE = _endpoints.VARIANT_MEDIA_TYPE
VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE = _endpoints.VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE
os = _endpoints.os
settings = _endpoints.settings
shutil = _endpoints.shutil

_check_public_image_lookup_rate_limit = _endpoints._check_public_image_lookup_rate_limit
_check_signed_image_rate_limit = _endpoints._check_signed_image_rate_limit
_check_upload_rate_limit = _endpoints._check_upload_rate_limit
_enforce_pixel_limit = _endpoints._enforce_pixel_limit
_ensure_image_visible_to_user = _endpoints._ensure_image_visible_to_user
_ensure_storage_free_space = _endpoints._ensure_storage_free_space
_etag_matches_if_none_match = _endpoints._etag_matches_if_none_match
_fs_path = _endpoints._fs_path
_http = _endpoints._http
_image_out = _endpoints._image_out
_iter_open_file_and_close = _endpoints._iter_open_file_and_close
_make_display_variant = _endpoints._make_display_variant
_open_regular_file_no_symlink = _endpoints._open_regular_file_no_symlink
_storage_streaming_response = _endpoints._storage_streaming_response
_upload_allows_large_dimensions = _endpoints._upload_allows_large_dimensions
_upload_metadata_finalizer = _endpoints._upload_metadata_finalizer
_upload_requests_mask_preflight = _endpoints._upload_requests_mask_preflight
_variant_key_for_image = _endpoints._variant_key_for_image
_video_reference_token_is_valid = _endpoints._video_reference_token_is_valid
_write_new_file_atomic = _endpoints._write_new_file_atomic
get_image_binary = _endpoints.get_image_binary
get_image_meta = _endpoints.get_image_meta
get_image_variant = _endpoints.get_image_variant
upload_image = _endpoints.upload_image
upload_image_impl = _endpoints.upload_image_impl


async def get_image_signed(
    image_id: str,
    variant: str,
    exp: int,
    sig: str,
    request: Request,
    db: AsyncSession,
) -> Any:
    return await _endpoints.get_image_signed_impl(
        image_id,
        variant,
        exp,
        sig,
        request,
        db,
        check_signed_image_rate_limit=_check_signed_image_rate_limit,
    )


async def reference_image_binary(
    image_id: str,
    request: Request,
    db: AsyncSession,
    token: str,
    variant: str | None = None,
    variant_service: Any | None = None,
) -> Any:
    if variant_service is None and variant == _endpoints.VIDEO_REFERENCE_IMAGE_KIND:
        variant_service = _endpoints.get_variant_service(request)
    return await _endpoints.reference_image_binary_impl(
        image_id,
        request,
        db,
        token=token,
        variant=variant,
        variant_service=variant_service,
    )


async def reference_image_binary_named(
    image_id: str,
    filename: str,
    request: Request,
    db: AsyncSession,
    token: str,
    variant: str | None = None,
    variant_service: Any | None = None,
) -> Any:
    expected = _endpoints.volcano_asset_safe_filename(image_id, asset_type="Image")
    if filename != expected or variant != _endpoints.VOLCANO_ASSET_IMAGE_KIND:
        raise _endpoints._http("not_found", "image not found", 404)
    return await reference_image_binary(
        image_id,
        request,
        db,
        token=token,
        variant=variant,
        variant_service=variant_service,
    )


async def get_image_by_key(
    key: str,
    request: Request,
    user: CurrentUser,
    db: AsyncSession,
) -> Any:
    return await _endpoints.get_image_by_key_impl(
        key,
        request,
        user,
        db,
        check_public_image_lookup_rate_limit=_check_public_image_lookup_rate_limit,
    )


async def delete_image(
    image_id: str,
    request: Request,
    user: CurrentUser,
    db: AsyncSession,
) -> dict[str, bool]:
    return await _endpoints.delete_image_impl(
        image_id,
        request,
        user,
        db,
        write_audit_event=write_audit,
    )
