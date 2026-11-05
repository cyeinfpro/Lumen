"""Canonical idempotency contracts for paid message operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from lumen_core.schema_models.messaging import PostMessageIn


MESSAGE_CREATE_IDEMPOTENCY_OPERATION = "conversation.message.create"
MESSAGE_REGENERATE_IDEMPOTENCY_OPERATION = "conversation.message.regenerate"
SILENT_GENERATION_IDEMPOTENCY_OPERATION = "conversation.generation.create"
IDEMPOTENCY_OPERATION_NAMESPACE_KEY = "idempotency_operation_namespace"
IDEMPOTENCY_REQUEST_FINGERPRINT_KEY = "idempotency_request_fingerprint"


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


def message_request_fingerprint(body: PostMessageIn) -> str:
    payload = body.model_dump(mode="json", exclude={"idempotency_key"})
    # Optional 2.6 controls must not change the identity of an ordinary retry.
    if payload.get("markup_image_id") is None:
        payload.pop("markup_image_id", None)
    chat_params = payload.get("chat_params")
    if isinstance(chat_params, dict) and chat_params.get("artifact_format") is None:
        chat_params.pop("artifact_format", None)
    image_params = payload.get("image_params")
    if isinstance(image_params, dict):
        for key in ("complexity", "frame_count", "frame_duration_ms", "svg"):
            if image_params.get(key) is None:
                image_params.pop(key, None)
    return canonical_request_fingerprint(
        MESSAGE_CREATE_IDEMPOTENCY_OPERATION,
        payload,
    )


def regenerate_request_fingerprint(
    *,
    target_message_id: str,
    intent: str,
) -> str:
    return canonical_request_fingerprint(
        MESSAGE_REGENERATE_IDEMPOTENCY_OPERATION,
        {
            "target_message_id": target_message_id,
            "intent": intent,
        },
    )


def idempotency_request_metadata(
    metadata: dict[str, Any] | None,
    *,
    operation_namespace: str,
    request_fingerprint: str,
) -> dict[str, Any]:
    return {
        **(metadata or {}),
        IDEMPOTENCY_OPERATION_NAMESPACE_KEY: operation_namespace,
        IDEMPOTENCY_REQUEST_FINGERPRINT_KEY: request_fingerprint,
    }


def task_idempotency_metadata(task: Any) -> tuple[str | None, str | None]:
    upstream_request = getattr(task, "upstream_request", None)
    if not isinstance(upstream_request, dict):
        return None, None
    operation_namespace = upstream_request.get(IDEMPOTENCY_OPERATION_NAMESPACE_KEY)
    request_fingerprint = upstream_request.get(IDEMPOTENCY_REQUEST_FINGERPRINT_KEY)
    return (
        operation_namespace if isinstance(operation_namespace, str) else None,
        request_fingerprint if isinstance(request_fingerprint, str) else None,
    )


def require_matching_task_idempotency(
    tasks: list[Any],
    *,
    operation_namespace: str,
    request_fingerprint: str,
    http_error: Callable[[str, str, int], Exception],
) -> None:
    for task in tasks:
        stored_operation, stored_fingerprint = task_idempotency_metadata(task)
        if stored_operation is None and stored_fingerprint is None:
            continue
        if (
            stored_operation != operation_namespace
            or stored_fingerprint != request_fingerprint
        ):
            raise http_error(
                "idempotency_conflict",
                "idempotency_key was already used for a different operation",
                409,
            )
