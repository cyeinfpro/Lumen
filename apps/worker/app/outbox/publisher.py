"""Batch publisher transaction and retry/DLQ orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from sqlalchemy import select

from lumen_core.models import OutboxEvent

from .contracts import (
    OUTBOX_ENQUEUE_DEDUPE_TTL_S,
    OUTBOX_MAX_FAIL_COUNT,
    OutboxPayloadError,
)
from .delivery import EventPublisher, deliver_outbox_event
from .dlq import mirror, persist, persist_once, resolve
from .metrics import outbox_events_total
from .retry import clear_fail_count, increment_fail_count

logger = logging.getLogger(__name__)


async def process_outbox_batch(
    redis: Any,
    cutoff: datetime,
    limit: int,
    *,
    session_factory: Any,
    event_publisher: EventPublisher,
    log: logging.Logger = logger,
) -> int:
    """Claim, deliver, and commit one batch without losing retryability."""
    processed = 0
    dedupe_keys_to_set: list[tuple[str, str]] = []
    dlq_records_to_mirror: list[dict[str, Any]] = []
    fail_counts_to_clear: set[str] = set()
    delivered_event_ids: list[str] = []
    async with session_factory() as session:
        try:
            async with session.begin():
                rows = list(
                    (
                        await session.execute(
                            select(OutboxEvent)
                            .where(
                                OutboxEvent.published_at.is_(None),
                                OutboxEvent.created_at < cutoff,
                            )
                            .order_by(OutboxEvent.created_at)
                            .limit(limit)
                            .with_for_update(skip_locked=True)
                        )
                    ).scalars()
                )

                for row in rows:
                    event_id = str(row.id)
                    kind = row.kind
                    raw_payload = row.payload or {}
                    if not isinstance(raw_payload, dict):
                        log.error(
                            "outbox event malformed payload id=%s kind=%s "
                            "payload_type=%s payload=%r",
                            event_id,
                            kind,
                            type(raw_payload).__name__,
                            raw_payload,
                        )
                        dlq_records_to_mirror.append(
                            persist(
                                session,
                                event_id=event_id,
                                kind=kind,
                                payload={"raw_payload": repr(raw_payload)},
                                reason="malformed_payload",
                            )
                        )
                        row.published_at = datetime.now(timezone.utc)
                        fail_counts_to_clear.add(event_id)
                        outbox_events_total.labels(
                            kind=kind,
                            outcome="malformed",
                        ).inc()
                        continue

                    payload = dict(raw_payload)
                    payload.setdefault("outbox_id", event_id)
                    if payload != raw_payload:
                        row.payload = payload
                    try:
                        (
                            dedupe_key,
                            marker,
                            should_set_dedupe,
                        ) = await deliver_outbox_event(
                            redis,
                            event_id=event_id,
                            kind=kind,
                            payload=payload,
                            event_publisher=event_publisher,
                            log=log,
                        )
                    except OutboxPayloadError:
                        log.warning(
                            "outbox event invalid id=%s kind=%s payload=%s",
                            event_id,
                            kind,
                            payload,
                        )
                        dlq_records_to_mirror.append(
                            persist(
                                session,
                                event_id=event_id,
                                kind=kind,
                                payload=payload,
                                reason="invalid_payload",
                            )
                        )
                        row.published_at = datetime.now(timezone.utc)
                        fail_counts_to_clear.add(event_id)
                        outbox_events_total.labels(
                            kind=kind,
                            outcome="invalid",
                        ).inc()
                        continue
                    except Exception as exc:  # noqa: BLE001
                        fail_count = await increment_fail_count(
                            redis,
                            event_id,
                            log=log,
                        )
                        log.warning(
                            "outbox delivery failed; leaving unpublished for retry "
                            "event=%s marker=%s kind=%s fail_count=%d err=%s",
                            event_id,
                            payload.get("task_id") or payload.get("user_id"),
                            kind,
                            fail_count,
                            exc,
                        )
                        outbox_events_total.labels(
                            kind=kind,
                            outcome="retryable_failure",
                        ).inc()
                        if fail_count >= OUTBOX_MAX_FAIL_COUNT:
                            record = await persist_once(
                                session,
                                event_id=event_id,
                                kind=kind,
                                payload=payload,
                                reason="max_fail_count",
                                fail_count=fail_count,
                            )
                            if record is not None:
                                dlq_records_to_mirror.append(record)
                        continue

                    if should_set_dedupe:
                        dedupe_keys_to_set.append((dedupe_key, marker))
                    row.published_at = datetime.now(timezone.utc)
                    delivered_event_ids.append(event_id)
                    fail_counts_to_clear.add(event_id)
                    outbox_events_total.labels(kind=kind, outcome="published").inc()
                    processed += 1

                await resolve(session, delivered_event_ids)
        except Exception:  # noqa: BLE001
            log.warning("outbox event tx rolled back", exc_info=True)
            return 0

    for dedupe_key, marker in dedupe_keys_to_set:
        try:
            await redis.set(
                dedupe_key,
                marker,
                ex=OUTBOX_ENQUEUE_DEDUPE_TTL_S,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "outbox post-commit dedupe write failed key=%s err=%s",
                dedupe_key,
                exc,
            )
    for event_id in fail_counts_to_clear:
        await clear_fail_count(redis, event_id, log=log)
    for record in dlq_records_to_mirror:
        await mirror(redis, record, log=log)
    return processed
