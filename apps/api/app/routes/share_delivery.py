"""Share URL formatting and binary-delivery helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from lumen_core.model_entities import (
    Image,
    Share,
)
from lumen_core.schema_models import ShareOut

from ..config import settings
from ..services import storage_files


MAX_MULTI_SHARE_IMAGES = 100


def _http(code: str, msg: str, http: int) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def share_url(token: str, public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}/share/{token}"


def share_image_url(token: str) -> str:
    return f"/api/share/{token}/image"


def share_image_item_url(token: str, image_id: str) -> str:
    return f"/api/share/{token}/images/{image_id}"


def share_image_variant_url(token: str, image_id: str, kind: str) -> str:
    return f"/api/share/{token}/images/{image_id}/variants/{kind}"


def share_image_ids(share: Share) -> list[str]:
    raw = getattr(share, "image_ids", None)
    seen: set[str] = set()
    ids: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, str):
                continue
            clean = item.strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            ids.append(clean)
            if len(ids) >= MAX_MULTI_SHARE_IMAGES:
                break
        if ids:
            return ids
    image_id = getattr(share, "image_id", None)
    return [image_id] if isinstance(image_id, str) and image_id else []


def to_share_out(share: Share, public_base_url: str) -> ShareOut:
    image_ids = share_image_ids(share)
    return ShareOut(
        id=share.id,
        image_id=share.image_id,
        image_ids=image_ids,
        token=share.token,
        url=share_url(share.token, public_base_url),
        image_url=share_image_url(share.token),
        show_prompt=share.show_prompt,
        expires_at=share.expires_at,
        revoked_at=share.revoked_at,
        created_at=share.created_at,
    )


def fs_path(storage_key: str) -> Path:
    return storage_files.resolve_storage_path(
        settings.storage_root,
        storage_key,
        error_factory=_http,
    )


def open_storage_file_safe(storage_key: str) -> tuple[BinaryIO, int]:
    path = fs_path(storage_key)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.stat()
        fd = os.open(path, flags)
        try:
            after = os.fstat(fd)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or not stat.S_ISREG(after.st_mode)
            ):
                raise _http("invalid_path", "storage path changed while opening", 400)
            return os.fdopen(fd, "rb"), after.st_size
        except Exception:
            os.close(fd)
            raise
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        raise _http("invalid_path", "invalid storage path", 400) from exc


def storage_key_exists(storage_key: str) -> bool:
    try:
        return fs_path(storage_key).is_file()
    except HTTPException:
        return False


def iter_open_file_and_close(file: BinaryIO):
    yield from storage_files.iter_open_file_and_close(file)


def share_image_response(
    opened: BinaryIO,
    size: int,
    *,
    media_type: str,
    etag: str,
) -> StreamingResponse:
    headers = {
        "Cache-Control": "no-cache, must-revalidate",
        "Content-Length": str(size),
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        iter_open_file_and_close(opened),
        media_type=media_type,
        headers=headers,
    )


def image_etag(image: Image) -> str:
    sha = getattr(image, "sha256", None)
    return f'"{sha}"' if isinstance(sha, str) and sha else f'"{image.id}-orig"'
