"""Durable idempotency adapter for paid Telegram prompt enhancement."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .prompt_parts import idempotency as _shared


TELEGRAM_PROMPT_ENHANCE_OPERATION = "telegram.prompt_enhancement"

_LOCK_NAMESPACE = "paid-telegram-prompt-enhancement"
_LOCK_SUBJECT = "telegram"
_RECORD_TYPE = "telegram_prompt_enhancement_operation"
_RECORD_METADATA_KEY = "telegram_prompt_enhancement_idempotency"
_MAX_IDEMPOTENCY_KEY_LENGTH = 96
_RECORD_ID_NAMESPACE = uuid.UUID("79f7dd5f-21a6-4a56-97c4-a9d5fceab8d7")
_TERMINAL_PERSIST_ATTEMPTS = 3

TelegramPromptEnhanceOperation = _shared.PromptEnhanceOperation


@dataclass(frozen=True, slots=True)
class TelegramPromptEnhanceReservation:
    replay_enhanced: str | None = None
    attempt: _shared.PromptEnhanceAttempt | None = None
    billing_snapshot: dict[str, Any] | None = None
    recovery: _shared.PromptEnhanceRecovery | None = None


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def resolve_client_idempotency_key(raw_key: str | None) -> str:
    if raw_key is None:
        raise _http(
            "idempotency_key_required",
            "Idempotency-Key is required for paid Telegram prompt enhancement",
            422,
        )
    if (
        not raw_key
        or raw_key != raw_key.strip()
        or len(raw_key) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in raw_key)
    ):
        raise _http(
            "idempotency_key_invalid",
            "Idempotency-Key must be 1 to 96 printable ASCII characters",
            422,
        )
    return raw_key


def canonical_request_fingerprint(
    *,
    user_id: str,
    chat_id: str,
    tg_user_id: str,
    text: str,
) -> str:
    encoded = json.dumps(
        {
            "operation_namespace": TELEGRAM_PROMPT_ENHANCE_OPERATION,
            "payload": {
                "user_id": user_id,
                "telegram_identity": {
                    "chat_id": chat_id,
                    "tg_user_id": tg_user_id,
                },
                "text": text,
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_record_id(idempotency_key: str) -> str:
    return str(uuid.uuid5(_RECORD_ID_NAMESPACE, idempotency_key))


def telegram_prompt_enhance_operation(
    *,
    user_id: str,
    idempotency_key: str,
    chat_id: str,
    tg_user_id: str,
    text: str,
) -> TelegramPromptEnhanceOperation:
    return TelegramPromptEnhanceOperation(
        user_id=user_id,
        idempotency_key=idempotency_key,
        client_key_hash=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
        operation_namespace=TELEGRAM_PROMPT_ENHANCE_OPERATION,
        request_fingerprint=canonical_request_fingerprint(
            user_id=user_id,
            chat_id=chat_id,
            tg_user_id=tg_user_id,
            text=text,
        ),
        record_id=_operation_record_id(idempotency_key),
        record_type=_RECORD_TYPE,
        metadata_key=_RECORD_METADATA_KEY,
        lock_namespace=_LOCK_NAMESPACE,
        lock_subject=_LOCK_SUBJECT,
    )


def _enhanced_from_chunks(chunks: tuple[str, ...]) -> str:
    terminal = _shared.terminal_chunk_kind(chunks[-1]) if chunks else None
    if terminal == "failed":
        raise _http(
            "idempotency_terminal_failed",
            "the original Telegram prompt enhancement failed; start a new flow",
            409,
        )
    enhanced = "".join(
        text
        for chunk in chunks
        if isinstance(text := _shared.text_delta_from_chunk(chunk), str)
    ).strip()
    if terminal != "succeeded" or not enhanced:
        raise _http(
            "idempotency_replay_unavailable",
            "the stored Telegram prompt enhancement response is invalid",
            409,
        )
    return enhanced


async def reserve_telegram_prompt_enhance(
    db: AsyncSession,
    operation: TelegramPromptEnhanceOperation,
) -> TelegramPromptEnhanceReservation:
    reservation = await _shared.reserve_prompt_enhance_operation(db, operation)
    if reservation.replay_chunks is not None:
        return TelegramPromptEnhanceReservation(
            replay_enhanced=_enhanced_from_chunks(reservation.replay_chunks)
        )
    return TelegramPromptEnhanceReservation(
        attempt=reservation.attempt,
        billing_snapshot=reservation.billing_snapshot,
        recovery=reservation.recovery,
    )


async def _persist_terminal_with_retry(
    db: AsyncSession,
    operation: TelegramPromptEnhanceOperation,
    *,
    chunks: list[str],
    terminal_state: str,
    attempt: _shared.PromptEnhanceAttempt | None,
) -> None:
    last_error: Exception | None = None
    for retry in range(_TERMINAL_PERSIST_ATTEMPTS):
        try:
            await _shared.persist_terminal_response(
                db,
                operation,
                chunks=chunks,
                terminal_state=terminal_state,
                attempt=attempt,
            )
            return
        except _shared.AttemptOwnershipLost:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await db.rollback()
            if retry < _TERMINAL_PERSIST_ATTEMPTS - 1:
                await asyncio.sleep(0.05 * (2**retry))
    assert last_error is not None
    raise last_error


async def persist_terminal_success(
    db: AsyncSession,
    operation: TelegramPromptEnhanceOperation,
    enhanced: str,
    *,
    attempt: _shared.PromptEnhanceAttempt | None = None,
) -> None:
    if not enhanced.strip():
        raise ValueError("successful Telegram prompt enhancement must have text")
    await _persist_terminal_with_retry(
        db,
        operation,
        chunks=[
            f"data: {json.dumps({'text': enhanced})}\n\n",
            "data: [DONE]\n\n",
        ],
        terminal_state="succeeded",
        attempt=attempt,
    )


async def persist_terminal_failure(
    db: AsyncSession,
    operation: TelegramPromptEnhanceOperation,
    *,
    error_code: str,
    error_message: str,
    error_status: int,
    attempt: _shared.PromptEnhanceAttempt | None = None,
) -> None:
    await _persist_terminal_with_retry(
        db,
        operation,
        chunks=[
            "data: "
            + json.dumps(
                {
                    "error": error_code,
                    "message": error_message,
                    "status": int(error_status),
                }
            )
            + "\n\n"
        ],
        terminal_state="failed",
        attempt=attempt,
    )


__all__ = [
    "TELEGRAM_PROMPT_ENHANCE_OPERATION",
    "TelegramPromptEnhanceOperation",
    "TelegramPromptEnhanceReservation",
    "canonical_request_fingerprint",
    "persist_terminal_failure",
    "persist_terminal_success",
    "reserve_telegram_prompt_enhance",
    "resolve_client_idempotency_key",
    "telegram_prompt_enhance_operation",
]
