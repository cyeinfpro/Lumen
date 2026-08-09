"""Durable Telegram delivery quarantine and operator redrive workflow."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, cast

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.control_operations import (
    TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY,
    TELEGRAM_CONTROL_RESTART_INTENT_KEY,
    TelegramControlCommand,
    TelegramControlEffectReceipt,
    TelegramControlRestartIntent,
    TelegramDeliveryQuarantine,
)

from ..audit import write_audit


ControlTerminalStatus = Literal["accepted", "failed"]
ControlEffectPrepareAction = Literal[
    "execute",
    "already_succeeded",
    "outcome_unknown",
]
ControlEffectFinishStatus = Literal["succeeded", "failed", "outcome_unknown"]
ControlEffectReconciliation = Literal["succeeded", "retry"]


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


@dataclass(frozen=True, slots=True)
class ControlEffectClaim:
    command_id: str
    command: str
    payload: dict[str, Any]
    owner: str | None
    fence: int
    acquired: bool
    status: str
    lease_seconds: int


@dataclass(frozen=True, slots=True)
class ControlEffectRenewal:
    command_id: str
    fence: int
    renewed: bool
    lease_seconds: int


@dataclass(frozen=True, slots=True)
class ControlEffectPreparation:
    command_id: str
    fence: int
    action: ControlEffectPrepareAction
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ControlRestartIntentResult:
    command_id: str
    fence: int
    action: Literal["stop_current_generation"]
    requested_generation: str


@dataclass(frozen=True, slots=True)
class ControlEffectReconciliationResult:
    command_id: str
    command_status: str
    effect_status: str
    resolution: ControlEffectReconciliation


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _payload_copy(command: TelegramControlCommand) -> dict[str, Any]:
    return dict(command.payload or {})


async def _locked_control_command(
    db: AsyncSession,
    *,
    command_id: str,
    expected_command: str,
) -> TelegramControlCommand:
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
    return command


def _require_live_effect_lease(
    command: TelegramControlCommand,
    *,
    owner: str,
    fence: int,
    now: datetime,
) -> None:
    if (
        command.effect_status != "running"
        or command.effect_owner != owner
        or int(command.effect_fence or 0) != fence
        or command.effect_lease_until is None
        or command.effect_lease_until <= now
    ):
        raise QuarantineConflict("control effect lease or fence was lost")


def _listener_slot(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def quarantine_stream_key(user_id: str) -> str:
    return f"tg:bot:{{{_listener_slot(user_id)}}}:delivery-quarantine"


def quarantined_marker_key(user_id: str, generation_id: str) -> str:
    suffix = hashlib.sha256((generation_id or "invalid").encode("utf-8")).hexdigest()[
        :32
    ]
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
        statement = statement.where(TelegramDeliveryQuarantine.status != "resolved")
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
    recovered_receipt: dict[str, Any] | None = None
    if row.redrive_command_id:
        previous = await db.get(TelegramControlCommand, row.redrive_command_id)
        if (
            row.status == "redrive_queued"
            and previous is not None
            and previous.status in {"pending", "published"}
        ):
            return previous
        previous_payload = dict(previous.payload or {}) if previous is not None else {}
        previous_receipt = previous_payload.get(TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY)
        if isinstance(previous_receipt, dict):
            if previous_receipt.get("state") in {
                "dispatching",
                "outcome_unknown",
                "retryable",
            }:
                raise QuarantineConflict(
                    "previous redrive delivery has a durable receipt and must be "
                    "reconciled before another external attempt"
                )
            if previous_receipt.get("state") == "succeeded":
                recovered_receipt = dict(previous_receipt)

    payload: dict[str, Any] = {
        "quarantine_id": row.id,
        "source_stream": row.source_stream,
        "source_id": row.source_id,
        "stream_user_id": row.stream_user_id,
        "event": row.event,
        "generation_id": row.generation_id or "",
        "payload_raw": row.payload_raw,
        "redis_stream_id": row.redis_stream_id or "",
    }
    if recovered_receipt is not None:
        payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = recovered_receipt
    command = TelegramControlCommand(
        id=uuid.uuid4().hex,
        target="tgbot",
        command="redrive_quarantine",
        requested_by=requested_by,
        payload=payload,
    )
    db.add(command)
    await db.flush()
    row.status = "redrive_queued"
    row.redrive_count = int(row.redrive_count or 0) + 1
    row.redrive_command_id = command.id
    row.last_error = None
    return command


async def claim_control_effect(
    db: AsyncSession,
    *,
    command_id: str,
    expected_command: str,
    owner: str,
    lease_seconds: int = 60,
) -> ControlEffectClaim:
    if lease_seconds < 3:
        raise ValueError("control effect lease must be at least 3 seconds")
    command = await _locked_control_command(
        db,
        command_id=command_id,
        expected_command=expected_command,
    )
    now = _now()
    if command.status in {"accepted", "failed"}:
        expected_effect_status = (
            "succeeded" if command.status == "accepted" else "failed"
        )
        if command.effect_status != expected_effect_status:
            raise QuarantineConflict(
                "terminal control command has inconsistent effect state"
            )
    elif command.status not in {"pending", "published"}:
        raise QuarantineConflict("control command cannot execute its effect")
    if command.effect_status in {"succeeded", "failed"}:
        return ControlEffectClaim(
            command_id=command.id,
            command=command.command,
            payload=dict(command.payload or {}),
            owner=None,
            fence=int(command.effect_fence or 0),
            acquired=False,
            status=command.effect_status,
            lease_seconds=lease_seconds,
        )
    if (
        command.effect_status == "running"
        and command.effect_lease_until is not None
        and command.effect_lease_until > now
    ):
        return ControlEffectClaim(
            command_id=command.id,
            command=command.command,
            payload=dict(command.payload or {}),
            owner=command.effect_owner,
            fence=int(command.effect_fence or 0),
            acquired=False,
            status="running",
            lease_seconds=lease_seconds,
        )
    command.effect_status = "running"
    command.effect_owner = owner
    command.effect_lease_until = now + timedelta(seconds=lease_seconds)
    command.effect_fence = int(command.effect_fence or 0) + 1
    command.effect_attempts = int(command.effect_attempts or 0) + 1
    command.effect_error = None
    await db.flush()
    return ControlEffectClaim(
        command_id=command.id,
        command=command.command,
        payload=dict(command.payload or {}),
        owner=owner,
        fence=int(command.effect_fence),
        acquired=True,
        status="running",
        lease_seconds=lease_seconds,
    )


async def renew_control_effect(
    db: AsyncSession,
    *,
    command_id: str,
    expected_command: str,
    owner: str,
    fence: int,
    lease_seconds: int = 60,
) -> ControlEffectRenewal:
    if lease_seconds < 3:
        raise ValueError("control effect lease must be at least 3 seconds")
    command = await _locked_control_command(
        db,
        command_id=command_id,
        expected_command=expected_command,
    )
    now = _now()
    _require_live_effect_lease(
        command,
        owner=owner,
        fence=fence,
        now=now,
    )
    command.effect_lease_until = now + timedelta(seconds=lease_seconds)
    await db.flush()
    return ControlEffectRenewal(
        command_id=command.id,
        fence=fence,
        renewed=True,
        lease_seconds=lease_seconds,
    )


async def prepare_redrive_control_effect(
    db: AsyncSession,
    *,
    command_id: str,
    owner: str,
    fence: int,
) -> ControlEffectPreparation:
    command = await _locked_control_command(
        db,
        command_id=command_id,
        expected_command="redrive_quarantine",
    )
    now = _now()
    _require_live_effect_lease(
        command,
        owner=owner,
        fence=fence,
        now=now,
    )
    payload = _payload_copy(command)
    quarantine_id = str(payload.get("quarantine_id") or "")
    if not quarantine_id:
        raise QuarantineConflict("redrive quarantine id is missing")
    idempotency_key = f"telegram-quarantine-redrive:{quarantine_id}"
    raw_receipt = payload.get(TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY)
    if isinstance(raw_receipt, dict):
        receipt = cast(TelegramControlEffectReceipt, dict(raw_receipt))
        if receipt.get("idempotency_key") != idempotency_key:
            raise QuarantineConflict("redrive receipt idempotency key conflicts")
        receipt_state = receipt.get("state")
        if receipt_state == "succeeded":
            action: ControlEffectPrepareAction = "already_succeeded"
        elif receipt_state == "retryable":
            receipt.update(
                {
                    "state": "dispatching",
                    "owner": owner,
                    "fence": fence,
                    "attempt": int(receipt.get("attempt") or 1) + 1,
                    "started_at": _iso(now),
                }
            )
            receipt.pop("completed_at", None)
            receipt.pop("error", None)
            payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
            command.payload = payload
            action = "execute"
        elif receipt_state == "dispatching":
            receipt["state"] = "outcome_unknown"
            receipt["completed_at"] = _iso(now)
            receipt["error"] = (
                "previous redrive lease expired before a terminal receipt"
            )
            payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
            command.payload = payload
            action = "outcome_unknown"
        elif receipt_state == "outcome_unknown":
            action = "outcome_unknown"
        else:
            raise QuarantineConflict("redrive receipt state is invalid")
        await db.flush()
        return ControlEffectPreparation(
            command_id=command.id,
            fence=fence,
            action=action,
            idempotency_key=idempotency_key,
        )

    receipt: TelegramControlEffectReceipt = {
        "idempotency_key": idempotency_key,
        "state": "dispatching",
        "owner": owner,
        "fence": fence,
        "attempt": 1,
        "started_at": _iso(now),
    }
    payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
    command.payload = payload
    await db.flush()
    return ControlEffectPreparation(
        command_id=command.id,
        fence=fence,
        action="execute",
        idempotency_key=idempotency_key,
    )


async def commit_restart_intent(
    db: AsyncSession,
    *,
    command_id: str,
    owner: str,
    fence: int,
    generation: str,
) -> ControlRestartIntentResult:
    command = await _locked_control_command(
        db,
        command_id=command_id,
        expected_command="restart",
    )
    now = _now()
    _require_live_effect_lease(
        command,
        owner=owner,
        fence=fence,
        now=now,
    )
    payload = _payload_copy(command)
    raw_intent = payload.get(TELEGRAM_CONTROL_RESTART_INTENT_KEY)
    if isinstance(raw_intent, dict):
        intent = cast(TelegramControlRestartIntent, dict(raw_intent))
        requested_generation = str(intent.get("requested_generation") or "")
        if requested_generation != generation:
            raise QuarantineConflict(
                "restart intent belongs to a different process generation"
            )
    else:
        intent = {
            "state": "stop_intent_committed",
            "requested_generation": generation,
            "committed_at": _iso(now),
        }
        payload[TELEGRAM_CONTROL_RESTART_INTENT_KEY] = intent
        command.payload = payload

    command.effect_owner = None
    command.effect_lease_until = None
    await db.flush()
    return ControlRestartIntentResult(
        command_id=command.id,
        fence=fence,
        action="stop_current_generation",
        requested_generation=generation,
    )


def _redrive_receipt(
    payload: dict[str, Any],
) -> TelegramControlEffectReceipt:
    raw_receipt = payload.get(TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY)
    if not isinstance(raw_receipt, dict):
        raise QuarantineConflict("redrive delivery receipt is missing")
    return cast(TelegramControlEffectReceipt, dict(raw_receipt))


def _require_owned_dispatch_receipt(
    receipt: TelegramControlEffectReceipt,
    *,
    owner: str,
    fence: int,
) -> None:
    if (
        receipt.get("state") != "dispatching"
        or receipt.get("owner") != owner
        or int(receipt.get("fence") or 0) != fence
    ):
        raise QuarantineConflict("redrive delivery receipt is not owned by this effect")


def _finish_unknown_redrive(
    command: TelegramControlCommand,
    payload: dict[str, Any],
    *,
    owner: str,
    fence: int,
    now: datetime,
    error: str | None,
) -> None:
    if command.command != "redrive_quarantine":
        raise QuarantineConflict("only redrive effects can remain outcome_unknown")
    receipt = _redrive_receipt(payload)
    if receipt.get("state") == "outcome_unknown":
        return
    _require_owned_dispatch_receipt(receipt, owner=owner, fence=fence)
    receipt["state"] = "outcome_unknown"
    receipt["completed_at"] = _iso(now)
    receipt["error"] = (error or "")[:2000]
    payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
    command.payload = payload
    command.effect_completed_at = None
    command.effect_error = (error or "")[:2000] or None


def _finish_restart_success(
    command: TelegramControlCommand,
    payload: dict[str, Any],
    *,
    now: datetime,
    generation: str | None,
) -> None:
    raw_intent = payload.get(TELEGRAM_CONTROL_RESTART_INTENT_KEY)
    if not isinstance(raw_intent, dict):
        raise QuarantineConflict("restart intent is not durable")
    intent = cast(TelegramControlRestartIntent, dict(raw_intent))
    requested_generation = str(intent.get("requested_generation") or "")
    if not generation or requested_generation == generation:
        raise QuarantineConflict(
            "restart must be completed by a new process generation"
        )
    intent["state"] = "new_generation_ready"
    intent["completed_by_generation"] = generation
    intent["ready_at"] = _iso(now)
    payload[TELEGRAM_CONTROL_RESTART_INTENT_KEY] = intent
    command.payload = payload


def _finish_redrive_terminal(
    command: TelegramControlCommand,
    payload: dict[str, Any],
    *,
    status: ControlEffectFinishStatus,
    owner: str,
    fence: int,
    now: datetime,
) -> None:
    raw_receipt = payload.get(TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY)
    if status == "succeeded":
        receipt = _redrive_receipt(payload)
        if receipt.get("state") != "succeeded":
            _require_owned_dispatch_receipt(receipt, owner=owner, fence=fence)
            receipt["state"] = "succeeded"
            receipt["completed_at"] = _iso(now)
            payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
            command.payload = payload
    elif isinstance(raw_receipt, dict) and raw_receipt.get("state") == "dispatching":
        raise QuarantineConflict(
            "a dispatched redrive cannot be terminally failed while "
            "its external outcome is unknown"
        )


async def finish_control_effect(
    db: AsyncSession,
    *,
    command_id: str,
    expected_command: str,
    owner: str,
    fence: int,
    status: ControlEffectFinishStatus,
    error: str | None = None,
    generation: str | None = None,
) -> str:
    command = await _locked_control_command(
        db,
        command_id=command_id,
        expected_command=expected_command,
    )
    if status != "outcome_unknown" and command.effect_status == status:
        return status
    payload = _payload_copy(command)
    if status == "outcome_unknown":
        existing_receipt = payload.get(TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY)
        if (
            command.command == "redrive_quarantine"
            and isinstance(existing_receipt, dict)
            and existing_receipt.get("state") == "outcome_unknown"
            and command.effect_status == "running"
            and command.effect_owner == owner
            and int(command.effect_fence or 0) == fence
        ):
            return status
    now = _now()
    _require_live_effect_lease(
        command,
        owner=owner,
        fence=fence,
        now=now,
    )
    if status == "outcome_unknown":
        _finish_unknown_redrive(
            command,
            payload,
            owner=owner,
            fence=fence,
            now=now,
            error=error,
        )
        await db.flush()
        return status

    if command.command == "restart" and status == "succeeded":
        _finish_restart_success(
            command,
            payload,
            now=now,
            generation=generation,
        )
    elif command.command == "redrive_quarantine":
        _finish_redrive_terminal(
            command,
            payload,
            status=status,
            owner=owner,
            fence=fence,
            now=now,
        )
    command.effect_status = status
    command.effect_owner = None
    command.effect_lease_until = None
    command.effect_completed_at = now
    command.effect_error = (error or "")[:2000] or None
    await db.flush()
    return status


async def reconcile_redrive_control_effect(
    db: AsyncSession,
    *,
    command_id: str,
    resolution: ControlEffectReconciliation,
    note: str | None = None,
) -> ControlEffectReconciliationResult:
    command = await _locked_control_command(
        db,
        command_id=command_id,
        expected_command="redrive_quarantine",
    )
    payload = _payload_copy(command)
    raw_receipt = payload.get(TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY)
    if not isinstance(raw_receipt, dict):
        raise QuarantineConflict("redrive delivery receipt is missing")
    receipt = cast(TelegramControlEffectReceipt, dict(raw_receipt))
    quarantine_id = str(payload.get("quarantine_id") or "")
    expected_key = f"telegram-quarantine-redrive:{quarantine_id}"
    if not quarantine_id or receipt.get("idempotency_key") != expected_key:
        raise QuarantineConflict("redrive receipt identity is invalid")

    now = _now()
    receipt_state = receipt.get("state")
    if resolution == "succeeded":
        if receipt_state not in {"outcome_unknown", "succeeded"}:
            raise QuarantineConflict(
                "only an outcome_unknown redrive can be reconciled as succeeded"
            )
        receipt["state"] = "succeeded"
        receipt["completed_at"] = receipt.get("completed_at") or _iso(now)
        receipt["reconciled_at"] = receipt.get("reconciled_at") or _iso(now)
        receipt["reconciliation"] = "succeeded"
        if note and not receipt.get("reconciliation_note"):
            receipt["reconciliation_note"] = note[:2000]
        payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
        command.payload = payload
        command.effect_status = "succeeded"
        command.effect_owner = None
        command.effect_lease_until = None
        command.effect_completed_at = command.effect_completed_at or now
        command.effect_error = None
        await db.flush()
        result = await finish_control_command(
            db,
            command_id=command.id,
            expected_command="redrive_quarantine",
            status="accepted",
        )
        return ControlEffectReconciliationResult(
            command_id=command.id,
            command_status=result.status,
            effect_status="succeeded",
            resolution=resolution,
        )

    if command.status not in {"pending", "published"}:
        raise QuarantineConflict("terminal redrive command cannot be retried")
    if receipt_state == "retryable" and command.effect_status == "pending":
        return ControlEffectReconciliationResult(
            command_id=command.id,
            command_status=command.status,
            effect_status=command.effect_status,
            resolution=resolution,
        )
    if receipt_state != "outcome_unknown":
        raise QuarantineConflict(
            "only an outcome_unknown redrive can be authorized for retry"
        )
    receipt["state"] = "retryable"
    receipt["reconciled_at"] = _iso(now)
    receipt["reconciliation"] = "retry"
    if note:
        receipt["reconciliation_note"] = note[:2000]
    payload[TELEGRAM_CONTROL_EFFECT_RECEIPT_KEY] = receipt
    command.payload = payload
    command.effect_status = "pending"
    command.effect_owner = None
    command.effect_lease_until = None
    command.effect_completed_at = None
    command.effect_error = None
    quarantine = await _locked_quarantine(db, quarantine_id)
    if quarantine is None:
        raise QuarantineNotFound("redrive quarantine item not found")
    quarantine.status = "redrive_queued"
    quarantine.last_error = None
    await db.flush()
    return ControlEffectReconciliationResult(
        command_id=command.id,
        command_status=command.status,
        effect_status=command.effect_status,
        resolution=resolution,
    )


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
    expected_effect_status = "succeeded" if status == "accepted" else "failed"
    if command.effect_status != expected_effect_status:
        raise QuarantineConflict(
            "control command cannot be terminal before its effect is durable"
        )

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
    "ControlEffectClaim",
    "ControlEffectPreparation",
    "ControlEffectReconciliationResult",
    "ControlEffectRenewal",
    "ControlRestartIntentResult",
    "QuarantineConflict",
    "QuarantineNotFound",
    "cleanup_quarantine",
    "commit_restart_intent",
    "finish_control_command",
    "claim_control_effect",
    "finish_control_effect",
    "list_quarantines",
    "mark_quarantine_mirrored",
    "persist_quarantine",
    "prepare_redrive_control_effect",
    "quarantine_stream_key",
    "quarantined_marker_key",
    "queue_quarantine_redrive",
    "reconcile_redrive_control_effect",
    "renew_control_effect",
]
