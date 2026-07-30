"""Password reset contracts and behavior for the authentication facade."""

from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import AuthSession, User

from .runtime import AuthRuntimeAdapter


PASSWORD_RESET_TTL_SECONDS = 15 * 60
PASSWORD_RESET_KEY_PREFIX = "pwd_reset"
CLAIM_PASSWORD_RESET_TOKEN_LUA = """
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
RESTORE_PASSWORD_RESET_TOKEN_LUA = """
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
CONSUME_PASSWORD_RESET_CLAIM_LUA = """
if redis.call('HGET', KEYS[1], 'owner') == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(max_length=128)


class OkOut(BaseModel):
    ok: bool


def password_reset_key(runtime: AuthRuntimeAdapter, token: str) -> str:
    return (
        f"{runtime._PASSWORD_RESET_KEY_PREFIX}:"
        f"{runtime.hash_token(token)}"
    )


def password_reset_claim_key(runtime: AuthRuntimeAdapter, token: str) -> str:
    return (
        f"{runtime._PASSWORD_RESET_KEY_PREFIX}:claim:"
        f"{runtime.hash_token(token)}"
    )


def password_reset_url(token: str, public_base_url: str) -> str:
    return f"{public_base_url.rstrip('/')}/reset-password/{token}"


def redis_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


async def claim_password_reset_token(
    runtime: AuthRuntimeAdapter,
    redis: Any,
    token_key: str,
    claim_key: str,
    *,
    owner: str,
) -> str | None:
    result = await redis.eval(
        runtime._CLAIM_PASSWORD_RESET_TOKEN_LUA,
        2,
        token_key,
        claim_key,
        owner,
    )
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        raise RuntimeError("unexpected password reset claim response")
    if int(result[0]) != 1:
        return None
    user_id = redis_text(result[1])
    if not user_id:
        raise RuntimeError("password reset claim omitted user id")
    return user_id


async def restore_password_reset_token(
    runtime: AuthRuntimeAdapter,
    redis: Any,
    token_key: str,
    claim_key: str,
    *,
    owner: str,
) -> bool:
    result = await redis.eval(
        runtime._RESTORE_PASSWORD_RESET_TOKEN_LUA,
        2,
        token_key,
        claim_key,
        owner,
    )
    return int(result) == 1


async def consume_password_reset_claim(
    runtime: AuthRuntimeAdapter,
    redis: Any,
    claim_key: str,
    *,
    owner: str,
) -> None:
    try:
        await redis.eval(
            runtime._CONSUME_PASSWORD_RESET_CLAIM_LUA,
            1,
            claim_key,
            owner,
        )
    except Exception:
        runtime.logger.warning(
            "password_reset_claim_consume_failed",
            exc_info=True,
        )


async def password_reset_request(
    runtime: AuthRuntimeAdapter,
    *,
    body: PasswordResetRequestIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> OkOut:
    email = body.email.strip().lower()
    redis = runtime.get_redis()
    await runtime._PASSWORD_RESET_REQUEST_IP_LIMITER.check(
        redis,
        f"rl:pwd_reset_request:ip:{runtime.require_client_ip(request)}",
    )
    await runtime._PASSWORD_RESET_REQUEST_EMAIL_LIMITER.check(
        redis,
        f"rl:pwd_reset_request:email:{runtime._log_hash(email) or 'unknown'}",
    )
    user = (
        await db.execute(
            select(User).where(User.email == email, User.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    try:
        public_base_url = await runtime.resolve_public_base_url(request, db)
    except Exception:
        runtime.logger.exception(
            "password_reset_public_base_url_failed",
            extra={"email_hash": runtime._log_hash(email)},
        )
        return OkOut(ok=True)
    if user is None:
        return OkOut(ok=True)

    token = runtime.secrets.token_urlsafe(32)
    key = runtime._password_reset_key(token)
    reset_url = runtime._password_reset_url(token, public_base_url)
    try:
        await redis.set(
            key,
            user.id,
            ex=runtime._PASSWORD_RESET_TTL_SECONDS,
        )
    except Exception:
        runtime.logger.exception(
            "password_reset_token_store_failed",
            extra={"email_hash": runtime._log_hash(email), "user_id": user.id},
        )
        return OkOut(ok=True)
    background_tasks.add_task(
        runtime._send_password_reset_email_or_delete,
        redis,
        key,
        email,
        user.id,
        reset_url,
    )
    return OkOut(ok=True)


async def password_reset_confirm(
    runtime: AuthRuntimeAdapter,
    *,
    body: PasswordResetConfirmIn,
    request: Request,
    db: AsyncSession,
) -> OkOut:
    token = body.token.strip()
    if not token:
        raise runtime._bad(
            "invalid_token",
            "reset token is invalid or expired",
            400,
        )
    runtime._validate_password_strength(body.new_password)

    redis = runtime.get_redis()
    await runtime._PASSWORD_RESET_CONFIRM_IP_LIMITER.check(
        redis,
        f"rl:pwd_reset_confirm:ip:{runtime.require_client_ip(request)}",
    )
    key = runtime._password_reset_key(token)
    await runtime._PASSWORD_RESET_CONFIRM_TOKEN_LIMITER.check(
        redis,
        f"rl:pwd_reset_confirm:token:{runtime.hash_token(token)}",
    )
    claim_key = runtime._password_reset_claim_key(token)
    claim_owner = runtime.secrets.token_urlsafe(24)
    try:
        raw_user_id = await runtime._claim_password_reset_token(
            redis,
            key,
            claim_key,
            owner=claim_owner,
        )
    except Exception as exc:
        runtime.logger.error("password_reset_token_claim_failed", exc_info=True)
        raise runtime._bad(
            "reset_unavailable",
            "password reset is temporarily unavailable",
            503,
        ) from exc
    if not raw_user_id:
        raise runtime._bad(
            "invalid_token",
            "reset token is invalid or expired",
            400,
        )

    try:
        await runtime._PASSWORD_RESET_CONFIRM_USER_LIMITER.check(
            redis,
            f"rl:pwd_reset_confirm:user:{raw_user_id}",
        )
        user = (
            await db.execute(
                select(User).where(User.id == raw_user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if user is None or user.deleted_at is not None:
            raise runtime._bad(
                "invalid_token",
                "reset token is invalid or expired",
                400,
            )

        now = runtime.datetime.now(runtime.timezone.utc)
        user.password_hash = runtime.hash_password(body.new_password)
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
            runtime.logger.error(
                "password_reset_db_rollback_failed",
                exc_info=True,
            )

        if rollback_succeeded:
            try:
                restored = await runtime._restore_password_reset_token(
                    redis,
                    key,
                    claim_key,
                    owner=claim_owner,
                )
            except Exception:
                runtime.logger.error(
                    "password_reset_token_restore_failed",
                    exc_info=True,
                )
            else:
                if not restored:
                    runtime.logger.error("password_reset_token_restore_rejected")
        raise

    try:
        await db.commit()
    except Exception as exc:
        try:
            await db.rollback()
        except Exception:
            runtime.logger.error(
                "password_reset_db_rollback_failed",
                exc_info=True,
            )
        await runtime._consume_password_reset_claim(
            redis,
            claim_key,
            owner=claim_owner,
        )
        runtime.logger.error(
            "password_reset_commit_outcome_uncertain",
            extra={"user_id": raw_user_id},
            exc_info=True,
        )
        raise runtime._bad(
            "reset_outcome_uncertain",
            "password reset result is uncertain; request a new reset link before retrying",
            503,
        ) from exc

    await runtime._consume_password_reset_claim(
        redis,
        claim_key,
        owner=claim_owner,
    )
    return OkOut(ok=True)


async def delete_password_reset_token(
    runtime: AuthRuntimeAdapter,
    redis: Any,
    key: str,
) -> None:
    try:
        await redis.delete(key)
    except Exception:
        runtime.logger.error("password_reset_token_delete_failed", exc_info=True)


async def send_password_reset_email_or_delete(
    runtime: AuthRuntimeAdapter,
    redis: Any,
    key: str,
    email: str,
    user_id: str,
    reset_url: str,
) -> None:
    try:
        await runtime.send_password_reset_email(
            to_email=email,
            reset_url=reset_url,
            expires_minutes=runtime._PASSWORD_RESET_TTL_SECONDS // 60,
        )
    except runtime.EmailDeliveryError:
        await runtime._delete_password_reset_token(redis, key)
        runtime.logger.error(
            "password_reset_email_delivery_failed",
            extra={"email_hash": runtime._log_hash(email), "user_id": user_id},
            exc_info=True,
        )
    except Exception:
        await runtime._delete_password_reset_token(redis, key)
        runtime.logger.exception(
            "password_reset_email_unexpected_failed",
            extra={"email_hash": runtime._log_hash(email), "user_id": user_id},
        )
