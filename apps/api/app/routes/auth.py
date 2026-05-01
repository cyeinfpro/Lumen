"""Auth 路由（DESIGN §5.1 简化版）。

V1 实现：signup / login / logout / me，以及最小密码重置后端。
不实现：OAuth、refresh rotation（session 直接用 cookie 引用 auth_sessions 行）。
"""

from __future__ import annotations

import logging
import re
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import AllowedEmail, AuthSession, InviteLink, User
from lumen_core.schemas import LoginIn, SignupIn, UserOut

from ..audit import request_ip_hash, write_audit, write_audit_isolated
from ..config import settings
from ..db import get_db
from ..deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    ensure_utc,
    require_active_session_user,
    verify_csrf_session,
)
from ..security import (
    generate_csrf_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    make_session_cookie,
    parse_session_cookie,
    verify_password,
)
from ..ratelimit import (
    AUTH_LOGIN_LIMITER,
    AUTH_SIGNUP_LIMITER,
    RateLimiter,
    client_ip,
    require_client_ip,
)
from ..redis_client import get_redis


router = APIRouter()

logger = logging.getLogger(__name__)

# Why: strip control chars (incl. NUL/CR/LF/DEL) before persisting UA so log
# injection / DB driver quirks can't slip through user-controlled headers.
_UA_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$nwx+yaiP/NQqpodrnT3F9A"
    "$mmmttUtPlkaR5x78voo478doWSwYbHXVEUD9sfJkg9M"
)
_MIN_PASSWORD_LEN = 8
# Why: the reset token sits in Redis from generation until the user clicks
# the email link. Any leakage during that window (logs, mail relays, browser
# history, screenshot) lets an attacker reset the account. Shortening the
# window to 15 minutes meaningfully reduces this exposure while still
# accommodating typical email delivery latencies.
_PASSWORD_RESET_TTL_SECONDS = 15 * 60
_PASSWORD_RESET_KEY_PREFIX = "pwd_reset"
_PASSWORD_RESET_REQUEST_IP_LIMITER = RateLimiter(
    capacity=5, refill_per_sec=5 / 300, always_on=True
)
_PASSWORD_RESET_REQUEST_EMAIL_LIMITER = RateLimiter(
    capacity=3, refill_per_sec=3 / 900, always_on=True
)
_PASSWORD_RESET_CONFIRM_IP_LIMITER = RateLimiter(
    capacity=10, refill_per_sec=10 / 300, always_on=True
)
_PASSWORD_RESET_CONFIRM_TOKEN_LIMITER = RateLimiter(
    capacity=5, refill_per_sec=5 / 900, always_on=True
)


def _sanitize_ua(raw: str | None) -> str:
    return _UA_CONTROL_CHARS.sub("", raw or "")[:1024]


def _log_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def _bad(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(status_code=http, detail={"error": {"code": code, "message": msg}})


def _validate_password_strength(password: str) -> None:
    if len(password) < _MIN_PASSWORD_LEN:
        raise _bad(
            "weak_password",
            f"password must be at least {_MIN_PASSWORD_LEN} characters",
            400,
        )


def _password_reset_key(token: str) -> str:
    return f"{_PASSWORD_RESET_KEY_PREFIX}:{hash_token(token)}"


def _cookie_samesite() -> str:
    return "lax" if settings.app_env == "dev" else "strict"


def _set_auth_cookies(response: Response, session_id: str, csrf: str) -> None:
    secure = settings.app_env != "dev"
    max_age = settings.session_ttl_min * 60
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(session_id),
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite=_cookie_samesite(),
        path="/",
    )
    _set_csrf_cookie(response, csrf)


def _set_csrf_cookie(response: Response, csrf: str) -> None:
    secure = settings.app_env != "dev"
    max_age = settings.session_ttl_min * 60
    # CSRF must be readable by JS (double-submit). Not httponly.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite=_cookie_samesite(),
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


class CsrfOut(BaseModel):
    csrf_token: str


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=1)
    new_password: str


class OkOut(BaseModel):
    ok: bool


async def _create_session(
    db: AsyncSession, user: User, request: Request
) -> tuple[AuthSession, str]:
    refresh = generate_refresh_token()
    resolved_ip = client_ip(request)
    session = AuthSession(
        user_id=user.id,
        refresh_token_hash=hash_token(refresh),
        ua=_sanitize_ua(request.headers.get("user-agent")),
        ip=None if resolved_ip == "unknown" else resolved_ip,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.session_ttl_min),
    )
    db.add(session)
    await db.flush()
    return session, refresh


def _invite_validity_reason(inv: InviteLink, now: datetime) -> str | None:
    """Return None if the invite is currently usable; else a short reason."""
    if inv.revoked_at is not None:
        return "revoked"
    if inv.used_at is not None:
        return "used"
    if inv.expires_at is not None and ensure_utc(inv.expires_at) <= now:
        return "expired"
    return None


