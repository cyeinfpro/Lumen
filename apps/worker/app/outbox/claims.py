"""Durable claim and owner-checked finalize transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any, Literal

from sqlalchemy import select, update

from lumen_core.models import OutboxEvent

from .contracts import ClaimedOutboxEvent
from .dlq import persist, persist_once, resolve
from .retry import retry_delay_seconds

logger = logging.getLogger(__name__)

DeliveryState = Literal["published", "malformed", "invalid", "retryable_failure"]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    event: ClaimedOutboxEvent
    state: DeliveryState
    dedupe_key: str | None = None
    marker: str | None = None
    should_set_dedupe: bool = False
    fail_count: int | None = None
    error: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    published: int
    published_event_ids: tuple[str, ...]
    dedupe_keys_to_set: tuple[tuple[str, str], ...]
    dlq_records_to_mirror: tuple[dict[str, Any], ...]
    fail_counts_to_clear: tuple[str, ...]
    lost_event_ids: tuple[str, ...]


@dataclass(slots=True)
class _FinalizeAccumulator:
    published: int
    dedupe_keys: list[tuple[str, str]]
    dlq_records: list[dict[str, Any]]
    fail_counts_to_clear: list[str]
    finalized_ids: set[str]
    delivered_ids: list[str]


def _new_finalize_accumulator() -> _FinalizeAccumulator:
    return _FinalizeAccumulator(0, [], [], [], set(), [])


async def _finalize_owned_row(
    session: Any,
    *,
    row: OutboxEvent,
    result: DeliveryResult,
    now: datetime,
    max_fail_count: int,
    accumulator: _FinalizeAccumulator,
) -> None:
    event_id = str(row.id)
    accumulator.finalized_ids.add(event_id)
    row.claim_owner = None
    row.claim_until = None
    row.last_delivery_error = result.error

    if result.state == "retryable_failure":
        durable_attempts = max(
            int(row.delivery_attempts or 0),
            int(result.event.delivery_attempts or 0),
        )
        row.next_attempt_at = now + timedelta(
            seconds=retry_delay_seconds(
                delivery_attempts=durable_attempts,
            )
        )
        if durable_attempts >= max_fail_count:
            record = await persist_once(
                session,
                event_id=event_id,
                kind=result.event.kind,
                payload=result.payload or {},
                reason="max_delivery_attempts",
                fail_count=durable_attempts,
            )
            if record is not None:
                accumulator.dlq_records.append(record)
        return

    row.next_attempt_at = None
    row.published_at = now
    accumulator.fail_counts_to_clear.append(event_id)
    if result.state == "published":
        accumulator.published += 1
        accumulator.delivered_ids.append(event_id)
        if (
            result.should_set_dedupe
            and result.dedupe_key is not None
            and result.marker is not None
        ):
            accumulator.dedupe_keys.append((result.dedupe_key, result.marker))
        return

    reason = "malformed_payload" if result.state == "malformed" else "invalid_payload"
    accumulator.dlq_records.append(
        persist(
            session,
            event_id=event_id,
            kind=result.event.kind,
            payload=result.payload or {},
            reason=reason,
        )
    )


async def claim_outbox_events(
    *,
    session_factory: Any,
    cutoff: datetime,
    limit: int,
    owner: str,
    now: datetime,
    claim_ttl_s: int,
    log: logging.Logger = logger,
) -> list[ClaimedOutboxEvent]:
    """Persist a short-lived claim and commit before any external delivery."""
    claim_until = now + timedelta(seconds=claim_ttl_s)
    try:
        async with session_factory() as session:
            async with session.begin():
                candidate_ids = (
                    select(OutboxEvent.id)
                    .where(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.created_at < cutoff,
                        (
                            OutboxEvent.claim_until.is_(None)
                            | (OutboxEvent.claim_until <= now)
                        ),
                        (
                            OutboxEvent.next_attempt_at.is_(None)
                            | (OutboxEvent.next_attempt_at <= now)
                        ),
                    )
                    .order_by(OutboxEvent.created_at, OutboxEvent.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                rows = await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id.in_(candidate_ids))
                    .values(
                        claim_owner=owner,
                        claim_until=claim_until,
                        delivery_attempts=OutboxEvent.delivery_attempts + 1,
                    )
                    .returning(
                        OutboxEvent.id,
                        OutboxEvent.kind,
                        OutboxEvent.payload,
                        OutboxEvent.delivery_attempts,
                        OutboxEvent.claim_until,
                    )
                )
                claimed = [
                    (
                        ClaimedOutboxEvent(
                            id=str(row.id),
                            kind=row.kind,
                            payload=row.payload,
                            delivery_attempts=row.delivery_attempts,
                            claim_until=row.claim_until,
                        )
                    )
                    for row in rows
                ]
        return claimed
    except Exception:  # noqa: BLE001
        log.warning(
            "outbox claim transaction rolled back owner=%s", owner, exc_info=True
        )
        return []


async def finalize_outbox_results(
    *,
    session_factory: Any,
    owner: str,
    results: list[DeliveryResult],
    now: datetime,
    max_fail_count: int,
    log: logging.Logger = logger,
) -> FinalizeResult | None:
    """Finalize only rows still owned by this publisher."""
    if not results:
        return FinalizeResult(0, (), (), (), (), ())

    by_id = {result.event.id: result for result in results}
    try:
        async with session_factory() as session:
            async with session.begin():
                rows = list(
                    (
                        await session.execute(
                            select(OutboxEvent)
                            .where(
                                OutboxEvent.id.in_(by_id),
                                OutboxEvent.claim_owner == owner,
                            )
                            .with_for_update()
                        )
                    ).scalars()
                )
                accumulator = _new_finalize_accumulator()

                for row in rows:
                    if row.claim_owner != owner:
                        continue
                    result = by_id.get(str(row.id))
                    if result is None:
                        continue
                    await _finalize_owned_row(
                        session,
                        row=row,
                        result=result,
                        now=now,
                        max_fail_count=max_fail_count,
                        accumulator=accumulator,
                    )

                await resolve(session, accumulator.delivered_ids)
        lost_ids = tuple(sorted(set(by_id) - accumulator.finalized_ids))
        return FinalizeResult(
            published=accumulator.published,
            published_event_ids=tuple(accumulator.delivered_ids),
            dedupe_keys_to_set=tuple(accumulator.dedupe_keys),
            dlq_records_to_mirror=tuple(accumulator.dlq_records),
            fail_counts_to_clear=tuple(accumulator.fail_counts_to_clear),
            lost_event_ids=lost_ids,
        )
    except Exception:  # noqa: BLE001
        log.warning(
            "outbox finalize transaction rolled back owner=%s",
            owner,
            exc_info=True,
        )
        return None
