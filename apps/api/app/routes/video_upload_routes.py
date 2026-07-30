"""Reference-video upload orchestration."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable

from fastapi import UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    User,
    Video,
)
from lumen_core.schema_models import VideoUploadOut


@dataclass(frozen=True)
class UploadDependencies:
    reference_upload_ext: Callable[[UploadFile], tuple[str, str]]
    inspect_upload: Callable[[UploadFile], Awaitable[tuple[int, str, bytes]]]
    looks_like_video: Callable[[bytes], bool]
    http_error: Callable[..., Exception]
    fs_path: Callable[[str], Path]
    write_new_file_atomic: Callable[[Path, BinaryIO], None]
    unlink_file_if_exists: Callable[[Path], None]
    upload_key: Callable[[str, str, str], str]
    ensure_access_token: Callable[[Video], Any]
    token_expiry: Callable[[], str]
    upload_out: Callable[..., VideoUploadOut]
    max_count: int
    total_max_bytes: int


async def upload_reference_video(
    *,
    user: Any,
    db: AsyncSession,
    file: UploadFile,
    deps: UploadDependencies,
) -> VideoUploadOut:
    mime, ext = deps.reference_upload_ext(file)
    size, sha, header = await deps.inspect_upload(file)
    if not deps.looks_like_video(header):
        raise deps.http_error(
            "invalid_video_file",
            "reference video must be a valid mp4 or mov file",
            415,
        )

    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    existing = (
        await db.execute(
            select(Video).where(
                Video.user_id == user.id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_(None),
                Video.sha256 == sha,
                Video.storage_key.like(f"u/{user.id}/vref/%"),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_path = deps.fs_path(existing.storage_key)
        if not existing_path.is_file():
            repaired_key = deps.upload_key(user.id, existing.id, ext)
            repaired_path = deps.fs_path(repaired_key)
            try:
                await asyncio.to_thread(
                    deps.write_new_file_atomic,
                    repaired_path,
                    file.file,
                )
                existing.storage_key = repaired_key
                existing.mime = mime
                existing.size_bytes = size
                existing.sha256 = sha
                existing.etag = sha
                metadata = dict(existing.metadata_jsonb or {})
                metadata["filename"] = file.filename or ""
                metadata["source"] = "uploaded_reference"
                existing.metadata_jsonb = metadata
                deps.ensure_access_token(existing)
                await db.commit()
            except Exception:
                await db.rollback()
                await asyncio.to_thread(deps.unlink_file_if_exists, repaired_path)
                raise
            await db.refresh(existing)
            return deps.upload_out(existing, created=False)
        deps.ensure_access_token(existing)
        await db.commit()
        await db.refresh(existing)
        return deps.upload_out(existing, created=False)
    count, total_bytes = (
        await db.execute(
            select(
                func.count(Video.id),
                func.coalesce(func.sum(Video.size_bytes), 0),
            ).where(
                Video.user_id == user.id,
                Video.owner_generation_id.is_(None),
                Video.deleted_at.is_(None),
                Video.storage_key.like(f"u/{user.id}/vref/%"),
            )
        )
    ).one()
    if int(count or 0) >= deps.max_count:
        raise deps.http_error(
            "reference_video_quota_exceeded",
            f"reference video limit is {deps.max_count} files",
            429,
        )
    if int(total_bytes or 0) + size > deps.total_max_bytes:
        raise deps.http_error(
            "reference_video_quota_exceeded",
            "reference video storage quota exceeded",
            429,
        )
    video = Video(
        user_id=user.id,
        owner_generation_id=None,
        storage_key="",
        poster_storage_key=None,
        mime=mime,
        width=0,
        height=0,
        duration_ms=0,
        fps=None,
        size_bytes=size,
        sha256=sha,
        etag=sha,
        has_audio=False,
        faststart=False,
        visibility="private",
        metadata_jsonb={
            "source": "uploaded_reference",
            "filename": file.filename or "",
            "reference_access_token": secrets.token_urlsafe(32),
            "reference_access_token_expires_at": deps.token_expiry(),
        },
    )
    db.add(video)
    await db.flush()
    key = deps.upload_key(user.id, video.id, ext)
    video.storage_key = key
    path = deps.fs_path(key)
    try:
        await asyncio.to_thread(deps.write_new_file_atomic, path, file.file)
        await db.commit()
    except Exception:
        await db.rollback()
        await asyncio.to_thread(deps.unlink_file_if_exists, path)
        raise
    await db.refresh(video)
    return deps.upload_out(video, created=True)
