"""Durable outbox claim, delivery, and finalize orchestration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any
import uuid

from .claims import (
    DeliveryResult,
    claim_outbox_events,
    finalize_outbox_results,
)
from .contracts import (
    OUTBOX_CLAIM_TTL_S,
    OUTBOX_DELIVERY_CONCURRENCY,
    OUTBOX_DELIVERY_TIMEOUT_S,
    OUTBOX_ENQUEUE_DEDUPE_TTL_S,
    OUTBOX_MAX_FAIL_COUNT,
    ClaimedOutboxEvent,
    OutboxPayloadError,
)
from .delivery import EventPublisher, deliver_outbox_event
from .dlq import mirror
from .metrics import outbox_events_total
from .retry import clear_fail_count, increment_fail_count

logger = logging.getLogger(__name__)


def _delivery_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


async def _deliver_claimed_event(
    redis: Any,
    event: ClaimedOutboxEvent,
    *,
    event_publisher: EventPublisher,
    log: logging.Logger,
) -> DeliveryResult:
    raw_payload = event.payload
    if not isinstance(raw_payload, dict):
        log.error(
            "outbox event malformed payload id=%s kind=%s payload_type=%s payload=%r",
            event.id,
            event.kind,
            type(raw_payload).__name__,
            raw_payload,
        )
        outbox_events_total.labels(kind=event.kind, outcome="malformed").inc()
        return DeliveryResult(
            event=event,
            state="malformed",
            error="malformed_payload",
            payload={"raw_payload": repr(raw_payload)},
        )

    payload = dict(raw_payload)
    payload.setdefault("outbox_id", event.id)
    try:
        dedupe_key, marker, should_set_dedupe = await asyncio.wait_for(
            deliver_outbox_event(
                redis,
                event_id=event.id,
                kind=event.kind,
                payload=payload,
                event_publisher=event_publisher,
                log=log,
            ),
            timeout=OUTBOX_DELIVERY_TIMEOUT_S,
        )
    except OutboxPayloadError as exc:
        log.warning(
            "outbox event invalid id=%s kind=%s payload=%s",
            event.id,
            event.kind,
            payload,
        )
        outbox_events_total.labels(kind=event.kind, outcome="invalid").inc()
        return DeliveryResult(
            event=event,
            state="invalid",
            error=_delivery_error(exc),
            payload=payload,
        )
    except Exception as exc:  # noqa: BLE001
        fail_count = await increment_fail_count(redis, event.id, log=log)
        log.warning(
            "outbox delivery failed; leaving unpublished for retry "
            "event=%s marker=%s kind=%s pg_attempt=%d redis_fail_count=%s err=%s",
            event.id,
            payload.get("task_id") or payload.get("user_id"),
            event.kind,
            event.delivery_attempts,
            fail_count,
            exc,
        )
        outbox_events_total.labels(
            kind=event.kind,
            outcome="retryable_failure",
        ).inc()
        return DeliveryResult(
            event=event,
            state="retryable_failure",
            fail_count=fail_count,
            error=_delivery_error(exc),
            payload=payload,
        )

    outbox_events_total.labels(kind=event.kind, outcome="delivered").inc()
    return DeliveryResult(
        event=event,
        state="published",
        dedupe_key=dedupe_key,
        marker=marker,
        should_set_dedupe=should_set_dedupe,
        payload=payload,
    )


async def _deliver_claimed_batch(
    redis: Any,
    events: list[ClaimedOutboxEvent],
    *,
    event_publisher: EventPublisher,
    log: logging.Logger,
) -> list[DeliveryResult]:
    semaphore = asyncio.Semaphore(OUTBOX_DELIVERY_CONCURRENCY)

    async def deliver(event: ClaimedOutboxEvent) -> DeliveryResult:
        async with semaphore:
            return await _deliver_claimed_event(
                redis,
                event,
                event_publisher=event_publisher,
                log=log,
            )

    return list(await asyncio.gather(*(deliver(event) for event in events)))


async def _run_post_commit_diagnostics(
    redis: Any,
    *,
    dedupe_keys_to_set: tuple[tuple[str, str], ...],
    fail_counts_to_clear: tuple[str, ...],
    dlq_records_to_mirror: tuple[dict[str, Any], ...],
    log: logging.Logger,
) -> None:
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


async def process_outbox_batch(
    redis: Any,
    cutoff: datetime,
    limit: int,
    *,
    session_factory: Any,
    event_publisher: EventPublisher,
    log: logging.Logger = logger,
) -> int:
    """Claim durably, deliver outside transactions, then owner-finalize."""
    owner = uuid.uuid4().hex
    claimed_at = datetime.now(timezone.utc)
    claimed = await claim_outbox_events(
        session_factory=session_factory,
        cutoff=cutoff,
        limit=limit,
        owner=owner,
        now=claimed_at,
        claim_ttl_s=OUTBOX_CLAIM_TTL_S,
        log=log,
    )
    if not claimed:
        return 0

    results = await _deliver_claimed_batch(
        redis,
        claimed,
        event_publisher=event_publisher,
        log=log,
    )
    finalized = await finalize_outbox_results(
        session_factory=session_factory,
        owner=owner,
        results=results,
        now=datetime.now(timezone.utc),
        max_fail_count=OUTBOX_MAX_FAIL_COUNT,
        log=log,
    )
    if finalized is None:
        return 0
    results_by_id = {result.event.id: result for result in results}
    for event_id in finalized.published_event_ids:
        result = results_by_id[event_id]
        outbox_events_total.labels(
            kind=result.event.kind,
            outcome="published",
        ).inc()
    for event_id in finalized.lost_event_ids:
        result = results_by_id[event_id]
        log.warning(
            "outbox finalize skipped after claim ownership changed event=%s owner=%s",
            event_id,
            owner,
        )
        outbox_events_total.labels(
            kind=result.event.kind,
            outcome="claim_lost",
        ).inc()

    await _run_post_commit_diagnostics(
        redis,
        dedupe_keys_to_set=finalized.dedupe_keys_to_set,
        fail_counts_to_clear=finalized.fail_counts_to_clear,
        dlq_records_to_mirror=finalized.dlq_records_to_mirror,
        log=log,
    )
    return finalized.published
