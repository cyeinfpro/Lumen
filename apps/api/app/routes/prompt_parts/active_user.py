"""Active-user snapshots for paid prompt-enhancement reservations."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from fastapi import Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import User

from ...deps import durable_session_id
from ...services.active_user import (
    ActiveUserFenceError,
    ActiveUserSnapshot,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from . import idempotency, responses


async def reserve_active_prompt_operation(
    db: AsyncSession,
    operation: idempotency.PromptEnhanceOperation,
    *,
    user: User,
    request: Request | None,
) -> idempotency.PromptEnhanceReservation:
    expected_account_mode = account_mode_from_user(user)

    async def lock_snapshot() -> ActiveUserSnapshot:
        return await lock_active_user_snapshot(
            db,
            user.id,
            expected_account_mode,
            session_id=durable_session_id(request),
        )

    try:
        return await idempotency.reserve_prompt_enhance_operation(
            db,
            operation,
            before_write=lock_snapshot,
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc


async def commit_replay_response(
    db: AsyncSession,
    reservation: idempotency.PromptEnhanceReservation,
    idempotency_key: str,
    with_keepalive: Callable[[AsyncIterator[str]], AsyncIterator[str]],
) -> StreamingResponse | None:
    if reservation.replay_chunks is None:
        return None
    commit = getattr(db, "commit", None)
    if callable(commit):
        await commit()
    return responses.replay_response(
        reservation,
        idempotency_key=idempotency_key,
        with_keepalive=with_keepalive,
    )


def reserved_user(
    reservation: idempotency.PromptEnhanceReservation,
) -> User:
    snapshot = reservation.active_user_snapshot
    if snapshot is None:
        raise RuntimeError("prompt enhancement active-user snapshot is unavailable")
    return snapshot.user
