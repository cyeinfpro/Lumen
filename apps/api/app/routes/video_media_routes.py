"""Video binary and poster delivery routes."""

from __future__ import annotations

import errno
import os
import stat
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from typing import BinaryIO, Iterator

from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import Video
from lumen_core.volcano_asset_media import (
    VOLCANO_ASSET_VIDEO_KIND,
    VOLCANO_ASSET_VIDEO_MIME,
    volcano_asset_video_variant_metadata,
)
from lumen_core.volcano_assets import volcano_asset_safe_filename

from ..config import settings
from ..services.video.errors import video_http_error
from ..services.video.reference_media import reference_token_is_valid
from ..video_reference_videos import (
    VIDEO_REFERENCE_VIDEO_KIND,
    VIDEO_REFERENCE_VIDEO_MIME,
    video_reference_variant_metadata,
)


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return video_http_error(code, message, status_code)


def fs_path(storage_key: str) -> Path:
    root = Path(settings.storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _http("invalid_path", "storage path escapes root", 400) from exc
    return path


def open_regular_file_no_symlink(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise _http("not_found", "binary missing", 404) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _http(
                "invalid_path", "symlink storage paths are not allowed", 400
            ) from exc
        raise
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise _http("not_found", "binary missing", 404)
        return os.fdopen(fd, "rb"), int(file_stat.st_size)
    except Exception:
        os.close(fd)
        raise


def iter_file_and_close(
    file: BinaryIO,
    *,
    start: int = 0,
    length: int | None = None,
) -> Iterator[bytes]:
    try:
        if start:
            file.seek(start)
        remaining = length
        while remaining is None or remaining > 0:
            chunk_size = (
                1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
            )
            data = file.read(chunk_size)
            if not data:
                break
            if remaining is not None:
                remaining -= len(data)
            yield data
    finally:
        file.close()


def quote_etag(etag: str) -> str:
    value = etag.strip()
    if value.startswith('"') and value.endswith('"'):
        return value
    return f'"{value}"'


def etag_matches(if_none_match: str | None, quoted_etag: str) -> bool:
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        value = candidate.strip()
        if value == "*":
            return True
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == quoted_etag:
            return True
    return False


def parse_range(range_header: str, size: int) -> tuple[int, int] | None:
    if not range_header.startswith("bytes=") or "," in range_header:
        return None
    spec = range_header.removeprefix("bytes=").strip()
    if "-" not in spec:
        return None
    start_raw, end_raw = spec.split("-", 1)
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def media_response(
    request: Request,
    path: Path,
    *,
    media_type: str,
    etag: str,
    last_modified: datetime | None,
    immutable: bool,
    download_filename: str | None = None,
    inline_filename: str | None = None,
) -> Response:
    quoted_etag = quote_etag(etag)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": (
            "private, max-age=31536000, immutable"
            if immutable
            else "private, max-age=3600"
        ),
        "ETag": quoted_etag,
    }
    if download_filename:
        headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'
    elif inline_filename:
        headers["Content-Disposition"] = f'inline; filename="{inline_filename}"'
    if last_modified is not None:
        headers["Last-Modified"] = format_datetime(last_modified, usegmt=True)
    if etag_matches(request.headers.get("if-none-match"), quoted_etag):
        return Response(status_code=304, headers=headers)
    file, size = open_regular_file_no_symlink(path)
    range_header = request.headers.get("range")
    if range_header:
        parsed = parse_range(range_header, size)
        if parsed is None:
            file.close()
            return Response(
                status_code=416,
                headers={**headers, "Content-Range": f"bytes */{size}"},
            )
        start, end = parsed
        length = end - start + 1
        return StreamingResponse(
            iter_file_and_close(file, start=start, length=length),
            status_code=206,
            media_type=media_type,
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            },
        )
    return StreamingResponse(
        iter_file_and_close(file),
        media_type=media_type,
        headers={**headers, "Content-Length": str(size)},
    )


