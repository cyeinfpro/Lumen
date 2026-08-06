"""Durable outbox dead-letter persistence and best-effort Redis mirroring."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from sqlalchemy import select, update

from lumen_core.models import OutboxDeadLetter

from .contracts import OUTBOX_DLQ_KEY, OUTBOX_DLQ_MAXLEN
from .metrics import outbox_dlq_total

logger = logging.getLogger(__name__)


async def persist_once(
    session: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
    reason: str,
    fail_count: int,
) -> dict[str, Any] | None:
    existing_id = (
        await session.execute(
            select(OutboxDeadLetter.id)
            .where(
                OutboxDeadLetter.outbox_id == event_id,
                OutboxDeadLetter.resolved_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_id is not None:
        return None
    return persist(
        session,
        event_id=event_id,
        kind=kind,
        payload=payload,
        reason=reason,
        fail_count=fail_count,
    )


def persist(
    session: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
    reason: str = "unspecified",
    fail_count: int = 0,
) -> dict[str, Any]:
    """Persist poison data in the transaction already locking its parent."""
    record = {
        "event_id": event_id,
        "kind": kind,
        "payload": payload,
        "reason": reason,
        "fail_count": fail_count,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    error_class = {
        "malformed_payload": "OutboxMalformedPayload",
        "invalid_payload": "OutboxInvalidPayload",
        "max_fail_count": "OutboxEnqueueFailed",
        "max_delivery_attempts": "OutboxEnqueueFailed",
    }.get(reason, "OutboxPublishFailed")
    session.add(
        OutboxDeadLetter(
            outbox_id=event_id,
            event_type=f"outbox.{kind}",
            payload=payload,
            error_class=error_class,
            error_message=reason,
            retry_count=fail_count,
        )
    )
    outbox_dlq_total.labels(kind=kind, reason=reason).inc()
    return record


async def resolve(session: Any, event_ids: list[str]) -> None:
    if not event_ids:
        return
    await session.execute(
        update(OutboxDeadLetter)
        .where(
            OutboxDeadLetter.outbox_id.in_(event_ids),
            OutboxDeadLetter.resolved_at.is_(None),
        )
        .values(resolved_at=datetime.now(timezone.utc))
    )


async def mirror(
    redis: Any,
    record: dict[str, Any],
    *,
    log: logging.Logger = logger,
) -> None:
    """Mirror diagnostics only after the PostgreSQL transaction commits."""
    try:
        await redis.lpush(
            OUTBOX_DLQ_KEY,
            json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        )
        await redis.ltrim(OUTBOX_DLQ_KEY, 0, OUTBOX_DLQ_MAXLEN - 1)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "outbox Redis DLQ mirror failed event=%s err=%s",
            record.get("event_id"),
            exc,
        )
