"""Durable staging and idempotent finalization for post-commit delivery."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from lumen_core.models import OutboxEvent, new_uuid7

from .contracts import PendingOutboxDelivery
from .dlq import resolve

logger = logging.getLogger(__name__)


def stage_outbox_event(
    session: Any,
    *,
    kind: str,
    payload: dict[str, Any],
) -> PendingOutboxDelivery:
    event_id = new_uuid7()
    durable_payload = {**payload, "outbox_id": event_id}
    session.add(
        OutboxEvent(
            id=event_id,
            kind=kind,
            payload=durable_payload,
            published_at=None,
        )
    )
    return event_id, kind, durable_payload


async def mark_staged_outbox_published(
    session_factory: Any,
    event_id: str,
    *,
    log: logging.Logger = logger,
) -> bool:
    """Idempotently mark an already committed staged event as published."""
    async with session_factory() as session:
        row = await session.get(OutboxEvent, event_id)
        if row is None:
            log.error("post-commit outbox delivery lost persistence event=%s", event_id)
            return False
        if row.published_at is None:
            row.published_at = datetime.now(timezone.utc)
        await resolve(session, [event_id])
        await session.commit()
    return True
