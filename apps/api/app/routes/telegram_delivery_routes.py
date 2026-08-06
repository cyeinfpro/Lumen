"""Service-auth routes for durable Telegram delivery and control receipts."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.tasks import Generation

from ..db import get_db
from ..deps import BotUser, require_bot_token
from ..services.telegram_delivery import (
    DeliveryTargetNotFound,
    StaleDeliveryOwner,
    begin_delivery_attempt,
    delivered_image_ids,
    finish_delivery_attempt,
    reconcile_delivery_attempt,
    validate_delivery_target,
)
from ..services.telegram_quarantine import (
    QuarantineConflict,
    QuarantineNotFound,
    finish_control_command,
    mark_quarantine_mirrored,
    persist_quarantine,
)


router = APIRouter()


def _http(code: str, message: str, status: int) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message}},
    )


class DeliveryBeginIn(BaseModel):
    generation_id: str = Field(min_length=1, max_length=36)
    image_id: str = Field(min_length=1, max_length=36)
    chat_id: int
    owner_token: str = Field(min_length=32, max_length=256)


class DeliveryDecisionOut(BaseModel):
    state: Literal["send_allowed", "already_delivered", "result_unknown"]
    attempt_id: str
    message_id: int | None = None


class DeliveryFinishIn(BaseModel):
    owner_token: str = Field(min_length=32, max_length=256)
    state: Literal[
        "delivered",
        "failed_before_accept",
        "delivery_result_unknown",
    ]
    telegram_message_id: int | None = None
    error_class: str | None = Field(default=None, max_length=128)


class DeliveryFinishOut(BaseModel):
    state: str
    newly_finished: bool


class DeliveryReconcileIn(BaseModel):
    generation_id: str = Field(min_length=1, max_length=36)
    image_id: str = Field(min_length=1, max_length=36)


class DeliveryReconcileOut(BaseModel):
    state: str | None


class DeliveredImagesOut(BaseModel):
    image_ids: list[str]


class QuarantinePersistIn(BaseModel):
    source_stream: str = Field(min_length=1, max_length=255)
    source_id: str = Field(min_length=1, max_length=64)
    stream_user_id: str = Field(min_length=1, max_length=64)
    event: str = Field(min_length=1, max_length=64)
    generation_id: str | None = Field(default=None, max_length=36)
    payload_raw: str = Field(max_length=1_000_000)
    reason: str = Field(min_length=1, max_length=2000)
    attempts: int = Field(ge=1)


class QuarantinePersistOut(BaseModel):
    quarantine_id: str
    status: str
    redis_stream_id: str | None = None


class QuarantineMirrorIn(BaseModel):
    redis_stream_id: str = Field(min_length=1, max_length=64)


class ControlAckIn(BaseModel):
    command: str = Field(min_length=1, max_length=32)
    status: Literal["accepted", "failed"] = "accepted"
    error: str | None = Field(default=None, max_length=2000)


class ControlAckOut(BaseModel):
    command: str
    status: str
    newly_accepted: bool
    quarantine_id: str | None = None


async def _require_generation_owner(
    db: AsyncSession,
    *,
    user_id: str,
    generation_id: str,
) -> None:
    owned = (
        await db.execute(
            select(Generation.id).where(
                Generation.id == generation_id,
                Generation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        raise _http("not_found", "generation not found", 404)


@router.get(
    "/telegram/deliveries/{generation_id}",
    response_model=DeliveredImagesOut,
)
async def list_delivery_receipts(
    generation_id: str,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeliveredImagesOut:
    await _require_generation_owner(
        db,
        user_id=user.id,
        generation_id=generation_id,
    )
    return DeliveredImagesOut(
        image_ids=await delivered_image_ids(
            db,
            generation_id=generation_id,
        )
    )


@router.post(
    "/telegram/deliveries/begin",
    response_model=DeliveryDecisionOut,
)
async def begin_delivery(
    body: DeliveryBeginIn,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeliveryDecisionOut:
    try:
        await validate_delivery_target(
            db,
            user_id=user.id,
            generation_id=body.generation_id,
            image_id=body.image_id,
        )
        decision = await begin_delivery_attempt(
            db,
            generation_id=body.generation_id,
            image_id=body.image_id,
            chat_id=body.chat_id,
            owner_token=body.owner_token,
        )
        await db.commit()
    except DeliveryTargetNotFound as exc:
        raise _http("not_found", "delivery target not found", 404) from exc
    return DeliveryDecisionOut(
        state=decision.state,
        attempt_id=decision.attempt_id,
        message_id=decision.message_id,
    )


@router.post(
    "/telegram/deliveries/{attempt_id}/finish",
    response_model=DeliveryFinishOut,
)
async def finish_delivery(
    attempt_id: str,
    body: DeliveryFinishIn,
    _user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeliveryFinishOut:
    try:
        result = await finish_delivery_attempt(
            db,
            attempt_id=attempt_id,
            owner_token=body.owner_token,
            state=body.state,
            telegram_message_id=body.telegram_message_id,
            error_class=body.error_class,
        )
        await db.commit()
    except LookupError as exc:
        raise _http("not_found", "delivery attempt not found", 404) from exc
    except StaleDeliveryOwner as exc:
        raise _http("stale_delivery_owner", str(exc), 409) from exc
    except ValueError as exc:
        raise _http("invalid_delivery_finish", str(exc), 422) from exc
    return DeliveryFinishOut(
        state=result.state,
        newly_finished=result.newly_finished,
    )


@router.post(
    "/telegram/deliveries/reconcile",
    response_model=DeliveryReconcileOut,
)
async def reconcile_delivery(
    body: DeliveryReconcileIn,
    user: BotUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DeliveryReconcileOut:
    try:
        await validate_delivery_target(
            db,
            user_id=user.id,
            generation_id=body.generation_id,
            image_id=body.image_id,
        )
    except DeliveryTargetNotFound as exc:
        raise _http("not_found", "delivery target not found", 404) from exc
    state = await reconcile_delivery_attempt(
        db,
        generation_id=body.generation_id,
        image_id=body.image_id,
    )
    await db.commit()
    return DeliveryReconcileOut(state=state)


@router.post(
    "/telegram/quarantines",
    response_model=QuarantinePersistOut,
    dependencies=[Depends(require_bot_token)],
)
async def create_quarantine(
    body: QuarantinePersistIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QuarantinePersistOut:
    row = await persist_quarantine(
        db,
        source_stream=body.source_stream,
        source_id=body.source_id,
        stream_user_id=body.stream_user_id,
        event=body.event,
        generation_id=body.generation_id,
        payload_raw=body.payload_raw,
        reason=body.reason,
        attempts=body.attempts,
    )
    await db.commit()
    return QuarantinePersistOut(
        quarantine_id=row.id,
        status=row.status,
        redis_stream_id=row.redis_stream_id,
    )


@router.post(
    "/telegram/quarantines/{quarantine_id}/mirror",
    response_model=QuarantinePersistOut,
    dependencies=[Depends(require_bot_token)],
)
async def mirror_quarantine(
    quarantine_id: str,
    body: QuarantineMirrorIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> QuarantinePersistOut:
    try:
        row = await mark_quarantine_mirrored(
            db,
            quarantine_id=quarantine_id,
            redis_stream_id=body.redis_stream_id,
        )
        await db.commit()
    except QuarantineNotFound as exc:
        raise _http("not_found", "quarantine item not found", 404) from exc
    except QuarantineConflict as exc:
        raise _http("quarantine_conflict", str(exc), 409) from exc
    return QuarantinePersistOut(
        quarantine_id=row.id,
        status=row.status,
        redis_stream_id=row.redis_stream_id,
    )


@router.post(
    "/telegram/control/{command_id}/ack",
    response_model=ControlAckOut,
    dependencies=[Depends(require_bot_token)],
)
async def ack_control_command(
    command_id: str,
    body: ControlAckIn,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ControlAckOut:
    try:
        result = await finish_control_command(
            db,
            command_id=command_id,
            expected_command=body.command,
            status=body.status,
            error=body.error,
        )
        await db.commit()
    except LookupError as exc:
        raise _http("not_found", "control command not found", 404) from exc
    except (QuarantineConflict, QuarantineNotFound) as exc:
        raise _http("command_not_ackable", str(exc), 409) from exc
    return ControlAckOut(
        command=result.command,
        status=result.status,
        newly_accepted=result.newly_terminal and result.status == "accepted",
        quarantine_id=result.quarantine_id,
    )


__all__ = ["router"]
