"""Configured reconciliation entrypoints used by the compatibility facade."""

from __future__ import annotations

import logging
from typing import Any

from ..outbox.delivery import EventPublisher, deliver_staged_outbox_events
from .bonus_billing import BONUS_BILLING_RECONCILER
from .cleanup import cleanup_terminal_sentinels
from .completion_billing import COMPLETION_BILLING_RECONCILER
from .coordinator import run_reconciliation
from .memory import MEMORY_RECONCILER
from .task_domains import COMPLETION_RECONCILER, GENERATION_RECONCILER

logger = logging.getLogger(__name__)

RECON_LOCK_KEY = "lock:outbox:reconciler"
RECON_LOCK_TTL_S = 50
MEMORY_RECON_LOCK_KEY = "lock:outbox:memory_reconciler"
MEMORY_RECON_LOCK_TTL_S = 50


async def reconcile_tasks(
    redis: Any,
    *,
    session_factory: Any,
    event_publisher: EventPublisher,
    billing: Any,
    log: logging.Logger = logger,
) -> int:
    async def deliver_pending(redis_client: Any, deliveries: list[Any]) -> None:
        await deliver_staged_outbox_events(
            redis_client,
            deliveries,
            session_factory=session_factory,
            event_publisher=event_publisher,
            log=log,
        )

    async def cleanup() -> None:
        try:
            await cleanup_terminal_sentinels(
                redis,
                session_factory=session_factory,
                log=log,
            )
        except Exception:  # noqa: BLE001
            log.warning("cleanup_terminal_sentinels failed", exc_info=True)

    return await run_reconciliation(
        redis,
        name="tasks",
        lock_key=RECON_LOCK_KEY,
        lock_ttl_s=RECON_LOCK_TTL_S,
        session_factory=session_factory,
        billing=billing,
        reconcilers=(
            GENERATION_RECONCILER,
            COMPLETION_RECONCILER,
            COMPLETION_BILLING_RECONCILER,
            BONUS_BILLING_RECONCILER,
        ),
        deliver_pending=deliver_pending,
        log=log,
        before_reconcile=cleanup,
        after_commit=billing.flush_balance_cache_refreshes,
        track_lease_unknowns=True,
    )


async def reconcile_memory_extractions(
    redis: Any,
    *,
    session_factory: Any,
    event_publisher: EventPublisher,
    billing: Any,
    log: logging.Logger = logger,
) -> int:
    async def deliver_pending(redis_client: Any, deliveries: list[Any]) -> None:
        await deliver_staged_outbox_events(
            redis_client,
            deliveries,
            session_factory=session_factory,
            event_publisher=event_publisher,
            log=log,
        )

    return await run_reconciliation(
        redis,
        name="memory_extractions",
        lock_key=MEMORY_RECON_LOCK_KEY,
        lock_ttl_s=MEMORY_RECON_LOCK_TTL_S,
        session_factory=session_factory,
        billing=billing,
        reconcilers=(MEMORY_RECONCILER,),
        deliver_pending=deliver_pending,
        log=log,
    )
