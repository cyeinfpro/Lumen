"""Shared active-identity fences for durable API writes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import AuthSession, User
from lumen_core.utils import ensure_utc


class ActiveUserFenceError(RuntimeError):
    """The authenticated identity became invalid before a durable write."""


class ActiveUserDeleted(ActiveUserFenceError):
    """The authenticated user disappeared before a durable write."""


class ActiveSessionRevoked(ActiveUserFenceError):
    """The durable session was revoked before a durable write."""


class ActiveSessionExpired(ActiveUserFenceError):
    """The durable session expired before a durable write."""


def active_user_deleted_http_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "error": {"code": "user_deleted", "message": "user account was deleted"}
        },
    )


def active_user_fence_http_error(error: ActiveUserFenceError) -> HTTPException:
    if isinstance(error, ActiveUserDeleted):
        return active_user_deleted_http_error()
    if isinstance(error, ActiveSessionExpired):
        return HTTPException(
            status_code=401,
            detail={"error": {"code": "session_expired", "message": "session expired"}},
        )
    return HTTPException(
        status_code=401,
        detail={"error": {"code": "session_revoked", "message": "session was revoked"}},
    )


async def _ensure_active_session(
    db: AsyncSession,
    *,
    user_id: str,
    session_id: str,
    lock: bool,
) -> None:
    # Request-start authentication already loaded this row into the session's
    # identity map. The fence must overwrite that nonlocking snapshot after
    # acquiring its lock, otherwise a just-committed revocation stays stale.
    statement = (
        select(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.user_id == user_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    session = (await db.execute(statement)).scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        raise ActiveSessionRevoked()
    now = datetime.now(timezone.utc)
    if int(ensure_utc(session.expires_at).timestamp()) <= int(now.timestamp()):
        raise ActiveSessionExpired()


async def ensure_active_user(
    db: AsyncSession,
    user_id: str,
    *,
    session_id: str | None = None,
) -> None:
    """Confirm an active user/session before side effects without retaining locks."""
    active_user_id = (
        await db.execute(
            select(User.id).where(
                User.id == user_id,
                User.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if active_user_id is None:
        raise ActiveUserDeleted()
    if session_id:
        await _ensure_active_session(
            db,
            user_id=user_id,
            session_id=session_id,
            lock=False,
        )


async def lock_active_user(
    db: AsyncSession,
    user_id: str,
    *,
    session_id: str | None = None,
) -> None:
    """Lock the user, then its durable session, across a durable write.

    The lock order is deliberately ``users -> auth_sessions``. Account
    deletion and bulk session revocation already start from the user row;
    single-session revocation locks only ``auth_sessions`` and never waits on
    ``users``, so it cannot form a cycle with this fence.
    """
    active_user_id = (
        await db.execute(
            select(User.id)
            .where(
                User.id == user_id,
                User.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if active_user_id is None:
        raise ActiveUserDeleted()
    if session_id:
        await _ensure_active_session(
            db,
            user_id=user_id,
            session_id=session_id,
            lock=True,
        )