async def owned_video(db: AsyncSession, user_id: str, video_id: str) -> Video:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("not_found", "video not found", 404)
    return video


async def reference_video_binary(
    video_id: str,
    request: Request,
    db: AsyncSession,
    *,
    token: str,
    variant: str | None,
) -> Response:
    video = (
        await db.execute(
            select(Video).where(
                Video.id == video_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http("not_found", "video not found", 404)
    if not reference_token_is_valid(
        video.metadata_jsonb or {},
        token_key="reference_access_token",
        expires_key="reference_access_token_expires_at",
        token=token,
    ):
        raise _http("not_found", "video not found", 404)
    if variant:
        if variant == VIDEO_REFERENCE_VIDEO_KIND:
            variant_meta = video_reference_variant_metadata(video)
            media_type = VIDEO_REFERENCE_VIDEO_MIME
        elif variant == VOLCANO_ASSET_VIDEO_KIND:
            variant_meta = volcano_asset_video_variant_metadata(video)
            media_type = VOLCANO_ASSET_VIDEO_MIME
        else:
            raise _http("not_found", "video not found", 404)
        if variant_meta is None:
            raise _http("not_found", "video not found", 404)
        storage_key = str(variant_meta["storage_key"])
        etag = str(variant_meta["sha256"])
        last_modified = video.updated_at
        inline_filename = (
            volcano_asset_safe_filename(video.id, asset_type="Video")
            if variant == VOLCANO_ASSET_VIDEO_KIND
            else None
        )
        rollback = getattr(db, "rollback", None)
        if callable(rollback):
            await rollback()
        return media_response(
            request,
            fs_path(storage_key),
            media_type=media_type,
            etag=etag,
            last_modified=last_modified,
            immutable=True,
            inline_filename=inline_filename,
        )
    storage_key = video.storage_key
    media_type = video.mime
    etag = video.etag or video.sha256
    last_modified = video.updated_at
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        await rollback()
    return media_response(
        request,
        fs_path(storage_key),
        media_type=media_type,
        etag=etag,
        last_modified=last_modified,
        immutable=True,
    )


async def reference_video_binary_named(
    video_id: str,
    filename: str,
    request: Request,
    db: AsyncSession,
    *,
    token: str,
    variant: str | None,
) -> Response:
    expected = volcano_asset_safe_filename(video_id, asset_type="Video")
    if filename != expected or variant != VOLCANO_ASSET_VIDEO_KIND:
        raise _http("not_found", "video not found", 404)
    return await reference_video_binary(
        video_id,
        request,
        db,
        token=token,
        variant=variant,
    )


async def video_binary(
    video_id: str,
    request: Request,
    user_id: str,
    db: AsyncSession,
    *,
    download: bool,
) -> Response:
    video = await owned_video(db, user_id, video_id)
    storage_key = video.storage_key
    media_type = video.mime
    etag = video.etag or video.sha256
    last_modified = video.updated_at
    download_filename = (
        f"lumen-video-{video.id}{Path(storage_key).suffix.lower() or '.mp4'}"
        if download
        else None
    )
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        await rollback()
    return media_response(
        request,
        fs_path(storage_key),
        media_type=media_type,
        etag=etag,
        last_modified=last_modified,
        immutable=True,
        download_filename=download_filename,
    )


async def video_poster(
    video_id: str,
    request: Request,
    user_id: str,
    db: AsyncSession,
) -> Response:
    video = await owned_video(db, user_id, video_id)
    if not video.poster_storage_key:
        raise _http("not_found", "poster not found", 404)
    storage_key = video.poster_storage_key
    etag = f"{video.etag}:poster"
    last_modified = video.updated_at
    rollback = getattr(db, "rollback", None)
    if callable(rollback):
        await rollback()
    return media_response(
        request,
        fs_path(storage_key),
        media_type="image/jpeg",
        etag=etag,
        last_modified=last_modified,
        immutable=True,
    )
