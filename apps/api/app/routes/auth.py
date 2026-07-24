"""Auth 路由（DESIGN §5.1 简化版）。

V1 实现：signup / login / logout / me，以及最小密码重置后端。
不实现：OAuth、refresh rotation（session 直接用 cookie 引用 auth_sessions 行）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    AllowedEmail,
    AuthSession,
    InviteLink,
    PendingApiKeyVerification,
    User,
    UserApiCredential,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    LoginIn,
    NavigationVisibilityOut,
    RuntimeDefaultsOut,
    SignupByokIn,
    SignupIn,
    UserOut,
)

from ..audit import request_ip_hash, write_audit, write_audit_isolated
from ..byok_service import read_byok_settings, verification_token_hash
from ..config import effective_session_cookie_secure, settings
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
    AUTH_ADMIN_LOGIN_LIMITER,
    AUTH_LOGIN_LIMITER,
    AUTH_SIGNUP_LIMITER,
    RateLimiter,
    client_ip,
    require_client_ip,
)
from ..public_urls import resolve_public_base_url
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..services.email import EmailDeliveryError, send_password_reset_email


router = APIRouter()

logger = logging.getLogger(__name__)
_GENERATION_FAST_DEFAULT_KEY = "generation.fast_default"
_CANVAS_ENABLED_KEY = "canvas.enabled"
_NAV_VISIBILITY_SETTING_KEYS = {
    "studio": "ui.nav.studio_visible",
    "video": "ui.nav.video_visible",
    "projects": "ui.nav.projects_visible",
    "assets": "ui.nav.assets_visible",
}

# Why: strip control chars (incl. NUL/CR/LF/DEL) before persisting UA so log
# injection / DB driver quirks can't slip through user-controlled headers.
_UA_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$nwx+yaiP/NQqpodrnT3F9A"
    "$mmmttUtPlkaR5x78voo478doWSwYbHXVEUD9sfJkg9M"
)
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 128
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
_PASSWORD_RESET_CONFIRM_USER_LIMITER = RateLimiter(
    capacity=5, refill_per_sec=5 / 3600, always_on=True
)
_CLAIM_PASSWORD_RESET_TOKEN_LUA = """
local user_id = redis.call('GET', KEYS[1])
if not user_id then
  return {0, ''}
end
local ttl_ms = redis.call('PTTL', KEYS[1])
if ttl_ms <= 0 then
  return {0, ''}
end
if redis.call('EXISTS', KEYS[2]) ~= 0 then
  return {2, ''}
end
redis.call('HSET', KEYS[2], 'owner', ARGV[1], 'user_id', user_id)
redis.call('PEXPIRE', KEYS[2], ttl_ms)
redis.call('DEL', KEYS[1])
return {1, user_id}
"""
_RESTORE_PASSWORD_RESET_TOKEN_LUA = """
if redis.call('HGET', KEYS[2], 'owner') ~= ARGV[1] then
  return 0
end
local user_id = redis.call('HGET', KEYS[2], 'user_id')
local ttl_ms = redis.call('PTTL', KEYS[2])
if not user_id or ttl_ms <= 0 then
  return 0
end
if redis.call('SET', KEYS[1], user_id, 'PX', ttl_ms, 'NX') then
  redis.call('DEL', KEYS[2])
  return 1
end
return -1
"""
_CONSUME_PASSWORD_RESET_CLAIM_LUA = """
if redis.call('HGET', KEYS[1], 'owner') == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_DEV_ENVS = {"dev", "development", "local", "test"}
_BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE = (
    "verification failed; please verify your API key again"
)
_USER_EMAIL_INTEGRITY_MARKERS = (
    "uq_users_email_active",
    "users_email_key",
    "users.email",
)
_ALLOWED_EMAIL_INTEGRITY_MARKERS = (
    "allowed_emails.email",
    "allowed_emails_email_key",
)


def _sanitize_ua(raw: str | None) -> str:
    return _UA_CONTROL_CHARS.sub("", raw or "")[:1024]


