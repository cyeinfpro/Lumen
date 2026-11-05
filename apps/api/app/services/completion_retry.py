"""Completion retry state and local artifact repair outbox policy."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import CompletionStatus
from lumen_core.model_entities import Completion, OutboxEvent
from lumen_core.upstream_billing import clear_upstream_execution_state

from .message_submission_billing import http_error as _http


_COMPLETION_EXECUTION_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "reasoning_tokens",
    "image_output_tokens",
)
_COMPLETION_EXECUTION_REQUEST_KEYS = frozenset(
    {
        "tool_image_reserved_micro",
        "completion_usage_execution_epoch",
        "completion_usage_attempt_epoch",
        "completion_billing_state",
        "completion_billing_pending_reason",
        "completion_billing_pending_at",
        "completion_billing_reconcile_attempts",
        "completion_billing_reconciled_at",
        "completion_billing_reconciled_source",
        "completion_usage_state",
        "completion_usage_unknown_reason",
        "context",
        "memory",
    }
)


def reset_completion_execution_fields(completion: Completion) -> None:
    completion.text = ""
    for field in _COMPLETION_EXECUTION_USAGE_FIELDS:
        setattr(completion, field, 0)


def clear_completion_execution_request(completion: Completion) -> dict[str, object]:
    request = clear_upstream_execution_state(completion)
    for key in _COMPLETION_EXECUTION_REQUEST_KEYS:
        request.pop(key, None)
    return request


async def queue_completion_artifact_repair(
    db: AsyncSession,
    comp: Completion,
    *,
    user_id: str,
) -> tuple[dict[str, Any], str]:
    """Commit local-only repair without resetting execution or billing."""
    req = dict(comp.upstream_request or {})
    if (
        comp.status != CompletionStatus.SUCCEEDED.value
        or comp.cancel_requested_at is not None
        or req.get("artifact_format") != "svg"
        or req.get("artifact_status") not in {"processing_failed", "pending"}
        or not req.get("completion_checkpoint_response_id")
        or req.get("completion_checkpoint_usage_exact") is not True
        or req.get("completion_checkpoint_state") != "billing_ready"
        or req.get("completion_checkpoint_execution_epoch") != comp.execution_epoch
        or req.get("completion_checkpoint_attempt_epoch") != comp.attempt
    ):
        raise _http(
            "artifact_not_retryable",
            "Only completed SVG local processing can be retried",
            409,
        )
    payload = {
        "task_id": comp.id,
        "user_id": user_id,
        "kind": "completion",
        "execution_epoch": comp.execution_epoch,
        "artifact_repair": True,
    }
    outbox = OutboxEvent(kind="completion", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    payload["outbox_id"] = str(outbox.id)
    outbox.payload = dict(payload)
    await db.commit()
    return payload, str(req["artifact_status"])
