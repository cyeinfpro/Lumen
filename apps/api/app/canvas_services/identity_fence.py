"""Durable active-identity fence for Canvas writes."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import durable_session_id_from_db
from ..services.active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    lock_active_user,
)


async def lock_canvas_write_identity(
    db: AsyncSession,
    *,
    user_id: str,
) -> None:
    try:
        await lock_active_user(
            db,
            user_id,
            session_id=durable_session_id_from_db(db),
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc


__all__ = ["lock_canvas_write_identity"]
