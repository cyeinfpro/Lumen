"""Durable idempotency contracts for paid workflow operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import WorkflowRequestError


PAID_OPERATION_RECORDS_KEY = "paid_operation_idempotency"
IDEMPOTENCY_OPERATION_NAMESPACE_KEY = "idempotency_operation_namespace"
IDEMPOTENCY_REQUEST_FINGERPRINT_KEY = "idempotency_request_fingerprint"
IDEMPOTENCY_CLIENT_KEY_HASH = "idempotency_client_key_hash"

APPAREL_CREATE_OPERATION = "workflow.apparel_showcase.create"
APPAREL_MODEL_CANDIDATES_OPERATION = "workflow.apparel_showcase.model_candidates.create"
APPAREL_ACCESSORY_PREVIEWS_OPERATION = (
    "workflow.apparel_showcase.accessory_previews.create"
)
APPAREL_SHOWCASE_IMAGES_OPERATION = "workflow.apparel_showcase.images.create"
APPAREL_REVISE_IMAGE_OPERATION = "workflow.apparel_showcase.image.revise"
POSTER_CREATE_OPERATION = "workflow.poster_design.create"
POSTER_MASTERS_OPERATION = "workflow.poster_design.masters.create"
POSTER_RENDERS_OPERATION = "workflow.poster_design.renders.create"
POSTER_REVISE_RENDER_OPERATION = "workflow.poster_design.render.revise"
POSTER_INPAINT_RENDER_OPERATION = "workflow.poster_design.render.inpaint"
MODEL_LIBRARY_GENERATE_OPERATION = "workflow.model_library.generate"

_MAX_IDEMPOTENCY_KEY_LENGTH = 96


@dataclass(frozen=True, slots=True)
class PaidOperationRequest:
    user_id: str
    idempotency_key: str
    client_key_hash: str
    operation_namespace: str
    request_fingerprint: str


class PaidOperationPort(Protocol):
    async def lock(self, request: PaidOperationRequest) -> None: ...

    async def find(self, request: PaidOperationRequest) -> Any | None: ...

    def bind(self, request: PaidOperationRequest) -> None: ...

    def clear(self, request: PaidOperationRequest) -> None: ...

    async def rollback(self) -> None: ...

    def is_integrity_error(self, exc: Exception) -> bool: ...


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


def paid_operation_request(
    *,
    user_id: str,
    idempotency_key: str | None,
    operation_namespace: str,
    payload: Any,
) -> PaidOperationRequest:
    key = idempotency_key or ""
    if not key:
        raise WorkflowRequestError(
            status_code=422,
            code="idempotency_key_required",
            message="Idempotency-Key header is required",
        )
    if key != key.strip() or len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise WorkflowRequestError(
            status_code=422,
            code="idempotency_key_invalid",
            message="Idempotency-Key must be 1 to 96 non-whitespace characters",
        )
    return PaidOperationRequest(
        user_id=user_id,
        idempotency_key=key,
        client_key_hash=hashlib.sha256(key.encode("utf-8")).hexdigest(),
        operation_namespace=operation_namespace,
        request_fingerprint=canonical_request_fingerprint(
            operation_namespace,
            payload,
        ),
    )


def operation_record(
    metadata_jsonb: Any,
    client_key_hash: str,
) -> dict[str, Any] | None:
    metadata = metadata_jsonb if isinstance(metadata_jsonb, dict) else {}
    records = metadata.get(PAID_OPERATION_RECORDS_KEY)
    if not isinstance(records, dict):
        return None
    record = records.get(client_key_hash)
    return record if isinstance(record, dict) else None


def ensure_matching_operation(
    record: dict[str, Any],
    request: PaidOperationRequest,
) -> None:
    if (
        record.get("operation_namespace") != request.operation_namespace
        or record.get("request_fingerprint") != request.request_fingerprint
    ):
        raise WorkflowRequestError(
            status_code=409,
            code="idempotency_conflict",
            message="idempotency_key was already used for a different operation",
        )


def record_paid_operation_metadata(
    metadata_jsonb: Any,
    request: PaidOperationRequest,
) -> dict[str, Any]:
    existing = operation_record(metadata_jsonb, request.client_key_hash)
    if existing is not None:
        ensure_matching_operation(existing, request)
        return dict(metadata_jsonb or {})
    metadata = dict(metadata_jsonb or {})
    records = dict(metadata.get(PAID_OPERATION_RECORDS_KEY) or {})
    records[request.client_key_hash] = {
        "operation_namespace": request.operation_namespace,
        "request_fingerprint": request.request_fingerprint,
    }
    metadata[PAID_OPERATION_RECORDS_KEY] = records
    return metadata


def paid_operation_task_metadata(
    request: PaidOperationRequest,
) -> dict[str, str]:
    return {
        IDEMPOTENCY_OPERATION_NAMESPACE_KEY: request.operation_namespace,
        IDEMPOTENCY_REQUEST_FINGERPRINT_KEY: request.request_fingerprint,
        IDEMPOTENCY_CLIENT_KEY_HASH: request.client_key_hash,
    }


async def execute_paid_operation(
    action: Callable[..., Awaitable[Any]],
    *,
    request: PaidOperationRequest,
    port: PaidOperationPort,
    replay: Callable[[Any], Awaitable[Any]],
    action_kwargs: dict[str, Any],
) -> Any:
    await port.lock(request)
    existing = await port.find(request)
    if existing is not None:
        return await replay(existing)
    try:
        port.bind(request)
        try:
            return await action(**action_kwargs)
        finally:
            port.clear(request)
    except Exception as exc:
        if not port.is_integrity_error(exc):
            raise
        await port.rollback()
        await port.lock(request)
        existing = await port.find(request)
        if existing is not None:
            return await replay(existing)
        raise WorkflowRequestError(
            status_code=409,
            code="idempotency_conflict",
            message="idempotency_key conflict",
        ) from exc


__all__ = [
    "APPAREL_ACCESSORY_PREVIEWS_OPERATION",
    "APPAREL_CREATE_OPERATION",
    "APPAREL_MODEL_CANDIDATES_OPERATION",
    "APPAREL_REVISE_IMAGE_OPERATION",
    "APPAREL_SHOWCASE_IMAGES_OPERATION",
    "IDEMPOTENCY_CLIENT_KEY_HASH",
    "IDEMPOTENCY_OPERATION_NAMESPACE_KEY",
    "IDEMPOTENCY_REQUEST_FINGERPRINT_KEY",
    "MODEL_LIBRARY_GENERATE_OPERATION",
    "PAID_OPERATION_RECORDS_KEY",
    "POSTER_CREATE_OPERATION",
    "POSTER_INPAINT_RENDER_OPERATION",
    "POSTER_MASTERS_OPERATION",
    "POSTER_RENDERS_OPERATION",
    "POSTER_REVISE_RENDER_OPERATION",
    "PaidOperationPort",
    "PaidOperationRequest",
    "canonical_request_fingerprint",
    "ensure_matching_operation",
    "execute_paid_operation",
    "operation_record",
    "paid_operation_task_metadata",
    "paid_operation_request",
    "record_paid_operation_metadata",
]
