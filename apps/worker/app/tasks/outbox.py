"""Compatibility entrypoint for outbox publishing and reconciliation.

The implementation lives in ``app.outbox``, ``app.reconciliation``, and
``app.locks``. This module preserves worker startup, cron schedules, and the
legacy helper imports used by task event-delivery code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
import logging
from typing import Any

from arq.cron import cron

from .. import billing as worker_billing
from .. import locks as redis_locks
from .. import outbox as outbox_domain
from .. import reconciliation as reconciliation_domain
from ..db import SessionLocal
from ..sse_publish import publish_event

logger = logging.getLogger(__name__)

# Legacy constants and types retained for tests and internal task imports.
_OUTBOX_LOCK_KEY = outbox_domain.OUTBOX_LOCK_KEY
_OUTBOX_LOCK_TTL_S = outbox_domain.OUTBOX_LOCK_TTL_S
_OUTBOX_BATCH = outbox_domain.OUTBOX_BATCH
_OUTBOX_CLAIM_TTL_S = outbox_domain.OUTBOX_CLAIM_TTL_S
_OUTBOX_DELIVERY_CONCURRENCY = outbox_domain.OUTBOX_DELIVERY_CONCURRENCY
_OUTBOX_DELIVERY_TIMEOUT_S = outbox_domain.OUTBOX_DELIVERY_TIMEOUT_S
_OUTBOX_DLQ_KEY = outbox_domain.OUTBOX_DLQ_KEY
_OUTBOX_DLQ_MAXLEN = outbox_domain.OUTBOX_DLQ_MAXLEN
_OUTBOX_MAX_FAIL_COUNT = outbox_domain.OUTBOX_MAX_FAIL_COUNT
_OUTBOX_FAIL_COUNT_HASH = outbox_domain.OUTBOX_FAIL_COUNT_HASH
_OUTBOX_FAIL_COUNT_TTL_S = outbox_domain.OUTBOX_FAIL_COUNT_TTL_S
_OUTBOX_ENQUEUE_DEDUPE_PREFIX = outbox_domain.OUTBOX_ENQUEUE_DEDUPE_PREFIX
_OUTBOX_ENQUEUE_DEDUPE_TTL_S = outbox_domain.OUTBOX_ENQUEUE_DEDUPE_TTL_S
_OUTBOX_TASK_JOBS = outbox_domain.OUTBOX_TASK_JOBS
_RELEASE_OWNED_LOCK_LUA = redis_locks.RELEASE_OWNED_LOCK_LUA
_RENEW_OWNED_LOCK_LUA = redis_locks.RENEW_OWNED_LOCK_LUA
_INCR_FAIL_COUNT_LUA = outbox_domain.INCR_FAIL_COUNT_LUA
_OutboxPayloadError = outbox_domain.OutboxPayloadError
_PendingOutboxDelivery = outbox_domain.PendingOutboxDelivery
_LeaseState = reconciliation_domain.LeaseState
_LeaseUnknownSummary = reconciliation_domain.LeaseUnknownSummary
_RECON_STUCK_AFTER = reconciliation_domain.RECON_STUCK_AFTER
_RECON_GENERATION_MAX_ATTEMPTS = 5
_RECON_COMPLETION_MAX_ATTEMPTS = 3
_RECON_TIMEOUT_CODE = reconciliation_domain.RECON_TIMEOUT_CODE
_RECON_TIMEOUT_MESSAGE = reconciliation_domain.RECON_TIMEOUT_MESSAGE
_EV_GEN_REQUEUED = reconciliation_domain.EV_GEN_REQUEUED
_EV_COMP_REQUEUED = reconciliation_domain.EV_COMP_REQUEUED


async def _renew_owned_lock(
    redis: Any,
    *,
    key: str,
    token: str,
    ttl_s: int,
) -> bool | None:
    return await redis_locks.renew_owned_lock(
        redis,
        key=key,
        token=token,
        ttl_s=ttl_s,
        log=logger,
    )


async def _release_owned_lock(redis: Any, *, key: str, token: str) -> None:
    await redis_locks.release_owned_lock(redis, key=key, token=token, log=logger)


def _owned_redis_lock(redis: Any, *, key: str, ttl_s: int):
    return redis_locks.owned_redis_lock(redis, key=key, ttl_s=ttl_s, log=logger)


async def _deliver_outbox_event(
    redis: Any,
    *,
    event_id: str,
    kind: str,
    payload: dict[str, Any],
) -> tuple[str, str, bool]:
    return await outbox_domain.deliver_outbox_event(
        redis,
        event_id=event_id,
        kind=kind,
        payload=payload,
        event_publisher=publish_event,
        log=logger,
    )


async def _process_outbox_batch(redis: Any, cutoff: datetime, limit: int) -> int:
    return await outbox_domain.process_outbox_batch(
        redis,
        cutoff,
        limit,
        session_factory=SessionLocal,
        event_publisher=publish_event,
        log=logger,
    )


async def publish_outbox(ctx: dict[str, Any]) -> int:
    """Publish one due batch while preserving the legacy cron callable."""
    redis = ctx["redis"]
    async with _owned_redis_lock(
        redis,
        key=_OUTBOX_LOCK_KEY,
        ttl_s=_OUTBOX_LOCK_TTL_S,
    ) as acquired:
        if not acquired:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=2)
        processed = await _process_outbox_batch(redis, cutoff, _OUTBOX_BATCH)
    if processed:
        logger.info("outbox: published %d events", processed)
    return processed


_increment_outbox_fail_count = partial(outbox_domain.increment_fail_count, log=logger)
_persist_outbox_dlq_once = outbox_domain.persist_once
_persist_outbox_dlq = outbox_domain.persist
_resolve_outbox_dlq_rows = outbox_domain.resolve
_mirror_outbox_dlq = partial(outbox_domain.mirror, log=logger)


_stage_outbox_event = outbox_domain.stage_outbox_event


async def _mark_staged_outbox_published(event_id: str) -> bool:
    return await outbox_domain.mark_staged_outbox_published(
        SessionLocal,
        event_id,
        log=logger,
    )


async def _deliver_staged_outbox_events(
    redis: Any,
    deliveries: list[outbox_domain.PendingOutboxDelivery],
) -> None:
    await outbox_domain.deliver_staged_outbox_events(
        redis,
        deliveries,
        session_factory=SessionLocal,
        event_publisher=publish_event,
        log=logger,
    )


_read_lease_state = reconciliation_domain.read_lease_state
_lease_expired = reconciliation_domain.lease_expired
_memory_retry_backoff_seconds = reconciliation_domain.memory_retry_backoff_seconds
_aware_utc = reconciliation_domain.aware_utc
_memory_run_due = reconciliation_domain.memory_run_due
_memory_run_invalid_reason = reconciliation_domain.memory_run_invalid_reason
_cancel_memory_run = reconciliation_domain.cancel_memory_run
_requeue_memory_run = reconciliation_domain.requeue_memory_run


async def reconcile_tasks(ctx: dict[str, Any]) -> int:
    touched = await reconciliation_domain.reconcile_tasks(
        ctx["redis"],
        session_factory=SessionLocal,
        event_publisher=publish_event,
        billing=worker_billing,
        log=logger,
    )
    if touched:
        logger.info("reconcile: touched %d rows", touched)
    return touched


async def reconcile_memory_extractions(ctx: dict[str, Any]) -> int:
    touched = await reconciliation_domain.reconcile_memory_extractions(
        ctx["redis"],
        session_factory=SessionLocal,
        event_publisher=publish_event,
        billing=worker_billing,
        log=logger,
    )
    if touched:
        logger.info("memory extraction reconcile: touched %d rows", touched)
    return touched


def _build_cron_jobs() -> list[Any]:
    return [
        cron(
            publish_outbox,
            second=set(range(0, 60, 2)),
            run_at_startup=True,
        ),
        cron(
            reconcile_tasks,
            minute=set(range(0, 60)),
        ),
        cron(
            reconcile_memory_extractions,
            second={20},
            run_at_startup=True,
        ),
    ]


cron_jobs = _build_cron_jobs()

__all__ = (
    "cron_jobs",
    "publish_outbox",
    "reconcile_memory_extractions",
    "reconcile_tasks",
)