def _log_hash(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:16]


def _bad(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


def _validate_password_strength(password: str) -> None:
    if len(password) < _MIN_PASSWORD_LEN:
        raise _bad(
            "weak_password",
            f"password must be at least {_MIN_PASSWORD_LEN} characters",
            400,
        )
    if len(password) > _MAX_PASSWORD_LEN:
        raise _bad(
            "password_too_long",
            f"password must be at most {_MAX_PASSWORD_LEN} characters",
            422,
        )


def _password_reset_key(token: str) -> str:
    return f"{_PASSWORD_RESET_KEY_PREFIX}:{hash_token(token)}"


def _password_reset_claim_key(token: str) -> str:
    return f"{_PASSWORD_RESET_KEY_PREFIX}:claim:{hash_token(token)}"


def _password_reset_url(token: str, public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}/reset-password/{token}"


def _redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _integrity_error_text(exc: IntegrityError) -> str:
    parts: list[str] = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
        diag = getattr(orig, "diag", None)
        if diag is not None:
            for attr in ("constraint_name", "table_name", "column_name"):
                value = getattr(diag, attr, None)
                if value:
                    parts.append(str(value))
    return " ".join(parts).lower()


def _integrity_error_matches(exc: IntegrityError, markers: tuple[str, ...]) -> bool:
    text = _integrity_error_text(exc)
    return any(marker in text for marker in markers)


async def _reject_byok_signup(
    *,
    request: Request,
    email: str,
    password: str,
    reason: str,
    code: str,
    message: str,
    status_code: int,
) -> None:
    verify_password(_DUMMY_PASSWORD_HASH, password)
    await write_audit_isolated(
        event_type="auth.signup.byok.fail",
        actor_email=email,
        actor_ip_hash=request_ip_hash(request),
        details={"reason": reason},
    )
    raise _bad(code, message, status_code)


async def _validated_byok_pending(
    db: AsyncSession,
    *,
    request: Request,
    email: str,
    password: str,
    token: str,
) -> tuple[PendingApiKeyVerification, datetime]:
    existing = (
        await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if existing is not None:
        await _reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="email_taken_masked_as_invalid_token",
            code="invalid_verification_token",
            message=_BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE,
            status_code=400,
        )

    token_hash = verification_token_hash(token)
    pending = (
        await db.execute(
            select(PendingApiKeyVerification)
            .where(PendingApiKeyVerification.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if (
        pending is None
        or pending.consumed_at is not None
        or ensure_utc(pending.expires_at) <= now
    ):
        await _reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="invalid_verification_token",
            code="invalid_verification_token",
            message=_BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE,
            status_code=400,
        )
    return pending, now


async def _byok_signup_access(
    db: AsyncSession,
    *,
    body: SignupByokIn,
    request: Request,
    email: str,
    password: str,
    now: datetime,
    bypasses_allowlist: bool,
) -> tuple[AllowedEmail | None, InviteLink | None, str]:
    allow = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()
    if bypasses_allowlist or allow is not None:
        return allow, None, "member"
    if not body.invite_token:
        await _reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="email_not_invited",
            code="email_not_invited",
            message="this email is not on the invite allowlist",
            status_code=403,
        )

    invite_row = (
        await db.execute(
            select(InviteLink, User)
            .join(User, User.id == InviteLink.created_by, isouter=True)
            .where(InviteLink.token == body.invite_token)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).first()
    invite = invite_row[0] if invite_row is not None else None
    invite_creator = invite_row[1] if invite_row is not None else None
    if invite is None:
        await _reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="invalid_invite",
            code="invalid_invite",
            message="invite token not found",
            status_code=403,
        )
    reason = _invite_validity_reason(invite, now, invite_creator)
    if reason is not None:
        await _reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason=reason,
            code="invalid_invite",
            message=f"invite is {reason}",
            status_code=403,
        )
    if invite.email is not None and invite.email.lower() != email:
        await _reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="invite_email_mismatch",
            code="invite_email_mismatch",
            message="this invite is bound to a different email",
            status_code=403,
        )
    return allow, invite, invite.role or "member"


