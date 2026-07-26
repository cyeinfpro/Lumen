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
    """Idempotently mark an already committed staged event as published.

    事务边界显式化（E-6）：published_at 与 DLQ resolve 必须同进同出，
    不能依赖 autobegin 的隐式行为——否则版本升级或配置漂移时可能只提交半边，
    造成「已投递但 DLQ 仍在」或反之的状态不一致。
    """
    async with session_factory() as session:
        async with session.begin():
            row = await session.get(OutboxEvent, event_id)
            if row is None:
                log.error(
                    "post-commit outbox delivery lost persistence event=%s", event_id
                )
                return False
            if row.published_at is None:
                row.published_at = datetime.now(timezone.utc)
            await resolve(session, [event_id])
    return True