@router.post(
    "/signup",
    response_model=UserOut,
    dependencies=[Depends(AUTH_SIGNUP_LIMITER)],
)
async def signup(
    body: SignupIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    email = body.email.strip().lower()
    if not email or not body.password:
        raise _bad("invalid_input", "email and password are required", 422)
    _validate_password_strength(body.password)

    # Pre-existing user check first — don't accidentally consume an invite.
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing:
        # Why: keep response time roughly equal to the success path so attackers
        # can't enumerate registered emails by timing.
        verify_password(_DUMMY_PASSWORD_HASH, body.password)
        logger.info(
            "signup_rejected",
            extra={"email_hash": _log_hash(email), "reason": "email_taken"},
        )
        await write_audit_isolated(
            event_type="auth.signup.fail",
            actor_email=email,
            actor_ip_hash=request_ip_hash(request),
            details={"reason": "email_taken"},
        )
        raise _bad("email_taken", "an account with this email already exists", 409)

    # Either allowlisted or holding a valid invite token.
    allow = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()

    invite: InviteLink | None = None
    role = "member"

    if not allow:
        if not body.invite_token:
            verify_password(_DUMMY_PASSWORD_HASH, body.password)
            logger.info(
                "signup_rejected",
                extra={"email_hash": _log_hash(email), "reason": "email_not_invited"},
            )
            await write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={"reason": "email_not_invited"},
            )
            raise _bad(
                "email_not_invited",
                "this email is not on the invite allowlist",
                403,
            )
        invite = (
            await db.execute(
                select(InviteLink)
                .where(InviteLink.token == body.invite_token)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if invite is None:
            verify_password(_DUMMY_PASSWORD_HASH, body.password)
            logger.info(
                "signup_rejected",
                extra={"email_hash": _log_hash(email), "reason": "invalid_invite"},
            )
            await write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={"reason": "invalid_invite"},
            )
            raise _bad("invalid_invite", "invite token not found", 403)
        now = datetime.now(timezone.utc)
        reason = _invite_validity_reason(invite, now)
        if reason is not None:
            verify_password(_DUMMY_PASSWORD_HASH, body.password)
            logger.info(
                "signup_rejected",
                extra={"email_hash": _log_hash(email), "reason": reason},
            )
            await write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={"reason": reason},
            )
            raise _bad("invalid_invite", f"invite is {reason}", 403)
        if invite.email is not None and invite.email.lower() != email:
            verify_password(_DUMMY_PASSWORD_HASH, body.password)
            logger.info(
                "signup_rejected",
                extra={"email_hash": _log_hash(email), "reason": "invite_email_mismatch"},
            )
            await write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={"reason": "invite_email_mismatch"},
            )
            raise _bad(
                "invite_email_mismatch",
                "this invite is bound to a different email",
                403,
            )
        role = invite.role or "member"

    user = User(
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name or email.split("@")[0],
        email_verified=False,
        role=role,
    )
    db.add(user)
    try:
        await db.flush()

        if invite is not None:
            # Mark invite consumed and ensure the email is allowlisted going forward.
            invite.used_at = datetime.now(timezone.utc)
            invite.used_by = user.id
            if not allow:
                db.add(AllowedEmail(email=email, invited_by=invite.created_by))

        session, _ = await _create_session(db, user, request)
        csrf = generate_csrf_token(session.id)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        logger.info(
            "signup_rejected",
            extra={"email_hash": _log_hash(email), "reason": "integrity_conflict"},
        )
        await write_audit_isolated(
            event_type="auth.signup.fail",
            actor_email=email,
            actor_ip_hash=request_ip_hash(request),
            details={"reason": "integrity_conflict"},
        )
        raise _bad("email_taken", "an account with this email already exists", 409) from exc

    logger.info(
        "signup_succeeded",
        extra={"email_hash": _log_hash(email), "user_id": user.id, "role": role},
    )
    await write_audit_isolated(
        event_type="auth.signup.success",
        user_id=user.id,
        actor_email=email,
        actor_ip_hash=request_ip_hash(request),
        details={"role": role},
    )
    _set_auth_cookies(response, session.id, csrf)
    return UserOut.model_validate(user)


@router.post(
    "/login",
    response_model=UserOut,
    dependencies=[Depends(AUTH_LOGIN_LIMITER)],
)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    email = body.email.strip().lower()
    user = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    password_hash = (
        user.password_hash
        if user is not None and user.deleted_at is None and user.password_hash
        else _DUMMY_PASSWORD_HASH
    )
    password_ok = verify_password(password_hash, body.password)
    if not user or user.deleted_at is not None or not password_ok:
        logger.info(
            "auth_failed",
            extra={
                "email_hash": _log_hash(email),
                "ip_hash": _log_hash(request.client.host if request.client else None),
            },
        )
        await write_audit_isolated(
            event_type="auth.login.fail",
            actor_email=email,
            actor_ip_hash=request_ip_hash(request),
            details={"reason": "invalid_credentials"},
        )
        raise _bad("invalid_credentials", "wrong email or password", 401)

    session, _ = await _create_session(db, user, request)
    csrf = generate_csrf_token(session.id)
    await write_audit(
        db,
        event_type="auth.login.success",
        user_id=user.id,
        actor_email=email,
        actor_ip_hash=request_ip_hash(request),
    )
    await db.commit()

    logger.info(
        "auth_succeeded",
        extra={"email_hash": _log_hash(email), "user_id": user.id},
    )
    _set_auth_cookies(response, session.id, csrf)
    return UserOut.model_validate(user)


