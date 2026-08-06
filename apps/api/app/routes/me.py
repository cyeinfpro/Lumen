"""当前用户自助端点（V1.0 收尾）：用量统计 / 数据导出 / 注销 / 会话管理。"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import CompletionStatus, GenerationStatus
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

from ..audit import request_ip_hash, write_audit, write_audit_isolated
from ..db import get_db
from ..deps import CurrentUser, verify_csrf_session
from ..ratelimit import RateLimiter
from ..redis_client import get_redis
from ..services.account_deletion import (
    cancel_account_active_tasks,
    dml_rowcount,
    post_commit_account_task_cleanup,
)
from . import me_export as _me_export
from .me_export import (
    build_export_archive as _build_export_archive,
    iter_tempfile_and_close as _iter_tempfile_and_close,
)


router = APIRouter(prefix="/me", tags=["me"])

# Compatibility exports used by focused route-security tests and callers.
_export_message_record = _me_export.export_message_record
_fs_path_safe = _me_export.fs_path_safe
_open_storage_file_safe = _me_export.open_storage_file_safe


def _http(
    code: str,
    msg: str,
    http: int = 400,
    *,
    details: dict[str, object] | None = None,
) -> HTTPException:
    error: dict[str, object] = {"code": code, "message": msg}
    if details:
        error["details"] = details
    return HTTPException(
        status_code=http,
        detail={"error": error},
    )


# Why: capacity=2 (instead of 1) so a transient redis blip mid-export — which
# leaves a token "consumed" in redis state — does not lock the user out for a
# full hour. The refill rate (1/hr) still caps sustained use to one export per
# hour; the extra burst slot is purely for retry-after-failure ergonomics.
_EXPORT_LIMITER = RateLimiter(capacity=2, refill_per_sec=1 / 3600, always_on=True)


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
      images/{image_id}.{ext}   — binary blobs
      export-manifest.json      — complete export counts
    """
    user_id = user.id
    user_email = user.email
    active_user_id = (
        await db.execute(
            select(User.id).where(User.id == user_id, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    await db.rollback()
    if active_user_id is None:
        raise _http("user_deleted", "user account was deleted", 401)
    await _EXPORT_LIMITER.check(get_redis(), f"rl:me:export:{user_id}")

    tmp = tempfile.TemporaryFile()
    try:
        stats = await _build_export_archive(db, tmp, user_id)
        await write_audit(
            db,
            event_type="me.data.export",
            user_id=user_id,
            actor_email=user_email,
            actor_ip_hash=request_ip_hash(request),
            target_user_id=user_id,
            details={
                "messages": stats.messages,
                "images": stats.images,
                "images_skipped": stats.images_skipped,
                "zip_bytes": stats.zip_bytes,
            },
            autocommit=True,
        )
        tmp.seek(0)
    except _me_export.ExportIntegrityError as exc:
        tmp.close()
        await write_audit_isolated(
            event_type="me.data.export.fail",
            user_id=user_id,
            actor_email=user_email,
            actor_ip_hash=request_ip_hash(request),
            target_user_id=user_id,
            details={"image_id": exc.image_id, "reason": exc.reason},
        )
        raise _http(
            "export_incomplete",
            "data export could not be completed",
            500,
            details={"image_id": exc.image_id, "reason": exc.reason},
        ) from exc
    except Exception:
        tmp.close()
        raise

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"lumen-export-{user_id}-{ts}.zip"

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(stats.zip_bytes),
        "X-Lumen-Export-Complete": "true",
        "X-Lumen-Export-Image-Count": str(stats.images),
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
    task_cleanup = await cancel_account_active_tasks(
        db,
        user_id=user.id,
        canceled_at=now,
        account_mode=getattr(user, "account_mode", "wallet"),
        queue_redis=get_redis(),
    )
    await write_audit(
        db,
        event_type="me.account.delete",
        user_id=user.id,
        actor_email=user.email,
        actor_ip_hash=request_ip_hash(request),
        target_user_id=user.id,
        details={
            "users": dml_rowcount(user_result),
            "sessions_revoked": dml_rowcount(sessions_result),
            "conversations_deleted": dml_rowcount(conversations_result),
            "images_deleted": dml_rowcount(images_result),
            "generations_canceled": task_cleanup["generations_canceled"],
            "completions_canceled": task_cleanup["completions_canceled"],
            "video_generations_canceled": task_cleanup["video_generations_canceled"],
            "videos_deleted": task_cleanup["videos_deleted"],
            "memory_extractions_canceled": task_cleanup["memory_extractions_canceled"],
        },
        autocommit=False,
    )
    await db.commit()

    # DB state is now durable. Redis/cache side effects are intentionally written
    # only after commit so a failed account deletion cannot leave stale cancels.
    await post_commit_account_task_cleanup(user_id=user.id, cleanup=task_cleanup)

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
        (
            await db.execute(
                select(AuthSession)
                .where(
                    AuthSession.user_id == user.id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .order_by(AuthSession.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
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
            autocommit=False,
        )
        await db.commit()
    return None
