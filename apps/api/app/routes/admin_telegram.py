"""Durable Telegram control commands and delivery quarantine administration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.control_operations import TelegramControlCommand

from ..db import get_db
from ..deps import AdminUser, verify_csrf
from ..redis_client import get_redis
from ..services.telegram_control_dispatch import (
    create_telegram_control_lifespan,
    wake_telegram_control_publisher,
)
from ..services.telegram_quarantine import (
    QuarantineConflict,
    QuarantineNotFound,
    cleanup_quarantine,
    list_quarantines,
    queue_quarantine_redrive,
)
from ._admin_common import write_admin_audit


router = APIRouter(
    prefix="/admin/telegram",
    tags=["admin-telegram"],
    lifespan=create_telegram_control_lifespan(),
)


class CommandOut(BaseModel):
    command_id: str
    command: str
    status: Literal["queued", "accepted", "failed"]
    error: str | None = None


class QuarantineOut(BaseModel):
    id: str
    source_stream: str
    source_id: str
    stream_user_id: str
    event: str
    generation_id: str | None
    payload_raw: str
    reason: str
    attempts: int
    status: str
    redrive_count: int
    redrive_command_id: str | None
    redis_stream_id: str | None
    last_error: str | None
    resolved_at: datetime | None
    cleaned_at: datetime | None
    created_at: datetime


def _http(code: str, message: str, http_status: int) -> HTTPException:
    return HTTPException(
        status_code=http_status,
        detail={"error": {"code": code, "message": message}},
    )


def _public_command(row: TelegramControlCommand) -> CommandOut:
    public_status: Literal["queued", "accepted", "failed"] = (
        "queued" if row.status in {"pending", "published"} else row.status
    )
    return CommandOut(
        command_id=row.id,
        command=row.command,
        status=public_status,
        error=row.last_error,
    )


async def _command(
    db: AsyncSession,
    command_id: str,
) -> TelegramControlCommand:
    row = await db.get(TelegramControlCommand, command_id)
    if row is None or row.target != "tgbot":
        raise _http("not_found", "command not found", 404)
    return row


@router.post(
    "/restart",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_csrf)],
)
async def restart_bot(
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommandOut:
    row = TelegramControlCommand(
        id=uuid.uuid4().hex,
        target="tgbot",
        command="restart",
        requested_by=admin.id,
        payload={},
        status="pending",
        active_slot=1,
    )
    db.add(row)
    await write_admin_audit(
        db,
        request,
        admin,
        event_type="admin.telegram.restart.queued",
        details={"command_id": row.id},
        autocommit=False,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _http(
            "telegram_command_pending",
            "a Telegram control command is already active",
            409,
        ) from exc
    wake_telegram_control_publisher(request)
    return _public_command(row)


@router.get("/restart/{command_id}", response_model=CommandOut)
async def restart_status(
    command_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommandOut:
    row = await _command(db, command_id)
    if row.command != "restart":
        raise _http("not_found", "restart command not found", 404)
    return _public_command(row)


@router.get("/commands/{command_id}", response_model=CommandOut)
async def command_status(
    command_id: str,
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommandOut:
    return _public_command(await _command(db, command_id))


@router.get("/quarantines")
async def list_delivery_quarantines(
    _admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    include_resolved: bool = Query(default=False),
) -> dict[str, object]:
    rows = await list_quarantines(
        db,
        limit=limit,
        include_resolved=include_resolved,
    )
    items = [
        QuarantineOut(
            id=row.id,
            source_stream=row.source_stream,
            source_id=row.source_id,
            stream_user_id=row.stream_user_id,
            event=row.event,
            generation_id=row.generation_id,
            payload_raw=row.payload_raw,
            reason=row.reason,
            attempts=row.attempts,
            status=row.status,
            redrive_count=row.redrive_count,
            redrive_command_id=row.redrive_command_id,
            redis_stream_id=row.redis_stream_id,
            last_error=row.last_error,
            resolved_at=row.resolved_at,
            cleaned_at=row.cleaned_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return {"items": items, "total": len(items)}


@router.post(
    "/quarantines/{quarantine_id}/redrive",
    response_model=CommandOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_csrf)],
)
async def redrive_delivery_quarantine(
    quarantine_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CommandOut:
    try:
        command = await queue_quarantine_redrive(
            db,
            quarantine_id=quarantine_id,
            requested_by=admin.id,
        )
        await write_admin_audit(
            db,
            request,
            admin,
            event_type="admin.telegram.quarantine.redrive_queued",
            details={
                "quarantine_id": quarantine_id,
                "command_id": command.id,
            },
            autocommit=False,
        )
        await db.commit()
    except QuarantineNotFound as exc:
        raise _http("not_found", "quarantine item not found", 404) from exc
    except QuarantineConflict as exc:
        raise _http("quarantine_conflict", str(exc), 409) from exc
    except IntegrityError as exc:
        await db.rollback()
        raise _http(
            "telegram_command_pending",
            "a Telegram control command is already active",
            409,
        ) from exc
    wake_telegram_control_publisher(request)
    return _public_command(command)


@router.post(
    "/quarantines/{quarantine_id}/cleanup",
    dependencies=[Depends(verify_csrf)],
)
async def cleanup_delivery_quarantine(
    quarantine_id: str,
    request: Request,
    admin: AdminUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, object]:
    try:
        row = await cleanup_quarantine(
            db,
            quarantine_id=quarantine_id,
            redis=get_redis(),
        )
        await write_admin_audit(
            db,
            request,
            admin,
            event_type="admin.telegram.quarantine.cleaned",
            details={"quarantine_id": quarantine_id},
            autocommit=False,
        )
        await db.commit()
    except QuarantineNotFound as exc:
        raise _http("not_found", "quarantine item not found", 404) from exc
    except QuarantineConflict as exc:
        raise _http("quarantine_conflict", str(exc), 409) from exc
    return {
        "ok": True,
        "quarantine_id": row.id,
        "cleaned_at": row.cleaned_at,
    }


__all__ = ["router"]
