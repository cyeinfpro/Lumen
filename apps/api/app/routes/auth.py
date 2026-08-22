# ruff: noqa: F401
"""Auth 路由与历史兼容 facade。

V1 实现：signup / login / logout / me，以及最小密码重置后端。
不实现：OAuth、refresh rotation（session 直接用 cookie 引用 auth_sessions 行）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel
from sqlalchemy import select
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
from ..public_urls import resolve_public_base_url
from ..ratelimit import (
    AUTH_ADMIN_LOGIN_LIMITER,
    AUTH_LOGIN_LIMITER,
    AUTH_SIGNUP_LIMITER,
    RateLimiter,
    client_ip,
    require_client_ip,
)
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..security import (
    generate_csrf_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    make_session_cookie,
    parse_session_cookie,
    verify_password,
)
from ..services.email import EmailDeliveryError, send_password_reset_email
from .auth_parts import password_reset as _password_reset
from .auth_parts import runtime_defaults as _runtime_defaults_part
from .auth_parts import signup as _signup
from .auth_parts.runtime import AuthRuntimeAdapter


router = APIRouter()

logger = logging.getLogger(__name__)
_GENERATION_FAST_DEFAULT_KEY = "generation.fast_default"
_CANVAS_ENABLED_KEY = "canvas.enabled"
_AGENT_ENABLED_KEY = "agent.enabled"
_NAV_VISIBILITY_SETTING_KEYS = MappingProxyType(
    {
        "studio": "ui.nav.studio_visible",
        "agent": "ui.nav.agent_visible",
        "video": "ui.nav.video_visible",
        "projects": "ui.nav.projects_visible",
        "assets": "ui.nav.assets_visible",
    }
)

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
_PASSWORD_RESET_TTL_SECONDS = _password_reset.PASSWORD_RESET_TTL_SECONDS
_PASSWORD_RESET_KEY_PREFIX = _password_reset.PASSWORD_RESET_KEY_PREFIX
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
_CLAIM_PASSWORD_RESET_TOKEN_LUA = (
    _password_reset.CLAIM_PASSWORD_RESET_TOKEN_LUA
)
_RESTORE_PASSWORD_RESET_TOKEN_LUA = (
    _password_reset.RESTORE_PASSWORD_RESET_TOKEN_LUA
)
_CONSUME_PASSWORD_RESET_CLAIM_LUA = (
    _password_reset.CONSUME_PASSWORD_RESET_CLAIM_LUA
)
_DEV_ENVS = frozenset({"dev", "development", "local", "test"})
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

_SignupAccess = _signup.SignupAccess
PasswordResetRequestIn = _password_reset.PasswordResetRequestIn
PasswordResetConfirmIn = _password_reset.PasswordResetConfirmIn
OkOut = _password_reset.OkOut
_runtime = AuthRuntimeAdapter(sys.modules[__name__])


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
    return _password_reset.password_reset_key(_runtime, token)


def _password_reset_claim_key(token: str) -> str:
    return _password_reset.password_reset_claim_key(_runtime, token)


def _password_reset_url(token: str, public_base_url: str) -> str:
    return _password_reset.password_reset_url(token, public_base_url)


def _redis_text(value: Any) -> str | None:
    return _password_reset.redis_text(value)


def _integrity_error_text(exc: IntegrityError) -> str:
    return _signup.integrity_error_text(exc)


def _integrity_error_matches(exc: IntegrityError, markers: tuple[str, ...]) -> bool:
    return _signup.integrity_error_matches(exc, markers)


async def _reject_signup(
    *,
    request: Request,
    email: str,
    password: str,
    reason: str,
    code: str,
    message: str,
    status_code: int,
) -> None:
    await _signup.reject_signup(
        _runtime,
        request=request,
        email=email,
        password=password,
        reason=reason,
        code=code,
        message=message,
        status_code=status_code,
    )


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
    await _signup.reject_byok_signup(
        _runtime,
        request=request,
        email=email,
        password=password,
        reason=reason,
        code=code,
        message=message,
        status_code=status_code,
    )


async def _validated_byok_pending(
    db: AsyncSession,
    *,
    request: Request,
    email: str,
    password: str,
    token: str,
) -> tuple[PendingApiKeyVerification, datetime]:
    return await _signup.validated_byok_pending(
        _runtime,
        db,
        request=request,
        email=email,
        password=password,
        token=token,
    )


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
    return await _signup.byok_signup_access(
        _runtime,
        db,
        body=body,
        request=request,
        email=email,
        password=password,
        now=now,
        bypasses_allowlist=bypasses_allowlist,
    )


async def _claim_password_reset_token(
    redis: Any,
    token_key: str,
    claim_key: str,
    *,
    owner: str,
) -> str | None:
    return await _password_reset.claim_password_reset_token(
        _runtime,
        redis,
        token_key,
        claim_key,
        owner=owner,
    )


async def _restore_password_reset_token(
    redis: Any,
    token_key: str,
    claim_key: str,
    *,
    owner: str,
) -> bool:
    return await _password_reset.restore_password_reset_token(
        _runtime,
        redis,
        token_key,
        claim_key,
        owner=owner,
    )


async def _consume_password_reset_claim(
    redis: Any,
    claim_key: str,
    *,
    owner: str,
) -> None:
    await _password_reset.consume_password_reset_claim(
        _runtime,
        redis,
        claim_key,
        owner=owner,
    )


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


RuntimeDefaultsProvider = _runtime_defaults_part.RuntimeDefaultsProvider


class _DatabaseRuntimeDefaultsProvider(
    _runtime_defaults_part.DatabaseRuntimeDefaultsProvider
):
    def __init__(self, db: AsyncSession) -> None:
        super().__init__(_runtime, db)


async def _runtime_defaults(db: AsyncSession) -> RuntimeDefaultsOut:
    provider: RuntimeDefaultsProvider = _DatabaseRuntimeDefaultsProvider(db)
    return await provider.load()


def _record_runtime_defaults_degraded() -> None:
    if not settings.metrics_enabled:
        return
    from prometheus_client import REGISTRY, Counter

    name = "lumen_auth_runtime_defaults_degraded"
    counter = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
    if counter is None:
        counter = Counter(
            name,
            "Number of auth responses that used safe runtime defaults.",
        )
    counter.inc()


def _user_out_snapshot(user: User) -> UserOut:
    return _runtime_defaults_part.user_out_snapshot(user)


def _auth_response_snapshot(
    user: User,
    session: AuthSession,
) -> tuple[str, UserOut]:
    return _runtime_defaults_part.auth_response_snapshot(
        _runtime,
        user,
        session,
    )


async def _user_out_with_runtime_defaults(
    user: User | UserOut,
    db: AsyncSession,
) -> UserOut:
    out = (
        user.model_copy(deep=True)
        if isinstance(user, UserOut)
        else _user_out_snapshot(user)
    )
    try:
        out.runtime_defaults = await _runtime_defaults(db)
    except Exception:
        logger.warning(
            "auth_runtime_defaults_degraded",
            extra={"user_id": out.id},
            exc_info=True,
        )
        try:
            _record_runtime_defaults_degraded()
        except Exception:
            logger.warning(
                "auth_runtime_defaults_degraded_metric_failed",
                extra={"user_id": out.id},
                exc_info=True,
            )
        out.runtime_defaults = _DatabaseRuntimeDefaultsProvider.safe_defaults()
    return out


async def _write_post_commit_audit_best_effort(
    *,
    event_type: str,
    user_id: str,
    actor_email: str,
    actor_ip_hash: str | None,
    details: dict[str, Any],
) -> None:
    try:
        await write_audit_isolated(
            event_type=event_type,
            user_id=user_id,
            actor_email=actor_email,
            actor_ip_hash=actor_ip_hash,
            details=details,
        )
    except Exception:
        logger.exception(
            "auth_post_commit_audit_failed",
            extra={"event_type": event_type, "user_id": user_id},
        )


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
    return _signup.invite_validity_reason(_runtime, inv, now, creator)


async def _standard_signup_access(
    db: AsyncSession,
    body: SignupIn,
    request: Request,
    email: str,
) -> _SignupAccess:
    return await _signup.standard_signup_access(
        _runtime,
        db,
        body,
        request,
        email,
    )


async def _persist_standard_signup(
    db: AsyncSession,
    body: SignupIn,
    request: Request,
    email: str,
    access: _SignupAccess,
) -> tuple[User, AuthSession, str, UserOut]:
    return await _signup.persist_standard_signup(
        _runtime,
        db,
        body,
        request,
        email,
        access,
    )


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

    access = await _standard_signup_access(db, body, request, email)
    user, session, csrf, user_out = await _persist_standard_signup(
        db,
        body,
        request,
        email,
        access,
    )
    _set_auth_cookies(response, session.id, csrf)
    logger.info(
        "signup_succeeded",
        extra={
            "email_hash": _log_hash(email),
            "user_id": user.id,
            "role": access.role,
        },
    )
    await _write_post_commit_audit_best_effort(
        event_type="auth.signup.success",
        user_id=user.id,
        actor_email=email,
        actor_ip_hash=request_ip_hash(request),
        details={"role": access.role},
    )
    return await _user_out_with_runtime_defaults(user_out, db)


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
    return await _signup.signup_byok(
        _runtime,
        body=body,
        request=request,
        response=response,
        db=db,
    )


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
    csrf, user_out = _auth_response_snapshot(user, session)
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
    return await _user_out_with_runtime_defaults(user_out, db)


@router.post("/password/reset-request", response_model=OkOut)
async def password_reset_request(
    body: PasswordResetRequestIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    return await _password_reset.password_reset_request(
        _runtime,
        body=body,
        request=request,
        background_tasks=background_tasks,
        db=db,
    )


@router.post("/password/reset-confirm", response_model=OkOut)
async def password_reset_confirm(
    body: PasswordResetConfirmIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OkOut:
    return await _password_reset.password_reset_confirm(
        _runtime,
        body=body,
        request=request,
        db=db,
    )


async def _delete_password_reset_token(redis: Any, key: str) -> None:
    await _password_reset.delete_password_reset_token(_runtime, redis, key)


async def _send_password_reset_email_or_delete(
    redis: Any,
    key: str,
    email: str,
    user_id: str,
    reset_url: str,
) -> None:
    await _password_reset.send_password_reset_email_or_delete(
        _runtime,
        redis,
        key,
        email,
        user_id,
        reset_url,
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
