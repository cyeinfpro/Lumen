"""Streaming archive helpers for the current-user data export route."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO, Iterator

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Conversation,
    Image,
    Message,
)
from lumen_core.message_content import public_message_content

from ..config import settings


_EXT_BY_MIME = MappingProxyType(
    {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }
)
_EXPORT_BATCH_SIZE = 500
_EXPORT_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class ExportStats:
    messages: int
    images: int
    images_skipped: int
    zip_bytes: int


def ext_for(mime: str) -> str:
    return _EXT_BY_MIME.get(mime, "bin")


def iter_tempfile_and_close(tmp: BinaryIO) -> Iterator[bytes]:
    try:
        while True:
            chunk = tmp.read(_EXPORT_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        tmp.close()


def export_message_record(message: Any) -> dict[str, Any]:
    return {
        "conversation_id": message.conversation_id,
        "id": message.id,
        "role": message.role,
        "content": public_message_content(message.content),
        "intent": message.intent,
        "status": message.status,
        "created_at": (message.created_at.isoformat() if message.created_at else None),
    }


def fs_path_safe(storage_key: str | None) -> Path | None:
    if not storage_key or not storage_key.strip() or "\x00" in storage_key:
        return None
    root = Path(settings.storage_root).resolve()
    key_path = Path(storage_key)
    if key_path.is_absolute() or str(key_path) == ".":
        return None
    try:
        path = (root / key_path).resolve()
        path.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def open_storage_file_safe(storage_key: str | None) -> BinaryIO | None:
    path = fs_path_safe(storage_key)
    if path is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    # O_NONBLOCK prevents a swapped-in FIFO from blocking before fstat rejects it.
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")
    except OSError:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        return None


async def _export_messages(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    user_id: str,
) -> int:
    exported = 0
    last_created_at: datetime | None = None
    last_id: str | None = None
    with archive.open("messages.ndjson", "w") as messages_file:
        while True:
            filters = [Conversation.user_id == user_id]
            if last_created_at is not None and last_id is not None:
                filters.append(
                    or_(
                        Message.created_at > last_created_at,
                        and_(
                            Message.created_at == last_created_at,
                            Message.id > last_id,
                        ),
                    )
                )
            rows = (
                await db.execute(
                    select(
                        Message.conversation_id.label("conversation_id"),
                        Message.id.label("id"),
                        Message.role.label("role"),
                        Message.content.label("content"),
                        Message.intent.label("intent"),
                        Message.status.label("status"),
                        Message.created_at.label("created_at"),
                    )
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(*filters)
                    .order_by(Message.created_at.asc(), Message.id.asc())
                    .limit(_EXPORT_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break
            for message in rows:
                line = export_message_record(message)
                await asyncio.to_thread(
                    messages_file.write,
                    json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n",
                )
                exported += 1
            last_created_at = rows[-1].created_at
            last_id = rows[-1].id
    return exported


async def _write_export_image(
    archive: zipfile.ZipFile,
    image: Any,
) -> bool:
    source = await asyncio.to_thread(open_storage_file_safe, image.storage_key)
    if source is None:
        return False
    extension = ext_for(image.mime)
    with source, archive.open(f"images/{image.id}.{extension}", "w") as image_file:
        while chunk := await asyncio.to_thread(source.read, _EXPORT_CHUNK_SIZE):
            await asyncio.to_thread(image_file.write, chunk)
    return True


async def _export_images(
    db: AsyncSession,
    archive: zipfile.ZipFile,
    user_id: str,
) -> tuple[int, int]:
    exported = 0
    skipped = 0
    last_created_at: datetime | None = None
    last_id: str | None = None
    while True:
        filters = [Image.user_id == user_id, Image.deleted_at.is_(None)]
        if last_created_at is not None and last_id is not None:
            filters.append(
                or_(
                    Image.created_at > last_created_at,
                    and_(
                        Image.created_at == last_created_at,
                        Image.id > last_id,
                    ),
                )
            )
        rows = (
            await db.execute(
                select(
                    Image.id.label("id"),
                    Image.storage_key.label("storage_key"),
                    Image.mime.label("mime"),
                    Image.created_at.label("created_at"),
                )
                .where(*filters)
                .order_by(Image.created_at.asc(), Image.id.asc())
                .limit(_EXPORT_BATCH_SIZE)
            )
        ).all()
        if not rows:
            break
        for image in rows:
            if await _write_export_image(archive, image):
                exported += 1
            else:
                skipped += 1
        last_created_at = rows[-1].created_at
        last_id = rows[-1].id
    return exported, skipped


async def build_export_archive(
    db: AsyncSession,
    tmp: BinaryIO,
    user_id: str,
) -> ExportStats:
    with zipfile.ZipFile(
        tmp,
        "w",
        zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        messages = await _export_messages(db, archive, user_id)
        images, images_skipped = await _export_images(db, archive, user_id)
    return ExportStats(
        messages=messages,
        images=images,
        images_skipped=images_skipped,
        zip_bytes=tmp.tell(),
    )
