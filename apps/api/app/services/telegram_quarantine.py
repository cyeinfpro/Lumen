"""Durable Telegram delivery quarantine and operator redrive workflow."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.control_operations import (
    TelegramControlCommand,
    TelegramDeliveryQuarantine,
)

from ..audit import write_audit


ControlTerminalStatus = Literal["accepted", "failed"]


class QuarantineNotFound(LookupError):
    pass


class QuarantineConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ControlAckResult:
    command: str
    newly_terminal: bool
    status: ControlTerminalStatus
    quarantine_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _listener_slot(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def quarantine_stream_key(user_id: str) -> str:
    return f"tg:bot:{{{_listener_slot(user_id)}}}:delivery-quarantine"


def quarantined_marker_key(user_id: str, generation_id: str) -> str:
    suffix = hashlib.sha256(
        (generation_id or "invalid").encode("utf-8")
    ).hexdigest()[:32]
    return f"tg:bot:{{{_listener_slot(user_id)}}}:quarantined:{suffix}"


async def _locked_quarantine(
    db: AsyncSession,
    quarantine_id: str,
) -> TelegramDeliveryQuarantine | None:
    return (
        await db.execute(
            select(TelegramDeliveryQuarantine)
            .where(TelegramDeliveryQuarantine.id == quarantine_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def persist_quarantine(
    db: AsyncSession,
    *,
    source_stream: str,
    source_id: str,
    stream_user_id: str,
    event: str,
    generation_id: str | None,
    payload_raw: str,
    reason: str,
    attempts: int,
) -> TelegramDeliveryQuarantine:
    existing = (
        await db.execute(
            select(TelegramDeliveryQuarantine)
            .where(
                TelegramDeliveryQuarantine.source_stream == source_stream,
                TelegramDeliveryQuarantine.source_id == source_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = TelegramDeliveryQuarantine(
        source_stream=source_stream,
        source_id=source_id,
        stream_user_id=stream_user_id,
        event=event,
        generation_id=generation_id or None,
        payload_raw=payload_raw,
        reason=reason[:2000],
        attempts=max(1, attempts),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = (
            await db.execute(
                select(TelegramDeliveryQuarantine)
                .where(
                    TelegramDeliveryQuarantine.source_stream == source_stream,
                    TelegramDeliveryQuarantine.source_id == source_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing
    await write_audit(
        db,
        event_type="telegram.delivery.quarantined",
        details={
            "quarantine_id": row.id,
            "source_stream": source_stream,
            "source_id": source_id,
            "event": event,
            "generation_id": generation_id or None,
            "attempts": attempts,
        },
        autocommit=False,
    )
    return row


async def mark_quarantine_mirrored(
    db: AsyncSession,
    *,
    quarantine_id: str,
    redis_stream_id: str,
) -> TelegramDeliveryQuarantine:
    row = await _locked_quarantine(db, quarantine_id)
    if row is None:
        raise QuarantineNotFound("quarantine item not found")
    if row.redis_stream_id not in {None, redis_stream_id}:
        raise QuarantineConflict("quarantine mirror id conflicts")
    row.redis_stream_id = redis_stream_id
    return row


async def list_quarantines(
    db: AsyncSession,
    *,
    limit: int,
    include_resolved: bool,
) -> list[TelegramDeliveryQuarantine]:
    statement = select(TelegramDeliveryQuarantine)
    if not include_resolved:
        statement = statement.where(
            TelegramDeliveryQuarantine.status != "resolved"
        )
    return list(
        (
            await db.execute(
                statement.order_by(
                    desc(TelegramDeliveryQuarantine.created_at),
                    desc(TelegramDeliveryQuarantine.id),
                ).limit(limit)
            )
        ).scalars()
    )


async def queue_quarantine_redrive(
    db: AsyncSession,
    *,
    quarantine_id: str,
    requested_by: str,
) -> TelegramControlCommand:
    row = await _locked_quarantine(db, quarantine_id)
    if row is None:
        raise QuarantineNotFound("quarantine item not found")
    if row.status == "resolved":
        raise QuarantineConflict("quarantine item is already resolved")
    if row.status == "redrive_queued" and row.redrive_command_id:
        command = await db.get(TelegramControlCommand, row.redrive_command_id)
        if command is not None and command.status in {"pending", "published"}:
            return command

    command = TelegramControlCommand(
        id=uuid.uuid4().hex,
        target="tgbot",
        command="redrive_quarantine",
        requested_by=requested_by,
        payload={
            "quarantine_id": row.id,
            "source_stream": row.source_stream,
            "source_id": row.source_id,
            "stream_user_id": row.stream_user_id,
            "event": row.event,
            "generation_id": row.generation_id or "",
            "payload_raw": row.payload_raw,
            "redis_stream_id": row.redis_stream_id or "",
        },
    )
    db.add(command)
    await db.flush()
    row.status = "redrive_queued"
    row.redrive_count = int(row.redrive_count or 0) + 1
    row.redrive_command_id = command.id
    row.last_error = None
    return command


async def finish_control_command(
    db: AsyncSession,
    *,
    command_id: str,
    expected_command: str,
    status: ControlTerminalStatus,
    error: str | None = None,
) -> ControlAckResult:
    command = (
        await db.execute(
            select(TelegramControlCommand)
            .where(
                TelegramControlCommand.id == command_id,
                TelegramControlCommand.target == "tgbot",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if command is None:
        raise LookupError("control command not found")
    if command.command != expected_command:
        raise QuarantineConflict("control command type does not match transport")
    if command.status in {"accepted", "failed"}:
        if command.status != status:
            raise QuarantineConflict("control command is already terminal")
        quarantine_id = str(command.payload.get("quarantine_id") or "") or None
        return ControlAckResult(
            command=command.command,
            newly_terminal=False,
            status=status,
            quarantine_id=quarantine_id,
        )
    if command.status not in {"pending", "published"}:
        raise QuarantineConflict("control command cannot be acknowledged")

    now = _now()
    command.status = status
    command.active_slot = None
    command.publish_owner = None
    command.publish_lease_until = None
    command.completed_at = now
    command.last_error = (error or "")[:2000] or None
    if status == "accepted":
        command.accepted_at = now
    quarantine_id = str(command.payload.get("quarantine_id") or "") or None
    if command.command == "redrive_quarantine" and quarantine_id:
        quarantine = await _locked_quarantine(db, quarantine_id)
        if quarantine is None:
            raise QuarantineNotFound("redrive quarantine item not found")
        if status == "accepted":
            quarantine.status = "resolved"
            quarantine.resolved_at = now
            quarantine.last_error = None
        else:
            quarantine.status = "pending"
            quarantine.last_error = command.last_error
    await write_audit(
        db,
        event_type=f"admin.telegram.{command.command}.{status}",
        user_id=command.requested_by,
        details={
            "command_id": command.id,
            "quarantine_id": quarantine_id,
            "error": command.last_error,
        },
        autocommit=False,
    )
    return ControlAckResult(
        command=command.command,
        newly_terminal=True,
        status=status,
        quarantine_id=quarantine_id,
    )


async def cleanup_quarantine(
    db: AsyncSession,
    *,
    quarantine_id: str,
    redis: Any,
) -> TelegramDeliveryQuarantine:
    row = await _locked_quarantine(db, quarantine_id)
    if row is None:
        raise QuarantineNotFound("quarantine item not found")
    if row.status != "resolved":
        raise QuarantineConflict("quarantine item is not resolved")
    if row.cleaned_at is not None:
        return row
    stream_key = quarantine_stream_key(row.stream_user_id)
    if row.redis_stream_id:
        await redis.xdel(stream_key, row.redis_stream_id)
    await redis.delete(
        quarantined_marker_key(
            row.stream_user_id,
            row.generation_id or "",
        )
    )
    row.cleaned_at = _now()
    return row


__all__ = [
    "ControlAckResult",
    "QuarantineConflict",
    "QuarantineNotFound",
    "cleanup_quarantine",
    "finish_control_command",
    "list_quarantines",
    "mark_quarantine_mirrored",
    "persist_quarantine",
    "quarantine_stream_key",
    "quarantined_marker_key",
    "queue_quarantine_redrive",
]
