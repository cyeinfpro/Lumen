"""Billing-safe non-streaming prompt enhancement for Telegram."""

from __future__ import annotations

import json
import logging
from contextlib import aclosing
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import TelegramBinding, User

from ..billing_cache_state import invalidate_balance_cache
from ..redis_client import get_redis
from ..services.active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    lock_active_user,
)
from .prompts import (
    PROMPTS_ENHANCE_LIMITER,
    PromptRuntime,
    durable_prompt_enhance_stream,
    prepare_reserved_prompt_billing,
    resolve_provider_order,
)
from .prompt_parts import idempotency as _shared_idempotency
from . import telegram_prompt_idempotency as _telegram_prompt_idempotency


logger = logging.getLogger(__name__)


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


async def _lock_telegram_prompt_enhance_user(
    db: AsyncSession,
    *,
    authenticated_user_id: str,
    chat_id: str,
    tg_user_id: str,
) -> User:
    """Refresh Telegram binding and account mode under durable write locks."""

    try:
        await lock_active_user(db, authenticated_user_id)
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc

    row = (
        await db.execute(
            select(TelegramBinding, User)
            .join(User, User.id == TelegramBinding.user_id)
            .where(
                TelegramBinding.chat_id == chat_id,
                User.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
            .with_for_update(of=TelegramBinding)
        )
    ).first()
    if row is None:
        raise _http(
            "telegram_binding_revoked",
            "telegram binding was removed while prompt enhancement was starting",
            403,
        )

    binding, locked_user = row
    if (
        binding.user_id != authenticated_user_id
        or (binding.tg_user_id or "").strip() != tg_user_id
    ):
        raise _http(
            "telegram_binding_changed",
            "telegram binding changed while prompt enhancement was starting",
            403,
        )
    return locked_user


def _prompt_enhance_operation(
    *,
    text: str,
    user_id: str,
    chat_id: str,
    tg_user_id: str,
    idempotency_key: str | None,
) -> _telegram_prompt_idempotency.TelegramPromptEnhanceOperation:
    client_key = _telegram_prompt_idempotency.resolve_client_idempotency_key(
        idempotency_key
    )
    return _telegram_prompt_idempotency.telegram_prompt_enhance_operation(
        user_id=user_id,
        idempotency_key=client_key,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        text=text,
    )


async def _replay_enhancement(
    db: AsyncSession,
    *,
    replay_enhanced: str,
    user_id: str,
    chat_id: str,
    tg_user_id: str,
) -> str:
    await _lock_telegram_prompt_enhance_user(
        db,
        authenticated_user_id=user_id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
    )
    await db.commit()
    return replay_enhanced


async def _prepare_paid_enhancement(
    db: AsyncSession,
    *,
    operation: _telegram_prompt_idempotency.TelegramPromptEnhanceOperation,
    reservation: _telegram_prompt_idempotency.TelegramPromptEnhanceReservation,
    user_id: str,
    chat_id: str,
    tg_user_id: str,
    runtime: PromptRuntime,
) -> tuple[
    list[Any],
    Any,
    _shared_idempotency.PromptEnhanceReservation,
]:
    if reservation.recovery is None:
        await PROMPTS_ENHANCE_LIMITER.check(
            get_redis(),
            f"rl:prompt_enhance:{user_id}",
        )
        providers = [
            provider
            for provider in await resolve_provider_order(db, runtime)
            if provider.api_key.strip()
        ]
        if not providers:
            raise _http("not_configured", "upstream API key not set", 503)
    else:
        providers = []
    locked_user = await _lock_telegram_prompt_enhance_user(
        db,
        authenticated_user_id=user_id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
    )
    shared_reservation = _shared_idempotency.PromptEnhanceReservation(
        attempt=reservation.attempt,
        billing_snapshot=reservation.billing_snapshot,
        recovery=reservation.recovery,
    )
    billing, invalidate_hold = await prepare_reserved_prompt_billing(
        db,
        locked_user,
        operation,
        shared_reservation,
        runtime=runtime,
    )
    await db.commit()
    if invalidate_hold:
        await invalidate_balance_cache(user_id)
    return providers, billing, shared_reservation


def _decode_enhance_chunk(chunk: str) -> tuple[str, str] | None:
    if not chunk.startswith("data: "):
        return None
    payload = chunk[6:].strip()
    if payload == "[DONE]":
        return "done", ""
    if not payload:
        return None
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    text = event.get("text")
    if isinstance(text, str):
        return "text", text
    if "error" in event:
        return "error", str(event["error"])
    return None


def _validated_enhancement(
    parts: list[str],
    *,
    completed: bool,
    error: str | None,
) -> str:
    if error is not None:
        raise _http("enhance_failed", error, 502)
    if not completed:
        raise _http(
            "enhance_failed",
            "prompt enhancement stream ended before completion",
            502,
        )
    enhanced = "".join(parts).strip()
    if not enhanced:
        raise _http("enhance_failed", "no enhanced text returned", 502)
    return enhanced


async def _collect_enhanced_text(
    *,
    operation: _telegram_prompt_idempotency.TelegramPromptEnhanceOperation,
    reservation: _shared_idempotency.PromptEnhanceReservation,
    text: str,
    providers: list[Any],
    billing: Any,
    runtime: PromptRuntime,
) -> str:
    parts: list[str] = []
    error: str | None = None
    completed = False
    stream, task = durable_prompt_enhance_stream(
        operation,
        reservation,
        text=text,
        providers=providers,
        billing=billing,
        runtime=runtime,
    )
    try:
        async with aclosing(stream):
            async for chunk in stream:
                decoded = _decode_enhance_chunk(chunk)
                if decoded is None:
                    continue
                kind, value = decoded
                if kind == "text":
                    parts.append(value)
                elif kind == "error":
                    error = value
                    break
                else:
                    completed = True
                    break
    finally:
        await task
    return _validated_enhancement(parts, completed=completed, error=error)


async def _execute_new_enhancement(
    db: AsyncSession,
    *,
    operation: _telegram_prompt_idempotency.TelegramPromptEnhanceOperation,
    reservation: _telegram_prompt_idempotency.TelegramPromptEnhanceReservation,
    text: str,
    user_id: str,
    chat_id: str,
    tg_user_id: str,
    runtime: PromptRuntime,
) -> str:
    providers, billing, shared_reservation = await _prepare_paid_enhancement(
        db,
        operation=operation,
        reservation=reservation,
        user_id=user_id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        runtime=runtime,
    )
    return await _collect_enhanced_text(
        operation=operation,
        reservation=shared_reservation,
        text=text,
        providers=providers,
        billing=billing,
        runtime=runtime,
    )


async def enhance_telegram_prompt(
    *,
    text: str,
    user: Any,
    chat_id: str,
    tg_user_id: str,
    idempotency_key: str | None,
    db: AsyncSession,
    runtime: PromptRuntime,
) -> str:
    operation = _prompt_enhance_operation(
        text=text,
        user_id=user.id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        idempotency_key=idempotency_key,
    )
    reservation = await _telegram_prompt_idempotency.reserve_telegram_prompt_enhance(
        db,
        operation,
    )
    if reservation.replay_enhanced is not None:
        return await _replay_enhancement(
            db,
            replay_enhanced=reservation.replay_enhanced,
            user_id=user.id,
            chat_id=chat_id,
            tg_user_id=tg_user_id,
        )
    return await _execute_new_enhancement(
        db,
        operation=operation,
        reservation=reservation,
        text=text,
        user_id=user.id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        runtime=runtime,
    )
