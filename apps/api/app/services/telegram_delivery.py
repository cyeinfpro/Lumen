"""PostgreSQL-backed Telegram image delivery receipt protocol."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.control_operations import TelegramDeliveryAttempt
from lumen_core.model_entities.tasks import (
    Generation,
)
from lumen_core.model_entities.media_workflows import Image

from ..audit import write_audit


DELIVERY_DISPATCH_STALE_SECONDS = 15 * 60
DeliveryDecisionState = Literal[
    "send_allowed",
    "already_delivered",
    "result_unknown",
]
DeliveryTerminalState = Literal[
    "delivered",
    "failed_before_accept",
    "delivery_result_unknown",
]


class DeliveryTargetNotFound(LookupError):
    pass


class StaleDeliveryOwner(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    state: DeliveryDecisionState
    attempt_id: str
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class DeliveryFinishResult:
    state: DeliveryTerminalState
    newly_finished: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def delivery_owner_hash(owner_token: str) -> str:
    if len(owner_token) < 32:
        raise ValueError("delivery owner token is too short")
    return hashlib.sha256(owner_token.encode("utf-8")).hexdigest()


async def validate_delivery_target(
    db: AsyncSession,
    *,
    user_id: str,
    generation_id: str,
    image_id: str,
) -> None:
    target = (
        await db.execute(
            select(Image.id)
            .join(Generation, Generation.id == Image.owner_generation_id)
            .where(
                Image.id == image_id,
                Image.owner_generation_id == generation_id,
                Image.deleted_at.is_(None),
                Generation.id == generation_id,
                Generation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise DeliveryTargetNotFound("telegram delivery target was not found")


async def _locked_attempt(
    db: AsyncSession,
    *,
    generation_id: str,
    image_id: str,
) -> TelegramDeliveryAttempt | None:
    return (
        await db.execute(
            select(TelegramDeliveryAttempt)
            .where(
                TelegramDeliveryAttempt.generation_id == generation_id,
                TelegramDeliveryAttempt.image_id == image_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _insert_attempt(
    db: AsyncSession,
    *,
    generation_id: str,
    image_id: str,
    chat_id: int,
    owner_hash: str,
    now: datetime,
) -> tuple[TelegramDeliveryAttempt, bool]:
    candidate = TelegramDeliveryAttempt(
        generation_id=generation_id,
        image_id=image_id,
        telegram_chat_id=chat_id,
        owner_token_hash=owner_hash,
        state="dispatching",
        dispatch_started_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(candidate)
            await db.flush()
        return candidate, True
    except IntegrityError:
        row = await _locked_attempt(
            db,
            generation_id=generation_id,
            image_id=image_id,
        )
        if row is None:
            raise
        return row, False


async def begin_delivery_attempt(
    db: AsyncSession,
    *,
    generation_id: str,
    image_id: str,
    chat_id: int,
    owner_token: str,
    now: datetime | None = None,
) -> DeliveryDecision:
    current_time = now or _now()
    owner_hash = delivery_owner_hash(owner_token)
    row = await _locked_attempt(
        db,
        generation_id=generation_id,
        image_id=image_id,
    )
    if row is None:
        row, inserted = await _insert_attempt(
            db,
            generation_id=generation_id,
            image_id=image_id,
            chat_id=chat_id,
            owner_hash=owner_hash,
            now=current_time,
        )
        if inserted:
            return DeliveryDecision("send_allowed", row.id)

    if row.state == "delivered":
        return DeliveryDecision(
            "already_delivered",
            row.id,
            row.telegram_message_id,
        )
    if row.state == "failed_before_accept":
        row.state = "dispatching"
        row.telegram_chat_id = chat_id
        row.owner_token_hash = owner_hash
        row.telegram_message_id = None
        row.error_class = None
        row.dispatch_started_at = current_time
        row.completed_at = None
        return DeliveryDecision("send_allowed", row.id)
    if (
        row.state == "dispatching"
        and row.dispatch_started_at
        <= current_time - timedelta(seconds=DELIVERY_DISPATCH_STALE_SECONDS)
    ):
        row.state = "delivery_result_unknown"
        row.completed_at = current_time
        row.error_class = row.error_class or "StaleDispatchReconciled"
    return DeliveryDecision(
        "result_unknown",
        row.id,
        row.telegram_message_id,
    )


async def finish_delivery_attempt(
    db: AsyncSession,
    *,
    attempt_id: str,
    owner_token: str,
    state: DeliveryTerminalState,
    telegram_message_id: int | None = None,
    error_class: str | None = None,
    now: datetime | None = None,
) -> DeliveryFinishResult:
    if state == "delivered" and (
        telegram_message_id is None or telegram_message_id <= 0
    ):
        raise ValueError("delivered requires telegram_message_id")
    if state != "delivered" and telegram_message_id is not None:
        raise ValueError("non-delivered state cannot carry telegram_message_id")
    row = (
        await db.execute(
            select(TelegramDeliveryAttempt)
            .where(TelegramDeliveryAttempt.id == attempt_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("delivery attempt not found")
    supplied_hash = delivery_owner_hash(owner_token)
    if not secrets.compare_digest(row.owner_token_hash, supplied_hash):
        raise StaleDeliveryOwner("delivery owner no longer holds the attempt")
    normalized_error = (error_class or "")[:128] or None
    if row.state != "dispatching":
        same_terminal = (
            row.state == state
            and row.telegram_message_id == telegram_message_id
            and row.error_class == normalized_error
        )
        if same_terminal:
            return DeliveryFinishResult(state, newly_finished=False)
        raise StaleDeliveryOwner("delivery attempt is already terminal")

    row.state = state
    row.telegram_message_id = telegram_message_id
    row.error_class = normalized_error
    row.completed_at = now or _now()
    await write_audit(
        db,
        event_type=f"telegram.delivery.{state}",
        details={
            "attempt_id": row.id,
            "generation_id": row.generation_id,
            "image_id": row.image_id,
            "telegram_message_id": telegram_message_id,
            "error_class": normalized_error,
        },
        autocommit=False,
    )
    return DeliveryFinishResult(state, newly_finished=True)


async def reconcile_delivery_attempt(
    db: AsyncSession,
    *,
    generation_id: str,
    image_id: str,
    now: datetime | None = None,
) -> str | None:
    current_time = now or _now()
    row = await _locked_attempt(
        db,
        generation_id=generation_id,
        image_id=image_id,
    )
    if row is None:
        return None
    if (
        row.state == "dispatching"
        and row.dispatch_started_at
        <= current_time - timedelta(seconds=DELIVERY_DISPATCH_STALE_SECONDS)
    ):
        row.state = "delivery_result_unknown"
        row.completed_at = current_time
        row.error_class = row.error_class or "StaleDispatchReconciled"
    return row.state


async def delivered_image_ids(
    db: AsyncSession,
    *,
    generation_id: str,
) -> list[str]:
    return list(
        (
            await db.execute(
                select(TelegramDeliveryAttempt.image_id)
                .where(
                    TelegramDeliveryAttempt.generation_id == generation_id,
                    TelegramDeliveryAttempt.state == "delivered",
                )
                .order_by(TelegramDeliveryAttempt.created_at.asc())
            )
        ).scalars()
    )


__all__ = [
    "DeliveryDecision",
    "DeliveryFinishResult",
    "DeliveryTargetNotFound",
    "StaleDeliveryOwner",
    "begin_delivery_attempt",
    "delivered_image_ids",
    "delivery_owner_hash",
    "finish_delivery_attempt",
    "reconcile_delivery_attempt",
    "validate_delivery_target",
]
