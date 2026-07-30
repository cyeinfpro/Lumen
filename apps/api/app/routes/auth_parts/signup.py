"""Signup access checks and persistence for the authentication facade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Request, Response
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
from lumen_core.schemas import SignupByokIn, SignupIn, UserOut

from .runtime import AuthRuntimeAdapter


@dataclass(frozen=True, slots=True)
class SignupAccess:
    allow: AllowedEmail | None
    invite: InviteLink | None
    role: str


def integrity_error_text(exc: IntegrityError) -> str:
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


def integrity_error_matches(
    exc: IntegrityError,
    markers: tuple[str, ...],
) -> bool:
    text = integrity_error_text(exc)
    return any(marker in text for marker in markers)


def invite_validity_reason(
    runtime: AuthRuntimeAdapter,
    invite: InviteLink,
    now: datetime,
    creator: User | None = None,
) -> str | None:
    if invite.revoked_at is not None:
        return "revoked"
    if invite.used_at is not None:
        return "used"
    if invite.expires_at is not None and runtime.ensure_utc(invite.expires_at) <= now:
        return "expired"
    if creator is None or creator.deleted_at is not None:
        return "creator_deleted"
    return None


async def reject_signup(
    runtime: AuthRuntimeAdapter,
    *,
    request: Request,
    email: str,
    password: str,
    reason: str,
    code: str,
    message: str,
    status_code: int,
) -> None:
    runtime.verify_password(runtime._DUMMY_PASSWORD_HASH, password)
    runtime.logger.info(
        "signup_rejected",
        extra={"email_hash": runtime._log_hash(email), "reason": reason},
    )
    await runtime.write_audit_isolated(
        event_type="auth.signup.fail",
        actor_email=email,
        actor_ip_hash=runtime.request_ip_hash(request),
        details={"reason": reason},
    )
    raise runtime._bad(code, message, status_code)


async def reject_byok_signup(
    runtime: AuthRuntimeAdapter,
    *,
    request: Request,
    email: str,
    password: str,
    reason: str,
    code: str,
    message: str,
    status_code: int,
) -> None:
    runtime.verify_password(runtime._DUMMY_PASSWORD_HASH, password)
    await runtime.write_audit_isolated(
        event_type="auth.signup.byok.fail",
        actor_email=email,
        actor_ip_hash=runtime.request_ip_hash(request),
        details={"reason": reason},
    )
    raise runtime._bad(code, message, status_code)


async def validated_byok_pending(
    runtime: AuthRuntimeAdapter,
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
        await runtime._reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="email_taken_masked_as_invalid_token",
            code="invalid_verification_token",
            message=runtime._BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE,
            status_code=400,
        )

    token_hash = runtime.verification_token_hash(token)
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
        or runtime.ensure_utc(pending.expires_at) <= now
    ):
        await runtime._reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="invalid_verification_token",
            code="invalid_verification_token",
            message=runtime._BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE,
            status_code=400,
        )
    return pending, now


async def byok_signup_access(
    runtime: AuthRuntimeAdapter,
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
        await runtime._reject_byok_signup(
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
        await runtime._reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="invalid_invite",
            code="invalid_invite",
            message="invite token not found",
            status_code=403,
        )
    reason = runtime._invite_validity_reason(invite, now, invite_creator)
    if reason is not None:
        await runtime._reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason=reason,
            code="invalid_invite",
            message=f"invite is {reason}",
            status_code=403,
        )
    if invite.email is not None and invite.email.lower() != email:
        await runtime._reject_byok_signup(
            request=request,
            email=email,
            password=password,
            reason="invite_email_mismatch",
            code="invite_email_mismatch",
            message="this invite is bound to a different email",
            status_code=403,
        )
    return allow, invite, invite.role or "member"


async def standard_signup_access(
    runtime: AuthRuntimeAdapter,
    db: AsyncSession,
    body: SignupIn,
    request: Request,
    email: str,
) -> SignupAccess:
    existing = (
        await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if existing:
        await runtime._reject_signup(
            request=request,
            email=email,
            password=body.password,
            reason="email_taken",
            code="email_taken",
            message="an account with this email already exists",
            status_code=409,
        )

    allow = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == email))
    ).scalar_one_or_none()
    if allow is not None:
        return SignupAccess(allow=allow, invite=None, role="member")
    if not body.invite_token:
        await runtime._reject_signup(
            request=request,
            email=email,
            password=body.password,
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
        await runtime._reject_signup(
            request=request,
            email=email,
            password=body.password,
            reason="invalid_invite",
            code="invalid_invite",
            message="invite token not found",
            status_code=403,
        )
    reason = runtime._invite_validity_reason(
        invite,
        datetime.now(timezone.utc),
        invite_creator,
    )
    if reason is not None:
        await runtime._reject_signup(
            request=request,
            email=email,
            password=body.password,
            reason=reason,
            code="invalid_invite",
            message=f"invite is {reason}",
            status_code=403,
        )
    if invite.email is not None and invite.email.lower() != email:
        await runtime._reject_signup(
            request=request,
            email=email,
            password=body.password,
            reason="invite_email_mismatch",
            code="invite_email_mismatch",
            message="this invite is bound to a different email",
            status_code=403,
        )
    return SignupAccess(allow=None, invite=invite, role=invite.role or "member")


async def persist_standard_signup(
    runtime: AuthRuntimeAdapter,
    db: AsyncSession,
    body: SignupIn,
    request: Request,
    email: str,
    access: SignupAccess,
) -> tuple[User, AuthSession, str, UserOut]:
    user = User(
        email=email,
        password_hash=runtime.hash_password(body.password),
        display_name=body.display_name or email.split("@")[0],
        email_verified=False,
        role=access.role,
    )
    db.add(user)
    try:
        await db.flush()
        if access.invite is not None:
            access.invite.used_at = datetime.now(timezone.utc)
            access.invite.used_by = user.id
            if access.allow is None:
                db.add(
                    AllowedEmail(
                        email=email,
                        invited_by=access.invite.created_by,
                    )
                )
        session, _ = await runtime._create_session(db, user, request)
        csrf, user_out = runtime._auth_response_snapshot(user, session)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if runtime._integrity_error_matches(
            exc,
            runtime._USER_EMAIL_INTEGRITY_MARKERS,
        ):
            runtime.logger.info(
                "signup_rejected",
                extra={
                    "email_hash": runtime._log_hash(email),
                    "reason": "email_taken_race",
                },
            )
            await runtime.write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=runtime.request_ip_hash(request),
                details={"reason": "email_taken"},
            )
            raise runtime._bad(
                "email_taken",
                "an account with this email already exists",
                409,
            ) from exc
        if runtime._integrity_error_matches(
            exc,
            runtime._ALLOWED_EMAIL_INTEGRITY_MARKERS,
        ):
            runtime.logger.info(
                "signup_rejected",
                extra={
                    "email_hash": runtime._log_hash(email),
                    "reason": "allowlist_integrity_conflict",
                },
            )
            await runtime.write_audit_isolated(
                event_type="auth.signup.fail",
                actor_email=email,
                actor_ip_hash=runtime.request_ip_hash(request),
                details={"reason": "allowlist_integrity_conflict"},
            )
            raise runtime._bad(
                "signup_conflict",
                "signup could not be completed; please retry",
                409,
            ) from exc
        runtime.logger.exception(
            "signup_integrity_error",
            extra={"email_hash": runtime._log_hash(email)},
        )
        await runtime.write_audit_isolated(
            event_type="auth.signup.fail",
            actor_email=email,
            actor_ip_hash=runtime.request_ip_hash(request),
            details={"reason": "integrity_conflict_unclassified"},
        )
        raise runtime._bad(
            "signup_unavailable",
            "signup is temporarily unavailable",
            503,
        ) from exc
    return user, session, csrf, user_out


async def signup_byok(
    runtime: AuthRuntimeAdapter,
    *,
    body: SignupByokIn,
    request: Request,
    response: Response,
    db: AsyncSession,
) -> UserOut:
    byok_settings = await runtime.read_byok_settings(db)
    if not byok_settings.mode_enabled or not byok_settings.byok_signup_enabled:
        raise runtime._bad("byok_disabled", "BYOK signup is disabled", 403)

    email = body.email.strip().lower()
    if not email or not body.password:
        raise runtime._bad("invalid_input", "email and password are required", 422)
    runtime._validate_password_strength(body.password)

    token = body.verification_token.strip()
    if not token:
        raise runtime._bad(
            "invalid_verification_token",
            "verification token is invalid",
            400,
        )

    pending, now = await runtime._validated_byok_pending(
        db,
        request=request,
        email=email,
        password=body.password,
        token=token,
    )
    allow, invite, role = await runtime._byok_signup_access(
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
        password_hash=runtime.hash_password(body.password),
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
            db.add(AllowedEmail(email=email, invited_by=None))

        session, _ = await runtime._create_session(db, user, request)
        csrf, user_out = runtime._auth_response_snapshot(user, session)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        await runtime.write_audit_isolated(
            event_type="auth.signup.byok.fail",
            actor_email=email,
            actor_ip_hash=runtime.request_ip_hash(request),
            details={"reason": "integrity_conflict_masked_as_invalid_token"},
        )
        raise runtime._bad(
            "invalid_verification_token",
            runtime._BYOK_SIGNUP_VERIFICATION_FAILED_MESSAGE,
            400,
        ) from exc

    runtime._set_auth_cookies(response, session.id, csrf)
    await runtime._write_post_commit_audit_best_effort(
        event_type="auth.signup.byok.success",
        user_id=user.id,
        actor_email=email,
        actor_ip_hash=runtime.request_ip_hash(request),
        details={"role": role, "supplier_id": pending.supplier_id},
    )
    return await runtime._user_out_with_runtime_defaults(user_out, db)
