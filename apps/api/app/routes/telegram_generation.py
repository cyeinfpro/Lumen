"""Telegram generation ownership and durable operation identity."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any
import uuid

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import AuditLog, Conversation, TelegramBinding, User

from ..idempotency.advisory import lock_user_key
from ..services.active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    lock_active_user,
)

_TG_CONV_TITLE = "Telegram Bot"
_TG_CONV_MARKER = MappingProxyType({"telegram": True})
_OPERATION_NAMESPACE = "telegram.generation.create"
_OPERATION_EVENT_TYPE = "telegram.generation.operation"
_OPERATION_RECORD_NAMESPACE = uuid.UUID("1dd2e77a-7053-48b7-a5d2-fd30de1fe790")
_OPERATION_LOCK_NAMESPACE = "telegram-generation-operation"
_OPERATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TelegramGenerationContext:
    user: User
    conversation: Conversation
    message_idempotency_key: str | None = None
    operation_id: str | None = None
    request_fingerprint: str | None = None
    replay: bool = False


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def canonical_generation_request_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {
            "operation_namespace": _OPERATION_NAMESPACE,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_identity(chat_id: str, tg_user_id: str, client_key: str) -> str:
    return "\0".join((chat_id, tg_user_id, client_key))


def telegram_generation_operation_id(
    chat_id: str,
    tg_user_id: str,
    client_key: str,
) -> str:
    return str(
        uuid.uuid5(
            _OPERATION_RECORD_NAMESPACE,
            _operation_identity(chat_id, tg_user_id, client_key),
        )
    )


def telegram_generation_message_key(
    chat_id: str,
    tg_user_id: str,
    client_key: str,
) -> str:
    digest = hashlib.sha256(
        _operation_identity(chat_id, tg_user_id, client_key).encode("utf-8")
    ).hexdigest()
    return f"tg:{digest[:61]}"


def _identity_hash(chat_id: str, tg_user_id: str) -> str:
    return hashlib.sha256(f"{chat_id}\0{tg_user_id}".encode("utf-8")).hexdigest()


def _client_key_hash(client_key: str) -> str:
    return hashlib.sha256(client_key.encode("utf-8")).hexdigest()


async def _get_or_create_tg_conversation_locked(
    db: AsyncSession,
    user_id: str,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                Conversation.default_params.contains({"telegram": True}),
            )
            .order_by(desc(Conversation.last_activity_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if conv is not None:
        return conv
    conv = Conversation(
        user_id=user_id,
        title=_TG_CONV_TITLE,
        default_params=dict(_TG_CONV_MARKER),
        archived=True,
    )
    db.add(conv)
    await db.flush()
    return conv


async def get_or_create_tg_conversation(
    db: AsyncSession,
    user_id: str,
) -> Conversation:
    """Serialize the first Telegram conversation on the durable user row."""
    try:
        await lock_active_user(db, user_id)
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    conv = await _get_or_create_tg_conversation_locked(db, user_id)
    await db.commit()
    await db.refresh(conv)
    return conv


async def _lock_telegram_binding_user(
    db: AsyncSession,
    *,
    authenticated_user_id: str,
    chat_id: str,
    tg_user_id: str,
) -> User:
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
            "telegram binding was removed while generation was starting",
            403,
        )

    binding, locked_user = row
    if (
        binding.user_id != authenticated_user_id
        or (binding.tg_user_id or "").strip() != tg_user_id
    ):
        raise _http(
            "telegram_binding_changed",
            "telegram binding changed while generation was starting",
            403,
        )
    return locked_user


def _operation_details(
    *,
    conversation_id: str,
    chat_id: str,
    tg_user_id: str,
    client_key: str,
    message_idempotency_key: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    return {
        "schema_version": _OPERATION_SCHEMA_VERSION,
        "operation_namespace": _OPERATION_NAMESPACE,
        "telegram_identity_hash": _identity_hash(chat_id, tg_user_id),
        "client_key_hash": _client_key_hash(client_key),
        "request_fingerprint": request_fingerprint,
        "conversation_id": conversation_id,
        "message_idempotency_key": message_idempotency_key,
    }


def _stored_operation_details(record: AuditLog) -> dict[str, Any]:
    details = record.details if isinstance(record.details, dict) else {}
    if (
        record.event_type != _OPERATION_EVENT_TYPE
        or details.get("schema_version") != _OPERATION_SCHEMA_VERSION
        or details.get("operation_namespace") != _OPERATION_NAMESPACE
    ):
        raise _http(
            "telegram_generation_operation_conflict",
            "telegram generation operation identity is unavailable",
            409,
        )
    return details


async def _existing_operation_context(
    db: AsyncSession,
    *,
    record: AuditLog,
    locked_user: User,
    expected_details: dict[str, Any],
    operation_id: str,
) -> TelegramGenerationContext:
    if record.user_id != locked_user.id:
        raise _http(
            "telegram_generation_rebind_conflict",
            "telegram generation request belongs to a different account binding",
            409,
        )

    details = _stored_operation_details(record)
    for field in (
        "telegram_identity_hash",
        "client_key_hash",
        "request_fingerprint",
        "message_idempotency_key",
    ):
        if details.get(field) != expected_details[field]:
            raise _http(
                "idempotency_conflict",
                "idempotency_key was already used for a different operation",
                409,
            )

    conversation_id = details.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise _http(
            "telegram_generation_replay_unavailable",
            "telegram generation replay is unavailable",
            409,
        )
    conversation = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.user_id == locked_user.id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise _http(
            "telegram_generation_replay_unavailable",
            "telegram generation replay is unavailable",
            409,
        )
    return TelegramGenerationContext(
        user=locked_user,
        conversation=conversation,
        message_idempotency_key=expected_details["message_idempotency_key"],
        operation_id=operation_id,
        request_fingerprint=expected_details["request_fingerprint"],
        replay=True,
    )


async def lock_telegram_generation_context(
    db: AsyncSession,
    *,
    authenticated_user_id: str,
    chat_id: str,
    tg_user_id: str,
    client_key: str | None = None,
    request_payload: dict[str, Any] | None = None,
) -> TelegramGenerationContext:
    """Revalidate identity and reserve a durable Telegram operation when requested."""

    if (client_key is None) != (request_payload is None):
        raise ValueError("client_key and request_payload must be provided together")
    if client_key is not None:
        await lock_user_key(
            db,
            _OPERATION_LOCK_NAMESPACE,
            _identity_hash(chat_id, tg_user_id),
            client_key,
        )

    locked_user = await _lock_telegram_binding_user(
        db,
        authenticated_user_id=authenticated_user_id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
    )
    if client_key is None or request_payload is None:
        conversation = await _get_or_create_tg_conversation_locked(
            db,
            authenticated_user_id,
        )
        return TelegramGenerationContext(
            user=locked_user,
            conversation=conversation,
        )

    operation_id = telegram_generation_operation_id(
        chat_id,
        tg_user_id,
        client_key,
    )
    message_idempotency_key = telegram_generation_message_key(
        chat_id,
        tg_user_id,
        client_key,
    )
    request_fingerprint = canonical_generation_request_fingerprint(request_payload)
    record = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.id == operation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    ).scalar_one_or_none()
    expected_details = _operation_details(
        conversation_id="",
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        client_key=client_key,
        message_idempotency_key=message_idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    if record is not None:
        return await _existing_operation_context(
            db,
            record=record,
            locked_user=locked_user,
            expected_details=expected_details,
            operation_id=operation_id,
        )

    conversation = await _get_or_create_tg_conversation_locked(
        db,
        authenticated_user_id,
    )
    details = _operation_details(
        conversation_id=conversation.id,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        client_key=client_key,
        message_idempotency_key=message_idempotency_key,
        request_fingerprint=request_fingerprint,
    )
    db.add(
        AuditLog(
            id=operation_id,
            user_id=locked_user.id,
            event_type=_OPERATION_EVENT_TYPE,
            target_user_id=locked_user.id,
            details=details,
        )
    )
    await db.flush()
    return TelegramGenerationContext(
        user=locked_user,
        conversation=conversation,
        message_idempotency_key=message_idempotency_key,
        operation_id=operation_id,
        request_fingerprint=request_fingerprint,
    )
