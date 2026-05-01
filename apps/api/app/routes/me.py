"""当前用户自助端点（V1.0 收尾）：用量统计 / 数据导出 / 注销 / 会话管理。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, BinaryIO, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus, CompletionStatus
from lumen_core.models import (
    AuthSession,
    Completion,
    Conversation,
    Generation,
    Image,
    Message,
    User,
)
from lumen_core.schemas import SessionOut, SessionsOut, UsageOut

from ..audit import request_ip_hash, write_audit
from ..config import settings
from ..db import get_db
from ..deps import CurrentUser, verify_csrf_session


router = APIRouter(prefix="/me", tags=["me"])


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_EXPORT_BATCH_SIZE = 500
_EXPORT_CHUNK_SIZE = 64 * 1024


def _ext_for(mime: str) -> str:
    return _EXT_BY_MIME.get(mime, "bin")


def _iter_tempfile_and_close(tmp: BinaryIO) -> Iterator[bytes]:
    try:
        while True:
            chunk = tmp.read(_EXPORT_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        tmp.close()


def _fs_path_safe(storage_key: str | None) -> Path | None:
    if not storage_key or not storage_key.strip() or "\x00" in storage_key:
        return None
    root = Path(settings.storage_root).resolve()
    key_path = Path(storage_key)
    if key_path.is_absolute() or str(key_path) == ".":
        return None
    try:
        p = (root / key_path).resolve()
        p.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return p


def _open_storage_file_safe(storage_key: str | None) -> BinaryIO | None:
    path = _fs_path_safe(storage_key)
    if path is None:
        return None
    try:
        before = path.stat()
        src = path.open("rb")
    except OSError:
        return None
    try:
        after = os.fstat(src.fileno())
        if not (
            stat.S_ISREG(after.st_mode)
            and before.st_dev == after.st_dev
            and before.st_ino == after.st_ino
        ):
            src.close()
            return None
        return src
    except OSError:
        src.close()
        return None


@router.get("/usage", response_model=UsageOut)
async def get_my_usage(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> UsageOut:
    range_end = datetime.now(timezone.utc)
    range_start = range_end - timedelta(days=days)

    # messages_count: messages in user's conversations with role='user' in range
    messages_count_sq = (
        select(func.count(Message.id))
        .select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.user_id == user.id,
            Message.role == "user",
            Message.created_at >= range_start,
            Message.created_at <= range_end,
        )
        .scalar_subquery()
    )

    generations_count_sq = (
        select(func.count(Generation.id))
        .where(
            Generation.user_id == user.id,
            Generation.created_at >= range_start,
            Generation.created_at <= range_end,
        )
        .scalar_subquery()
    )

    generations_succeeded_sq = (
        select(func.count(Generation.id))
        .where(
            Generation.user_id == user.id,
            Generation.status == GenerationStatus.SUCCEEDED.value,
            Generation.created_at >= range_start,
            Generation.created_at <= range_end,
        )
        .scalar_subquery()
    )

    generations_failed_sq = (
        select(func.count(Generation.id))
        .where(
            Generation.user_id == user.id,
            Generation.status == GenerationStatus.FAILED.value,
            Generation.created_at >= range_start,
            Generation.created_at <= range_end,
        )
        .scalar_subquery()
    )

    completions_count_sq = (
        select(func.count(Completion.id))
        .where(
            Completion.user_id == user.id,
            Completion.created_at >= range_start,
            Completion.created_at <= range_end,
        )
        .scalar_subquery()
    )

    completions_succeeded_sq = (
        select(func.count(Completion.id))
        .where(
            Completion.user_id == user.id,
            Completion.status == CompletionStatus.SUCCEEDED.value,
            Completion.created_at >= range_start,
            Completion.created_at <= range_end,
        )
        .scalar_subquery()
    )

    completions_failed_sq = (
        select(func.count(Completion.id))
        .where(
            Completion.user_id == user.id,
            Completion.status == CompletionStatus.FAILED.value,
            Completion.created_at >= range_start,
            Completion.created_at <= range_end,
        )
        .scalar_subquery()
    )

    total_pixels_sq = (
        select(func.coalesce(func.sum(Generation.upstream_pixels), 0))
        .where(
            Generation.user_id == user.id,
            Generation.status == GenerationStatus.SUCCEEDED.value,
            Generation.created_at >= range_start,
            Generation.created_at <= range_end,
        )
        .scalar_subquery()
    )

    total_tokens_in_sq = (
        select(func.coalesce(func.sum(Completion.tokens_in), 0))
        .where(
            Completion.user_id == user.id,
            Completion.created_at >= range_start,
            Completion.created_at <= range_end,
        )
        .scalar_subquery()
    )

    total_tokens_out_sq = (
        select(func.coalesce(func.sum(Completion.tokens_out), 0))
        .where(
            Completion.user_id == user.id,
            Completion.created_at >= range_start,
            Completion.created_at <= range_end,
        )
        .scalar_subquery()
    )

    # storage_bytes: all time, non-deleted images
    storage_bytes_sq = (
        select(func.coalesce(func.sum(Image.size_bytes), 0))
        .where(
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
        )
        .scalar_subquery()
    )

    stmt = select(
        messages_count_sq.label("messages_count"),
        generations_count_sq.label("generations_count"),
        generations_succeeded_sq.label("generations_succeeded"),
        generations_failed_sq.label("generations_failed"),
        completions_count_sq.label("completions_count"),
        completions_succeeded_sq.label("completions_succeeded"),
        completions_failed_sq.label("completions_failed"),
        total_pixels_sq.label("total_pixels_generated"),
        total_tokens_in_sq.label("total_tokens_in"),
        total_tokens_out_sq.label("total_tokens_out"),
        storage_bytes_sq.label("storage_bytes"),
    )
    row = (await db.execute(stmt)).one()

    return UsageOut(
        range_start=range_start,
        range_end=range_end,
        messages_count=int(row.messages_count or 0),
        generations_count=int(row.generations_count or 0),
        generations_succeeded=int(row.generations_succeeded or 0),
        generations_failed=int(row.generations_failed or 0),
        completions_count=int(row.completions_count or 0),
        completions_succeeded=int(row.completions_succeeded or 0),
        completions_failed=int(row.completions_failed or 0),
        total_pixels_generated=int(row.total_pixels_generated or 0),
        total_tokens_in=int(row.total_tokens_in or 0),
        total_tokens_out=int(row.total_tokens_out or 0),
        storage_bytes=int(row.storage_bytes or 0),
    )


# ---------------------------------------------------------------------------
# Data export — POST /me/export
# ---------------------------------------------------------------------------

@router.post("/export", dependencies=[Depends(verify_csrf_session)])
async def export_my_data(
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StreamingResponse:
    """Pack all the user's conversations / messages / images into a single zip.

    Layout:
      messages.ndjson           — one JSON object per line, asc by created_at
      images/{image_id}.{ext}   — binary blobs (skips entries whose file is gone)
    """
    active_user_id = (
        await db.execute(
            select(User.id)
            .where(User.id == user.id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if active_user_id is None:
        raise _http("user_deleted", "user account was deleted", 401)

    tmp = tempfile.TemporaryFile()
    messages_exported = 0
    images_exported = 0
    images_skipped = 0

    try:
        with zipfile.ZipFile(
            tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True
        ) as zf:
            with zf.open("messages.ndjson", "w") as messages_file:
                last_created_at: datetime | None = None
                last_id: str | None = None
                while True:
                    filters = [Conversation.user_id == user.id]
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
                            .join(
                                Conversation,
                                Conversation.id == Message.conversation_id,
                            )
                            .where(*filters)
                            .order_by(Message.created_at.asc(), Message.id.asc())
                            .limit(_EXPORT_BATCH_SIZE)
                        )
                    ).all()
                    if not rows:
                        break
                    for m in rows:
                        line = {
                            "conversation_id": m.conversation_id,
                            "id": m.id,
                            "role": m.role,
                            "content": m.content,
                            "intent": m.intent,
                            "status": m.status,
                            "created_at": (
                                m.created_at.isoformat() if m.created_at else None
                            ),
                        }
                        messages_file.write(
                            json.dumps(line, ensure_ascii=False).encode("utf-8")
                            + b"\n"
                        )
                        messages_exported += 1
                    last_created_at = rows[-1].created_at
                    last_id = rows[-1].id

            last_created_at = None
            last_id = None
            while True:
                filters = [Image.user_id == user.id, Image.deleted_at.is_(None)]
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
                for img in rows:
                    src = _open_storage_file_safe(img.storage_key)
                    if src is None:
                        images_skipped += 1
                        continue
                    ext = _ext_for(img.mime)
                    with src, zf.open(f"images/{img.id}.{ext}", "w") as image_file:
                        while True:
                            chunk = src.read(_EXPORT_CHUNK_SIZE)
                            if not chunk:
                                break
                            image_file.write(chunk)
                    images_exported += 1
                last_created_at = rows[-1].created_at
                last_id = rows[-1].id

        zip_size = tmp.tell()
        await write_audit(
            db,
            event_type="me.data.export",
            user_id=user.id,
            actor_email=user.email,
            actor_ip_hash=request_ip_hash(request),
            target_user_id=user.id,
            details={
                "messages": messages_exported,
                "images": images_exported,
                "images_skipped": images_skipped,
                "zip_bytes": zip_size,
            },
        )
        await db.commit()
        tmp.seek(0)
    except Exception:
        tmp.close()
        raise

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"lumen-export-{user.id}-{ts}.zip"

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(zip_size),
    }
    return StreamingResponse(
        _iter_tempfile_and_close(tmp),
        media_type="application/zip",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Account deletion — DELETE /me  (soft)
# ---------------------------------------------------------------------------

@router.delete("", status_code=204, dependencies=[Depends(verify_csrf_session)])
async def delete_my_account(
    request: Request,
    user: CurrentUser,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    now = datetime.now(timezone.utc)
    active_user_id = (
        await db.execute(
            select(User.id)
            .where(User.id == user.id, User.deleted_at.is_(None))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active_user_id is None:
        raise _http("user_deleted", "user account was deleted", 401)

    # User: soft-delete
    user_result = await db.execute(
        update(User)
        .where(User.id == user.id, User.deleted_at.is_(None))
        .values(deleted_at=now)
    )
    # Sessions: revoke all
    sessions_result = await db.execute(
        update(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    # Conversations: soft-delete all
    conversations_result = await db.execute(
        update(Conversation)
        .where(
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
        )
        .values(deleted_at=now)
    )
    # Images: soft-delete all
    images_result = await db.execute(
        update(Image)
        .where(
            Image.user_id == user.id,
            Image.deleted_at.is_(None),
        )
        .values(deleted_at=now)
    )
    await write_audit(
        db,
        event_type="me.account.delete",
        user_id=user.id,
        actor_email=user.email,
        actor_ip_hash=request_ip_hash(request),
        target_user_id=user.id,
        details={
            "users": user_result.rowcount,
            "sessions_revoked": sessions_result.rowcount,
            "conversations_deleted": conversations_result.rowcount,
            "images_deleted": images_result.rowcount,
        },
    )
    await db.commit()

    # Best-effort: clear cookies
    response.delete_cookie("session", path="/")
    response.delete_cookie("csrf", path="/")
    response.status_code = 204
    return response


# ---------------------------------------------------------------------------
# Sessions — list & revoke
# ---------------------------------------------------------------------------

@router.get("/sessions", response_model=SessionsOut)
async def list_my_sessions(
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionsOut:
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(AuthSession)
            .where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > now,
            )
            .order_by(AuthSession.created_at.desc())
        )
    ).scalars().all()
    current_sid = getattr(request.state, "session_id", None)
    items = [
        SessionOut(
            id=s.id,
            ua=s.ua,
            ip=s.ip,
            created_at=s.created_at,
            expires_at=s.expires_at,
            is_current=(s.id == current_sid),
        )
        for s in rows
    ]
    return SessionsOut(items=items)


@router.delete(
    "/sessions/{sid}", status_code=204, dependencies=[Depends(verify_csrf_session)]
)
async def revoke_my_session(
    sid: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    sess = (
        await db.execute(
            select(AuthSession).where(AuthSession.id == sid).with_for_update()
        )
    ).scalar_one_or_none()
    if sess is None or sess.user_id != user.id:
        raise _http("not_found", "session not found", 404)
    if sess.revoked_at is None:
        sess.revoked_at = datetime.now(timezone.utc)
        await write_audit(
            db,
            event_type="me.session.revoke",
            user_id=user.id,
            actor_email=user.email,
            actor_ip_hash=request_ip_hash(request),
            target_user_id=user.id,
            details={
                "session_id": sid,
                "is_current": sid == getattr(request.state, "session_id", None),
            },
        )
        await db.commit()
    return None
