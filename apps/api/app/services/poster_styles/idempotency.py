"""Durable idempotency for paid poster-style generation."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.media_workflows import WorkflowRun
from lumen_core.schema_models.posters import PosterStyleGenerateOut

from ...idempotency.advisory import lock_user_key


POSTER_STYLE_GENERATE_OPERATION = "poster_style.generate"

_LOCK_NAMESPACE = "paid-poster-style-operation"
_RECORD_METADATA_KEY = "paid_poster_style_idempotency"
_MAX_IDEMPOTENCY_KEY_LENGTH = 96
_RECORD_ID_NAMESPACE = uuid.UUID("5adb30b8-27bd-4a26-845b-3cd7e91ea913")


@dataclass(frozen=True, slots=True)
class PosterStyleOperation:
    user_id: str
    idempotency_key: str
    client_key_hash: str
    operation_namespace: str
    request_fingerprint: str
    record_id: str


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def resolve_client_idempotency_key(raw_key: str | None) -> str:
    if raw_key is None:
        raise _http(
            "idempotency_key_required",
            "Idempotency-Key is required for paid poster-style generation",
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
    operation_namespace: str,
    payload: Any,
) -> str:
    encoded = json.dumps(
        {
            "operation_namespace": operation_namespace,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_record_id(user_id: str, idempotency_key: str) -> str:
    return str(
        uuid.uuid5(
            _RECORD_ID_NAMESPACE,
            f"{user_id}\0{idempotency_key}",
        )
    )


def poster_style_operation(
    *,
    user_id: str,
    idempotency_key: str,
    payload: Any,
) -> PosterStyleOperation:
    return PosterStyleOperation(
        user_id=user_id,
        idempotency_key=idempotency_key,
        client_key_hash=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
        operation_namespace=POSTER_STYLE_GENERATE_OPERATION,
        request_fingerprint=canonical_request_fingerprint(
            POSTER_STYLE_GENERATE_OPERATION,
            payload,
        ),
        record_id=_operation_record_id(user_id, idempotency_key),
    )


def operation_metadata(operation: PosterStyleOperation) -> dict[str, Any]:
    return {
        _RECORD_METADATA_KEY: {
            "client_key_hash": operation.client_key_hash,
            "operation_namespace": operation.operation_namespace,
            "request_fingerprint": operation.request_fingerprint,
            "state": "running",
        }
    }


def _record(run: WorkflowRun) -> dict[str, Any] | None:
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    record = metadata.get(_RECORD_METADATA_KEY)
    return record if isinstance(record, dict) else None


def _ensure_matching_operation(
    run: WorkflowRun,
    operation: PosterStyleOperation,
) -> dict[str, Any]:
    record = _record(run)
    if (
        run.user_id != operation.user_id
        or record is None
        or record.get("client_key_hash") != operation.client_key_hash
        or record.get("operation_namespace") != operation.operation_namespace
        or record.get("request_fingerprint") != operation.request_fingerprint
    ):
        raise _http(
            "idempotency_conflict",
            "idempotency_key was already used for a different operation",
            409,
        )
    return record


def record_response(
    run: WorkflowRun,
    operation: PosterStyleOperation,
    response: PosterStyleGenerateOut,
) -> None:
    record = _ensure_matching_operation(run, operation)
    metadata = dict(run.metadata_jsonb or {})
    metadata[_RECORD_METADATA_KEY] = {
        **record,
        "state": "completed",
        "response": response.model_dump(mode="json"),
    }
    run.metadata_jsonb = metadata


async def find_replay(
    db: AsyncSession,
    operation: PosterStyleOperation,
) -> PosterStyleGenerateOut | None:
    run = await db.get(WorkflowRun, operation.record_id)
    if run is None:
        return None
    record = _ensure_matching_operation(run, operation)
    raw_response = record.get("response")
    if record.get("state") != "completed" or not isinstance(raw_response, dict):
        raise _http(
            "idempotency_in_progress",
            "the original poster-style generation is still running",
            425,
        )
    try:
        return PosterStyleGenerateOut.model_validate(raw_response)
    except Exception as exc:
        raise _http(
            "idempotency_replay_unavailable",
            "idempotency response is invalid",
            409,
        ) from exc


async def execute_paid_operation(
    db: AsyncSession,
    operation: PosterStyleOperation,
    action: Callable[[PosterStyleOperation], Awaitable[PosterStyleGenerateOut]],
) -> PosterStyleGenerateOut:
    await lock_user_key(
        db,
        _LOCK_NAMESPACE,
        operation.user_id,
        operation.idempotency_key,
    )
    replay = await find_replay(db, operation)
    if replay is not None:
        await db.commit()
        return replay
    try:
        return await action(operation)
    except IntegrityError as exc:
        await db.rollback()
        await lock_user_key(
            db,
            _LOCK_NAMESPACE,
            operation.user_id,
            operation.idempotency_key,
        )
        replay = await find_replay(db, operation)
        if replay is not None:
            await db.commit()
            return replay
        raise _http(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        ) from exc


__all__ = [
    "POSTER_STYLE_GENERATE_OPERATION",
    "PosterStyleOperation",
    "canonical_request_fingerprint",
    "execute_paid_operation",
    "find_replay",
    "operation_metadata",
    "poster_style_operation",
    "record_response",
    "resolve_client_idempotency_key",
]