async def _claim_password_reset_token(
    redis: Any,
    token_key: str,
    claim_key: str,
    *,
    owner: str,
) -> str | None:
    result = await redis.eval(
        _CLAIM_PASSWORD_RESET_TOKEN_LUA,
        2,
        token_key,
        claim_key,
        owner,
    )
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        raise RuntimeError("unexpected password reset claim response")
    if int(result[0]) != 1:
        return None
    user_id = _redis_text(result[1])
    if not user_id:
        raise RuntimeError("password reset claim omitted user id")
    return user_id


async def _restore_password_reset_token(
    redis: Any,
    token_key: str,
    claim_key: str,
    *,
    owner: str,
) -> bool:
    result = await redis.eval(
        _RESTORE_PASSWORD_RESET_TOKEN_LUA,
        2,
        token_key,
        claim_key,
        owner,
    )
    return int(result) == 1


async def _consume_password_reset_claim(
    redis: Any,
    claim_key: str,
    *,
    owner: str,
) -> None:
    try:
        await redis.eval(
            _CONSUME_PASSWORD_RESET_CLAIM_LUA,
            1,
            claim_key,
            owner,
        )
    except Exception:
        # The original token was removed before the DB transaction began.
        # Leaving this owner-bound claim to expire cannot make it reusable.
        logger.warning("password_reset_claim_consume_failed", exc_info=True)


def _is_dev_env() -> bool:
    return settings.app_env.strip().lower() in _DEV_ENVS


def _cookie_secure() -> bool:
    return effective_session_cookie_secure(settings)


def _cookie_samesite() -> Literal["lax", "strict"]:
    return "lax" if _is_dev_env() else "strict"


def _set_auth_cookies(response: Response, session_id: str, csrf: str) -> None:
    max_age = settings.session_ttl_min * 60
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(session_id),
        max_age=max_age,
        httponly=True,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),
        path="/",
    )
    _set_csrf_cookie(response, csrf)


def _set_csrf_cookie(response: Response, csrf: str) -> None:
    max_age = settings.session_ttl_min * 60
    # CSRF must be readable by JS (double-submit). Not httponly.
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        secure=_cookie_secure(),
        samesite=_cookie_samesite(),
        path="/",
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=_cookie_secure(),
        httponly=True,
        samesite=_cookie_samesite(),
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=_cookie_secure(),
        httponly=False,
        samesite=_cookie_samesite(),
    )


class CsrfOut(BaseModel):
    csrf_token: str


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(max_length=_MAX_PASSWORD_LEN)


class OkOut(BaseModel):
    ok: bool


async def _runtime_defaults(db: AsyncSession) -> RuntimeDefaultsOut:
    from .images import MAX_BYTES as IMAGE_UPLOAD_MAX_BYTES

    # 默认 fast=True：未配置 generation.fast_default 或值不是 "0"/"1" 时
    # 走 V1 体验偏好（Fast 模式默认开启）。get_setting 已包含 env fallback。
    fast_default = True
    spec = get_spec(_GENERATION_FAST_DEFAULT_KEY)
    if spec is not None:
        raw = await get_setting(db, spec)
        if raw in {"0", "1"}:
            fast_default = raw == "1"
    nav_visibility: dict[str, bool] = {}
    for nav_key, setting_key in _NAV_VISIBILITY_SETTING_KEYS.items():
        visible = True
        nav_spec = get_spec(setting_key)
        if nav_spec is not None:
            raw = await get_setting(db, nav_spec)
            if raw in {"0", "1"}:
                visible = raw == "1"
        nav_visibility[nav_key] = visible
    canvas_enabled = False
    canvas_spec = get_spec(_CANVAS_ENABLED_KEY)
    if canvas_spec is not None:
        canvas_enabled = await get_setting(db, canvas_spec) == "1"
    return RuntimeDefaultsOut(
        fast=fast_default,
        upload_max_source_bytes=IMAGE_UPLOAD_MAX_BYTES,
        canvas_enabled=canvas_enabled,
        nav_visibility=NavigationVisibilityOut(**nav_visibility),
    )


