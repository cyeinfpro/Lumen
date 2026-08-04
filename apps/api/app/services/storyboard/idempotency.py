"""Durable idempotency for paid storyboard operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities.media_workflows import WorkflowRun

from ...idempotency.advisory import lock_user_key
from .common import STORYBOARD_WORKFLOW_TYPE, http_error
from .contracts import StoryboardRunOut


PAID_OPERATION_RECORDS_KEY = "storyboard_paid_operation_idempotency"
IDEMPOTENCY_OPERATION_NAMESPACE_KEY = "idempotency_operation_namespace"
IDEMPOTENCY_REQUEST_FINGERPRINT_KEY = "idempotency_request_fingerprint"
IDEMPOTENCY_CLIENT_KEY_HASH = "idempotency_client_key_hash"
IDEMPOTENCY_CHILD_IDENTITY_KEY = "idempotency_child_identity"

ASSET_GENERATE_OPERATION = "storyboard.asset.generate"
KEYFRAME_GENERATE_OPERATION = "storyboard.keyframe.generate"
KEYFRAME_GENERATE_ALL_OPERATION = "storyboard.keyframe.generate_all"
SHOT_SUBMIT_OPERATION = "storyboard.shot.submit"
SHOTS_SUBMIT_ALL_OPERATION = "storyboard.shots.submit_all"

_MAX_IDEMPOTENCY_KEY_LENGTH = 96
_LOCK_NAMESPACE = "paid-storyboard-operation"


@dataclass(frozen=True, slots=True)
class PaidStoryboardOperation:
    user_id: str
    idempotency_key: str
    client_key_hash: str
    operation_namespace: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class PaidStoryboardReplay:
    run: WorkflowRun
    response: StoryboardRunOut
    task_ids: tuple[str, ...]
    child_task_keys: dict[str, str]


def resolve_client_idempotency_key(
    header_key: str | None,
    body_key: str | None = None,
) -> str:
    if header_key is not None and body_key is not None and header_key != body_key:
        raise http_error(
            "idempotency_key_mismatch",
            "Idempotency-Key must match idempotency_key",
            422,
        )
    key = header_key if header_key is not None else body_key
    if key is None:
        raise http_error(
            "idempotency_key_required",
            "Idempotency-Key is required for paid storyboard operations",
            422,
        )
    if (
        not key
        or key != key.strip()
        or len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in key)
    ):
        raise http_error(
            "idempotency_key_invalid",
            "Idempotency-Key must be 1 to 96 printable ASCII characters",
            422,
        )
    return key


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


def paid_storyboard_operation(
    *,
    user_id: str,
    idempotency_key: str,
    operation_namespace: str,
    payload: Any,
) -> PaidStoryboardOperation:
    return PaidStoryboardOperation(
        user_id=user_id,
        idempotency_key=idempotency_key,
        client_key_hash=hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
        operation_namespace=operation_namespace,
        request_fingerprint=canonical_request_fingerprint(
            operation_namespace,
            payload,
        ),
    )


def child_task_idempotency_key(
    operation: PaidStoryboardOperation,
    child_identity: str,
) -> str:
    encoded = json.dumps(
        [
            operation.user_id,
            operation.idempotency_key,
            child_identity,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sb:{hashlib.sha256(encoded).hexdigest()}"


def child_task_metadata(
    operation: PaidStoryboardOperation,
    child_identity: str,
) -> dict[str, str]:
    return {
        IDEMPOTENCY_OPERATION_NAMESPACE_KEY: operation.operation_namespace,
        IDEMPOTENCY_REQUEST_FINGERPRINT_KEY: operation.request_fingerprint,
        IDEMPOTENCY_CLIENT_KEY_HASH: operation.client_key_hash,
        IDEMPOTENCY_CHILD_IDENTITY_KEY: child_identity,
    }


def _operation_record(
    run: WorkflowRun,
    client_key_hash: str,
) -> dict[str, Any] | None:
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    records = metadata.get(PAID_OPERATION_RECORDS_KEY)
    if not isinstance(records, dict):
        return None
    record = records.get(client_key_hash)
    return record if isinstance(record, dict) else None


def _ensure_matching_operation(
    record: dict[str, Any],
    operation: PaidStoryboardOperation,
) -> None:
    if (
        record.get("operation_namespace") != operation.operation_namespace
        or record.get("request_fingerprint") != operation.request_fingerprint
    ):
        raise http_error(
            "idempotency_conflict",
            "idempotency_key was already used for a different operation",
            409,
        )


def record_paid_operation(
    run: WorkflowRun,
    operation: PaidStoryboardOperation,
    *,
    response: StoryboardRunOut,
    task_ids: list[str],
    child_task_keys: dict[str, str],
    created_at: datetime,
) -> None:
    existing = _operation_record(run, operation.client_key_hash)
    if existing is not None:
        _ensure_matching_operation(existing, operation)
        return
    metadata = dict(run.metadata_jsonb or {})
    raw_records = metadata.get(PAID_OPERATION_RECORDS_KEY)
    records = dict(raw_records) if isinstance(raw_records, dict) else {}
    records[operation.client_key_hash] = {
        "operation_namespace": operation.operation_namespace,
        "request_fingerprint": operation.request_fingerprint,
        "task_ids": list(task_ids),
        "child_task_keys": dict(child_task_keys),
        "response": response.model_dump(mode="json"),
        "created_at": created_at.isoformat(),
    }
    metadata[PAID_OPERATION_RECORDS_KEY] = records
    run.metadata_jsonb = metadata


def _replay_from_record(
    run: WorkflowRun,
    record: dict[str, Any],
) -> PaidStoryboardReplay:
    raw_response = record.get("response")
    if not isinstance(raw_response, dict):
        raise http_error(
            "idempotency_conflict",
            "idempotency record is incomplete",
            409,
        )
    try:
        response = StoryboardRunOut.model_validate(raw_response)
    except Exception as exc:
        raise http_error(
            "idempotency_conflict",
            "idempotency record is invalid",
            409,
        ) from exc
    raw_task_ids = record.get("task_ids")
    task_ids = tuple(
        item
        for item in raw_task_ids
        if isinstance(item, str) and item
    ) if isinstance(raw_task_ids, list) else ()
    raw_child_keys = record.get("child_task_keys")
    child_task_keys = (
        {
            str(identity): key
            for identity, key in raw_child_keys.items()
            if isinstance(identity, str) and isinstance(key, str) and key
        }
        if isinstance(raw_child_keys, dict)
        else {}
    )
    return PaidStoryboardReplay(
        run=run,
        response=response,
        task_ids=task_ids,
        child_task_keys=child_task_keys,
    )


async def find_paid_operation(
    db: AsyncSession,
    operation: PaidStoryboardOperation,
) -> PaidStoryboardReplay | None:
    rows = list(
        (
            await db.execute(
                select(WorkflowRun).where(
                    WorkflowRun.user_id == operation.user_id,
                    WorkflowRun.type == STORYBOARD_WORKFLOW_TYPE,
                )
            )
        )
        .scalars()
        .all()
    )
    matches: list[PaidStoryboardReplay] = []
    for run in rows:
        record = _operation_record(run, operation.client_key_hash)
        if record is None:
            continue
        _ensure_matching_operation(record, operation)
        matches.append(_replay_from_record(run, record))
    if len(matches) > 1:
        raise http_error(
            "idempotency_conflict",
            "idempotency_key matched multiple storyboard operations",
            409,
        )
    return matches[0] if matches else None


async def lock_paid_operation(
    db: AsyncSession,
    operation: PaidStoryboardOperation,
) -> None:
    await lock_user_key(
        db,
        _LOCK_NAMESPACE,
        operation.user_id,
        operation.idempotency_key,
    )


def _is_idempotency_conflict(exc: HTTPException) -> bool:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    error = detail.get("error") if isinstance(detail, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    return exc.status_code == 409 and code in {
        "idempotency_conflict",
        "idempotency_request_mismatch",
    }


async def _recover_race(
    db: AsyncSession,
    operation: PaidStoryboardOperation,
) -> PaidStoryboardReplay | None:
    await db.rollback()
    await lock_paid_operation(db, operation)
    return await find_paid_operation(db, operation)


async def execute_paid_operation(
    db: AsyncSession,
    operation: PaidStoryboardOperation,
    action: Callable[[PaidStoryboardOperation], Awaitable[StoryboardRunOut]],
) -> StoryboardRunOut:
    await lock_paid_operation(db, operation)
    replay = await find_paid_operation(db, operation)
    if replay is not None:
        await db.commit()
        return replay.response
    try:
        return await action(operation)
    except IntegrityError as exc:
        replay = await _recover_race(db, operation)
        if replay is not None:
            await db.commit()
            return replay.response
        raise http_error(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        ) from exc
    except HTTPException as exc:
        if not _is_idempotency_conflict(exc):
            raise
        replay = await _recover_race(db, operation)
        if replay is not None:
            await db.commit()
            return replay.response
        raise


__all__ = [
    "ASSET_GENERATE_OPERATION",
    "IDEMPOTENCY_CHILD_IDENTITY_KEY",
    "IDEMPOTENCY_CLIENT_KEY_HASH",
    "IDEMPOTENCY_OPERATION_NAMESPACE_KEY",
    "IDEMPOTENCY_REQUEST_FINGERPRINT_KEY",
    "KEYFRAME_GENERATE_ALL_OPERATION",
    "KEYFRAME_GENERATE_OPERATION",
    "PAID_OPERATION_RECORDS_KEY",
    "SHOT_SUBMIT_OPERATION",
    "SHOTS_SUBMIT_ALL_OPERATION",
    "PaidStoryboardOperation",
    "PaidStoryboardReplay",
    "canonical_request_fingerprint",
    "child_task_idempotency_key",
    "child_task_metadata",
    "execute_paid_operation",
    "find_paid_operation",
    "lock_paid_operation",
    "paid_storyboard_operation",
    "record_paid_operation",
    "resolve_client_idempotency_key",
]
