"""FastAPI 依赖：当前用户 / CSRF 校验（DESIGN §9）。"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import AuthSession, TelegramBinding, User
from lumen_core.utils import ensure_utc

from .config import settings
from .db import get_db
from .ratelimit import RateLimiter, require_client_ip
from .security import parse_session_cookie, verify_csrf_token

logger = logging.getLogger(__name__)

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
SESSION_VALIDATION_FAILURE_LIMITER = RateLimiter(
    capacity=30,
    refill_per_sec=30 / 60,
    always_on=True,
    key_prefix="rl:session:failed",
    scope="ip",
)


def _unauthorized(code: str = "unauthenticated", msg: str = "missing or invalid session") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": {"code": code, "message": msg}},
    )


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    raw = request.cookies.get(SESSION_COOKIE)
    sid = parse_session_cookie(raw)
    if not sid:
        raise _unauthorized()

    cached_sid = getattr(request.state, "session_id", None)
    cached_user = getattr(request.state, "current_user", None)
    if cached_sid == sid and isinstance(cached_user, User):
        return cached_user

    _session, user = await require_active_session_user(request, db, sid)
    return user


async def require_active_session_user(
    request: Request,
    db: AsyncSession,
    sid: str,
) -> tuple[AuthSession, User]:
    row = (
        await db.execute(
            select(AuthSession, User)
            .join(User, User.id == AuthSession.user_id)
            .where(AuthSession.id == sid)
        )
    ).first()
    if not row:
        await _record_failed_session_validation(request)
        raise _unauthorized()

    session, user = row
    now = datetime.now(timezone.utc)
    if session.revoked_at is not None:
        await _record_failed_session_validation(request)
        raise _unauthorized("session_revoked", "session was revoked")
    # Compare at whole-second precision so this freshness check stays in lockstep
    # with the cookie's `exp` (which is `int(time.time())`). Mixing microsecond
    # `datetime` with second-precision cookie `exp` produced sub-second windows
    # where one check passed and the other failed.
    if int(ensure_utc(session.expires_at).timestamp()) <= int(now.timestamp()):
        await _record_failed_session_validation(request)
        raise _unauthorized("session_expired", "session expired")
    if user.deleted_at is not None:
        await _record_failed_session_validation(request)
        raise _unauthorized("user_deleted", "user account was deleted")
    # attach session state for downstream handlers and avoid duplicate queries
    request.state.session_id = sid
    request.state.current_user = user
    return session, user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def _record_failed_session_validation(request: Request) -> None:
    try:
        ip = require_client_ip(request)
    except HTTPException:
        return
    from .redis_client import get_redis

    try:
        await SESSION_VALIDATION_FAILURE_LIMITER.check(
            get_redis(), f"rl:session:failed:{ip}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("session validation limiter unavailable: %s", exc)


async def verify_csrf(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """CSRF check. Call via `Depends(verify_csrf)` on write endpoints.

    Safe (GET/HEAD/OPTIONS) requests skip. POST/PATCH/DELETE/PUT require a
    session-bound `X-CSRF-Token` header.
    """
    if request.method in SAFE_METHODS:
        return
    header = request.headers.get(CSRF_HEADER)
    sid = parse_session_cookie(request.cookies.get(SESSION_COOKIE))
    if not header or not sid or not verify_csrf_token(sid, header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "csrf_failed", "message": "CSRF token mismatch"}},
        )
    await require_active_session_user(request, db, sid)
    request.state.csrf_session_id = sid


async def verify_csrf_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """CSRF check for auth-sensitive routes that must reject stale sessions early."""
    if request.method in SAFE_METHODS:
        return
    header = request.headers.get(CSRF_HEADER)
    sid = parse_session_cookie(request.cookies.get(SESSION_COOKIE))
    if not header or not sid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "csrf_failed", "message": "CSRF token mismatch"}},
        )
    if not verify_csrf_token(sid, header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"code": "csrf_failed", "message": "CSRF token mismatch"}},
        )
    await require_active_session_user(request, db, sid)
    request.state.csrf_session_id = sid


BOT_TOKEN_HEADER = "X-Bot-Token"
BOT_CHAT_ID_HEADER = "X-Telegram-Chat-Id"
BOT_TOKEN_FAILURE_LIMITER = RateLimiter(
    capacity=20,
    refill_per_sec=20 / 60,
    always_on=True,
    key_prefix="rl:botauth:failed",
    scope="ip",
)


async def _record_bot_auth_failure(request: Request) -> None:
    try:
        ip = require_client_ip(request)
    except HTTPException:
        return
    from .redis_client import get_redis

    try:
        await BOT_TOKEN_FAILURE_LIMITER.check(get_redis(), f"rl:botauth:failed:{ip}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("bot-token failure limiter unavailable: %s", exc)


async def require_bot_token(request: Request) -> None:
    """Verify the shared X-Bot-Token. Used by /telegram/* routes that don't
    yet identify a user (link / health). Routes that need a user identity
    use `BotUser` instead — it includes this check.
    """
    expected = settings.telegram_bot_shared_secret.strip()
    provided = (request.headers.get(BOT_TOKEN_HEADER) or "").strip()
    if not expected or not provided or not hmac.compare_digest(expected, provided):
        await _record_bot_auth_failure(request)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"code": "bot_unauthorized", "message": "invalid bot token"}},
        )


async def get_bot_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the Lumen user for a Telegram chat_id. Requires a valid
    X-Bot-Token AND a chat_id that is bound to a (non-deleted) user.

    This is the bot's equivalent of `get_current_user`. It bypasses CSRF
    intentionally — the shared secret is the auth factor for service-to-service
    calls. The route surface is restricted to the small `/telegram/*` set, so
    a leaked token can only access bot endpoints, not admin.
    """
    await require_bot_token(request)
    chat_id = (request.headers.get(BOT_CHAT_ID_HEADER) or "").strip()
    if not chat_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {"code": "missing_chat_id", "message": "X-Telegram-Chat-Id header required"}
            },
        )
    row = (
        await db.execute(
            select(TelegramBinding, User)
            .join(User, User.id == TelegramBinding.user_id)
            .where(
                TelegramBinding.chat_id == chat_id,
                User.deleted_at.is_(None),
            )
        )
    ).first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "not_bound", "message": "telegram chat is not bound"}},
        )
    _binding, user = row
    request.state.current_user = user
    return user


BotUser = Annotated[User, Depends(get_bot_user)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "forbidden", "message": "admin only"}},
        )
    return user


AdminUser = Annotated[User, Depends(require_admin)]