@router.post("/password/reset-request", response_model=OkOut)
async def password_reset_request(
    body: PasswordResetRequestIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    email = body.email.strip().lower()
    redis = get_redis()
    await _PASSWORD_RESET_REQUEST_IP_LIMITER.check(
        redis, f"rl:pwd_reset_request:ip:{require_client_ip(request)}"
    )
    await _PASSWORD_RESET_REQUEST_EMAIL_LIMITER.check(
        redis, f"rl:pwd_reset_request:email:{_log_hash(email) or 'unknown'}"
    )
    user = (
        await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if user is None:
        return OkOut(ok=True)

    token = secrets.token_urlsafe(32)
    try:
        await redis.set(
            _password_reset_key(token),
            user.id,
            ex=_PASSWORD_RESET_TTL_SECONDS,
        )
    except Exception as exc:
        # Do not reveal whether the email exists; without mail integration this
        # endpoint remains a safe no-op if Redis is temporarily unavailable.
        logger.error(
            "password_reset_token_store_failed",
            extra={"email_hash": _log_hash(email), "user_id": user.id},
            exc_info=True,
        )
        raise _bad(
            "reset_unavailable",
            "password reset is temporarily unavailable",
            503,
        ) from exc
    return OkOut(ok=True)


@router.post("/password/reset-confirm", response_model=OkOut)
async def password_reset_confirm(
    body: PasswordResetConfirmIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    token = body.token.strip()
    if not token:
        raise _bad("invalid_token", "reset token is invalid or expired", 400)
    _validate_password_strength(body.new_password)

    redis = get_redis()
    await _PASSWORD_RESET_CONFIRM_IP_LIMITER.check(
        redis, f"rl:pwd_reset_confirm:ip:{require_client_ip(request)}"
    )
    await _PASSWORD_RESET_CONFIRM_TOKEN_LIMITER.check(
        redis, f"rl:pwd_reset_confirm:token:{hash_token(token)}"
    )
    key = _password_reset_key(token)
    try:
        # Atomically fetch and delete the reset token so it cannot be replayed.
        raw_user_id = await redis.getdel(key)
    except Exception as exc:
        logger.error("password_reset_token_lookup_failed", exc_info=True)
        raise _bad(
            "reset_unavailable",
            "password reset is temporarily unavailable",
            503,
        ) from exc

    if isinstance(raw_user_id, bytes):
        raw_user_id = raw_user_id.decode("utf-8")
    if not raw_user_id:
        raise _bad("invalid_token", "reset token is invalid or expired", 400)

    user = (
        await db.execute(
            select(User).where(User.id == raw_user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise _bad("invalid_token", "reset token is invalid or expired", 400)

    now = datetime.now(timezone.utc)
    user.password_hash = hash_password(body.new_password)
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await db.commit()
    return OkOut(ok=True)


async def _delete_password_reset_token(redis: Any, key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:
        logger.error("password_reset_token_delete_failed", exc_info=True)


@router.get("/csrf", response_model=CsrfOut)
async def refresh_csrf(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CsrfOut:
    sid = parse_session_cookie(request.cookies.get(SESSION_COOKIE))
    if not sid:
        raise _bad("unauthenticated", "missing or invalid session", 401)
    await require_active_session_user(request, db, sid)

    csrf = generate_csrf_token(sid)
    _set_csrf_cookie(response, csrf)
    response.headers["Cache-Control"] = "no-store"
    return CsrfOut(csrf_token=csrf)


@router.post("/logout", dependencies=[Depends(verify_csrf_session)])
async def logout(
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    _user: CurrentUser,
) -> dict[str, bool]:
    sid = getattr(request.state, "session_id", None)
    if sid:
        session = (
            await db.execute(
                select(AuthSession).where(AuthSession.id == sid).with_for_update()
            )
        ).scalar_one_or_none()
        if session and session.revoked_at is None:
            session.revoked_at = datetime.now(timezone.utc)
            await write_audit(
                db,
                event_type="auth.logout",
                user_id=getattr(_user, "id", None),
                actor_ip_hash=request_ip_hash(request),
            )
            await db.commit()
    logger.info(
        "auth_logout",
        extra={"user_id": getattr(_user, "id", None)},
    )
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