async def _user_out_with_runtime_defaults(
    user: User,
    db: AsyncSession,
) -> UserOut:
    out = UserOut.model_validate(user)
    out.runtime_defaults = await _runtime_defaults(db)
    return out


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
        expires_at=datetime.now(timezone.utc)
        + timedelta(minutes=settings.session_ttl_min),
    )
    db.add(session)
    await db.flush()
    return session, refresh


def _invite_validity_reason(
    inv: InviteLink, now: datetime, creator: User | None = None
) -> str | None:
    """Return None if the invite is currently usable; else a short reason."""
    if inv.revoked_at is not None:
        return "revoked"
    if inv.used_at is not None:
        return "used"
    if inv.expires_at is not None and ensure_utc(inv.expires_at) <= now:
        return "expired"
    if creator is None or creator.deleted_at is not None:
        return "creator_deleted"
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
        await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
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
        invite_row = (
            await db.execute(
                select(InviteLink, User)
                .join(User, User.id == InviteLink.created_by, isouter=True)
                .where(InviteLink.token == body.invite_token)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).first()
        invite = invite_row[0] if invite_row is not None else None
        invite_creator = invite_row[1] if invite_row is not None else None
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
        reason = _invite_validity_reason(invite, now, invite_creator)
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
                extra={
                    "email_hash": _log_hash(email),
                    "reason": "invite_email_mismatch",
                },
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
        if _integrity_error_matches(exc, _USER_EMAIL_INTEGRITY_MARKERS):
            logger.info(
                "signup_rejected",
                extra={"email_hash": _log_hash(email), "reason": "email_taken_race"},
            )
            await write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={"reason": "email_taken"},
            )
            raise _bad(
                "email_taken", "an account with this email already exists", 409
            ) from exc
        if _integrity_error_matches(exc, _ALLOWED_EMAIL_INTEGRITY_MARKERS):
            logger.info(
                "signup_rejected",
                extra={
                    "email_hash": _log_hash(email),
                    "reason": "allowlist_integrity_conflict",
                },
            )
            await write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={"reason": "allowlist_integrity_conflict"},
            )
            raise _bad(
                "signup_conflict",
                "signup could not be completed; please retry",
                409,
            ) from exc
        logger.exception(
            "signup_integrity_error",
            extra={"email_hash": _log_hash(email)},
        )
        await write_audit_isolated(
            event_type="auth.signup.fail",
            actor_email=email,
            actor_ip_hash=request_ip_hash(request),
            details={"reason": "integrity_conflict_unclassified"},
        )
        raise _bad(
            "signup_unavailable",
            "signup is temporarily unavailable",
            503,
        ) from exc

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
    return await _user_out_with_runtime_defaults(user, db)


