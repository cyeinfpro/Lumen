"""Public image-artifact delivery helpers for non-image route modules."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request, Response

from ..config import settings
from ..images.application.deliver import DeliverySpec, deliver_artifact
from ..services import storage_files
from ..images.application._file_delivery import (
    etag_matches_if_none_match,
    internal_redirect_enabled,
    iter_open_file_and_close,
    open_regular_file_no_symlink,
    storage_streaming_response,
)


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def image_storage_path(storage_key: str) -> Path:
    return storage_files.resolve_storage_path(
        settings.storage_root,
        storage_key,
        error_factory=_http,
    )


def image_storage_streaming_response(
    path: Path,
    *,
    media_type: str,
    etag: str,
    cache_control: str,
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
        response_builder=lambda delivery_path, **kwargs: storage_streaming_response(
            delivery_path,
            **kwargs,
            etag_matches=etag_matches_if_none_match,
            validate_storage_key=image_storage_path,
            open_file=open_regular_file_no_symlink,
            iter_file=iter_open_file_and_close,
            redirect_enabled=internal_redirect_enabled,
        ),
    )
