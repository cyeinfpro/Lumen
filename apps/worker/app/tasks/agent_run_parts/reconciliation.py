"""Requeue safe Agent claims and conservatively settle stale dispatched runs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from lumen_core.agent_events import AgentRunStatus
from lumen_core.model_entities import AgentCapabilityGrant, AgentRun

from ...db import SessionLocal
from ...observability import agent_reconciliation_total
from .persistence import reconcile_cancelled_agent_hold


logger = logging.getLogger(__name__)
_RECONCILE_BATCH = 100
_QUEUED_STALE_SECONDS = 60
_RUNNING_STALE_SECONDS = 5 * 60


async def reconcile_agent_runs(ctx: dict[str, Any]) -> int:
    redis = ctx.get("redis")
    if redis is None:
        return 0
    now = datetime.now(timezone.utc)
    queued_cutoff = now - timedelta(seconds=_QUEUED_STALE_SECONDS)
    running_cutoff = now - timedelta(seconds=_RUNNING_STALE_SECONDS)
    async with SessionLocal() as db:
        async with db.begin():
            await db.execute(
                delete(AgentCapabilityGrant).where(
                    AgentCapabilityGrant.expires_at < now
                )
            )
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(AgentRun)
                    .where(
                        (
                            (AgentRun.status == AgentRunStatus.QUEUED.value)
                            & (AgentRun.updated_at < queued_cutoff)
                        )
                        | (
                            (AgentRun.status == AgentRunStatus.RUNNING.value)
                            & (AgentRun.updated_at < running_cutoff)
                        )
                        | (
                            (AgentRun.status == AgentRunStatus.CANCELLED.value)
                            & (AgentRun.text_hold_micro > 0)
                        )
                    )
                    .order_by(AgentRun.updated_at.asc())
                    .limit(_RECONCILE_BATCH)
                )
            )
            .scalars()
            .all()
        )
    touched = 0
    for run in rows:
        if run.status == AgentRunStatus.CANCELLED.value:
            try:
                touched += int(await reconcile_cancelled_agent_hold(run.id))
                agent_reconciliation_total.labels(
                    action="billing",
                    outcome="reconciled",
                ).inc()
            except Exception:
                agent_reconciliation_total.labels(
                    action="billing",
                    outcome="failed",
                ).inc()
                logger.warning(
                    "cancelled Agent billing reconciliation failed run=%s",
                    run.id,
                    exc_info=True,
                )
            continue
        try:
            await redis.enqueue_job(
                "run_agent",
                run.id,
                _job_id=(
                    f"lumen:agent-reconcile:{run.id}:"
                    f"{run.execution_epoch}:{run.attempt}"
                ),
            )
            touched += 1
            agent_reconciliation_total.labels(
                action="requeue",
                outcome="enqueued",
            ).inc()
        except Exception:
            agent_reconciliation_total.labels(
                action="requeue",
                outcome="failed",
            ).inc()
            logger.warning("Agent run reconciliation enqueue failed run=%s", run.id)
    return touched


__all__ = ["reconcile_agent_runs"]