@router.post(
    "/signup/byok",
    response_model=UserOut,
    dependencies=[Depends(AUTH_SIGNUP_LIMITER)],
)
async def signup_byok(
    body: SignupByokIn,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    byok_settings = await read_byok_settings(db)
    if not byok_settings.mode_enabled or not byok_settings.byok_signup_enabled:
        raise _bad("byok_disabled", "BYOK signup is disabled", 403)

    email = body.email.strip().lower()
    if not email or not body.password:
        raise _bad("invalid_input", "email and password are required", 422)
    _validate_password_strength(body.password)

    token = body.verification_token.strip()
    if not token:
        raise _bad("invalid_verification_token", "verification token is invalid", 400)

    # Why: do the email-existence check *before* the token check, and collapse
    # both branches into the same generic `invalid_verification_token`. This
    # closes the user-enumeration side channel from §8.3 — an attacker can no
    # longer probe whether `email_taken` ever fires (vs token-expired). The
    # pending token is *not consumed* in either failure path so a legitimate
    # user who got the token via a different account can still finish signup
    # by changing the email.
    pending, now = await _validated_byok_pending(
        db,
        request=request,
        email=email,
        password=body.password,
        token=token,
    )
    allow, invite, role = await _byok_signup_access(
        db,
        body=body,
        request=request,
        email=email,
        password=body.password,
        now=now,
        bypasses_allowlist=byok_settings.byok_signup_bypasses_allowlist,
    )

    user = User(
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name or email.split("@")[0],
        email_verified=False,
        role=role,
        account_mode="byok",
    )
    db.add(user)
    try:
        await db.flush()
        credential = UserApiCredential(
            user_id=user.id,
            supplier_id=pending.supplier_id,
            key_ciphertext=pending.key_ciphertext,
            key_hash=pending.key_hash,
            key_hint=pending.key_hint,
            status="active",
            last_verified_at=pending.verified_at,
            capabilities_jsonb={},
        )
        db.add(credential)
        pending.consumed_at = now

        if invite is not None:
            invite.used_at = now
            invite.used_by = user.id
            if not allow:
                db.add(AllowedEmail(email=email, invited_by=invite.created_by))
        elif byok_settings.byok_signup_bypasses_allowlist and not allow:
            # Why: when allowlist is bypassed via BYOK, still record the email
            # in AllowedEmail so subsequent re-signups / OAuth callbacks have a
            # consistent allowlist view (matches the invite branch above).
            # invited_by=None marks the row as bypass-sourced.
            db.add(AllowedEmail(email=email, invited_by=None))

        session, _ = await _create_session(db, user, request)
        csrf = generate_csrf_token(session.id)
        # Why: write_audit(autocommit=False) returns False on failure and does
        # NOT raise. If session-bound audit fails the success row would be lost.
        # Fall back to write_audit_isolated (independent transaction) so the
        # audit row survives even when the caller's session has issues.
        audit_ok = await write_audit(
            db,
            event_type="auth.signup.byok.success",
            user_id=user.id,
            actor_email=email,
            actor_ip_hash=request_ip_hash(request),
            details={"role": role, "supplier_id": pending.supplier_id},
            autocommit=False,
        )
        await db.commit()
        if not audit_ok:
            # The user is committed; surface the audit gap via isolated retry.
            await write_audit_isolated(
                event_type="auth.signup.byok.success",
                user_id=user.id,
                actor_email=email,
                actor_ip_hash=request_ip_hash(request),
                details={
                    "role": role,
                    "supplier_id": pending.supplier_id,
                    "audit_fallback": True,
                },
            )
    except IntegrityError as exc:
        await db.rollback()
        await write_audit_isolated(
            event_type="auth.signup.byok.fail",
            actor_email=email,
            actor_ip_hash=request_ip_hash(request),
            details={"reason": "integrity_conflict_masked_as_invalid_token"},
        )
        raise _bad(
            "invalid_verification_token",
            _BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE,
            400,
        ) from exc

    _set_auth_cookies(response, session.id, csrf)
    return await _user_out_with_runtime_defaults(user, db)


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
    admin_login_key = (
        f"rl:auth:admin_login:{require_client_ip(request)}:"
        f"{_log_hash(email) or 'unknown'}"
    )
    await AUTH_ADMIN_LOGIN_LIMITER.check(
        get_redis(),
        admin_login_key,
    )
    user = (
        await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    password_hash = (
        user.password_hash
        if user is not None and user.password_hash
        else _DUMMY_PASSWORD_HASH
    )
    password_ok = verify_password(password_hash, body.password)
    if not user or not password_ok:
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
    return await _user_out_with_runtime_defaults(user, db)


@router.post("/password/reset-request", response_model=OkOut)
async def password_reset_request(
    body: PasswordResetRequestIn,
    request: Request,
    background_tasks: BackgroundTasks,
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
    try:
        public_base_url = await resolve_public_base_url(request, db)
    except Exception:
        logger.exception(
            "password_reset_public_base_url_failed",
            extra={"email_hash": _log_hash(email)},
        )
        return OkOut(ok=True)
    if user is None:
        return OkOut(ok=True)

    token = secrets.token_urlsafe(32)
    key = _password_reset_key(token)
    reset_url = _password_reset_url(token, public_base_url)
    try:
        await redis.set(
            key,
            user.id,
            ex=_PASSWORD_RESET_TTL_SECONDS,
        )
    except Exception:
        logger.exception(
            "password_reset_token_store_failed",
            extra={"email_hash": _log_hash(email), "user_id": user.id},
        )
        return OkOut(ok=True)
    background_tasks.add_task(
        _send_password_reset_email_or_delete,
        redis,
        key,
        email,
        user.id,
        reset_url,
    )
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
    key = _password_reset_key(token)
    await _PASSWORD_RESET_CONFIRM_TOKEN_LIMITER.check(
        redis, f"rl:pwd_reset_confirm:token:{hash_token(token)}"
    )
    claim_key = _password_reset_claim_key(token)
    claim_owner = secrets.token_urlsafe(24)
    try:
        raw_user_id = await _claim_password_reset_token(
            redis,
            key,
            claim_key,
            owner=claim_owner,
        )
    except Exception as exc:
        logger.error("password_reset_token_claim_failed", exc_info=True)
        raise _bad(
            "reset_unavailable",
            "password reset is temporarily unavailable",
            503,
        ) from exc
    if not raw_user_id:
        raise _bad("invalid_token", "reset token is invalid or expired", 400)

    try:
        await _PASSWORD_RESET_CONFIRM_USER_LIMITER.check(
            redis, f"rl:pwd_reset_confirm:user:{raw_user_id}"
        )
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
    except Exception:
        rollback_succeeded = False
        try:
            await db.rollback()
            rollback_succeeded = True
        except Exception:
            logger.error("password_reset_db_rollback_failed", exc_info=True)

        if rollback_succeeded:
            try:
                restored = await _restore_password_reset_token(
                    redis,
                    key,
                    claim_key,
                    owner=claim_owner,
                )
            except Exception:
                logger.error("password_reset_token_restore_failed", exc_info=True)
            else:
                if not restored:
                    logger.error("password_reset_token_restore_rejected")
        raise

    try:
        await db.commit()
    except Exception as exc:
        # A commit exception does not prove the transaction failed: the database
        # may have durably applied it before the client lost the acknowledgement.
        # Never restore the already-claimed token across this uncertainty.
        try:
            await db.rollback()
        except Exception:
            logger.error("password_reset_db_rollback_failed", exc_info=True)
        await _consume_password_reset_claim(
            redis,
            claim_key,
            owner=claim_owner,
        )
        logger.error(
            "password_reset_commit_outcome_uncertain",
            extra={"user_id": raw_user_id},
            exc_info=True,
        )
        raise _bad(
            "reset_outcome_uncertain",
            "password reset result is uncertain; request a new reset link before retrying",
            503,
        ) from exc

    await _consume_password_reset_claim(
        redis,
        claim_key,
        owner=claim_owner,
    )
    return OkOut(ok=True)


async def _delete_password_reset_token(redis: Any, key: str) -> None:
    try:
        await redis.delete(key)
    except Exception:
        logger.error("password_reset_token_delete_failed", exc_info=True)


async def _send_password_reset_email_or_delete(
    redis: Any,
    key: str,
    email: str,
    user_id: str,
    reset_url: str,
) -> None:
    try:
        await send_password_reset_email(
            to_email=email,
            reset_url=reset_url,
            expires_minutes=_PASSWORD_RESET_TTL_SECONDS // 60,
        )
    except EmailDeliveryError:
        await _delete_password_reset_token(redis, key)
        logger.error(
            "password_reset_email_delivery_failed",
            extra={"email_hash": _log_hash(email), "user_id": user_id},
            exc_info=True,
        )
    except Exception:
        await _delete_password_reset_token(redis, key)
        logger.exception(
            "password_reset_email_unexpected_failed",
            extra={"email_hash": _log_hash(email), "user_id": user_id},
        )


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
async def me(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UserOut:
    return await _user_out_with_runtime_defaults(user, db)
