"""Shared reconciliation coordinator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
import logging
from typing import Any

from ..locks import owned_redis_lock
from ..outbox.contracts import PendingOutboxDelivery
from ..outbox.staging import stage_outbox_event
from .contracts import DomainReconciler, ReconcileContext, ReconcileResult
from .lease import LeaseUnknownSummary
from .metrics import reconciliation_runs_total

BeforeReconcile = Callable[[], Awaitable[None]]
AfterCommit = Callable[[Any], Awaitable[None]]
DeliverPending = Callable[
    [Any, list[PendingOutboxDelivery]],
    Awaitable[None],
]


async def run_reconciliation(
    redis: Any,
    *,
    name: str,
    lock_key: str,
    lock_ttl_s: int,
    session_factory: Any,
    billing: Any,
    reconcilers: Iterable[DomainReconciler],
    deliver_pending: DeliverPending,
    log: logging.Logger,
    before_reconcile: BeforeReconcile | None = None,
    after_commit: AfterCommit | None = None,
    track_lease_unknowns: bool = False,
) -> int:
    """Run domain reconcilers under one owned lock and one DB transaction."""
    async with owned_redis_lock(
        redis,
        key=lock_key,
        ttl_s=lock_ttl_s,
        log=log,
    ) as acquired:
        if not acquired:
            reconciliation_runs_total.labels(
                coordinator=name,
                outcome="contended",
            ).inc()
            return 0

        try:
            if before_reconcile is not None:
                await before_reconcile()
            aggregate = ReconcileResult()
            unknowns = LeaseUnknownSummary() if track_lease_unknowns else None
            async with session_factory() as session:
                context = ReconcileContext(
                    redis=redis,
                    session=session,
                    now=datetime.now(timezone.utc),
                    billing=billing,
                    logger=log,
                    lease_unknowns=unknowns,
                    stage_event=stage_outbox_event,
                )
                for reconciler in reconcilers:
                    aggregate.merge(await reconciler.reconcile(context))
                await session.commit()
                if after_commit is not None:
                    await after_commit(session)

            if unknowns is not None:
                unknowns.log(log=log)
            await deliver_pending(redis, aggregate.pending_outbox)
        except Exception:  # noqa: BLE001
            reconciliation_runs_total.labels(
                coordinator=name,
                outcome="failed",
            ).inc()
            raise

    reconciliation_runs_total.labels(
        coordinator=name,
        outcome="completed",
    ).inc()
    return aggregate.touched
