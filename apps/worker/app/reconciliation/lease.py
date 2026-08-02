"""Fail-closed task lease observation and aggregated diagnostics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from ..observability import task_reconcile_lease_unknown_total
from .contracts import LeaseState
from .metrics import reconciliation_lease_state_total

logger = logging.getLogger(__name__)
LEASE_UNKNOWN_LOG_SAMPLE = 3
LEASE_READ_TIMEOUT_S = 2.0

# 与 worker 侧 lease TTL(60s) 对齐，留一倍余量吸收续期抖动。
LEASE_FRESHNESS_WINDOW = timedelta(seconds=120)


class LeaseKeyMissingWhileFresh(RuntimeError):
    """Lease key vanished while the task row still looks actively updated."""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"lease key missing for recently updated task={task_id}")


def _within_lease_window(
    heartbeat_at: datetime | None,
    now: datetime | None,
) -> bool:
    """Whether the task row was written recently enough to imply a live worker."""
    if heartbeat_at is None:
        return False
    if heartbeat_at.tzinfo is None:
        heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - heartbeat_at < LEASE_FRESHNESS_WINDOW


class LeaseUnknownSummary:
    def __init__(self) -> None:
        self.counts = {"generation": 0, "completion": 0}
        self.samples: list[str] = []

    def record(self, *, kind: str, task_id: str, error: Exception) -> None:
        task_reconcile_lease_unknown_total.labels(kind=kind).inc()
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if len(self.samples) < LEASE_UNKNOWN_LOG_SAMPLE:
            self.samples.append(f"{kind}:{task_id}:{type(error).__name__}")

    def log(self, *, log: logging.Logger = logger) -> None:
        total = sum(self.counts.values())
        if total == 0:
            return
        log.warning(
            "reconcile lease state unknown total=%d generations=%d "
            "completions=%d samples=%s",
            total,
            self.counts.get("generation", 0),
            self.counts.get("completion", 0),
            ",".join(self.samples),
        )


async def read_lease_state(
    redis: Any,
    task_id: str,
    *,
    kind: str | None = None,
    unknowns: LeaseUnknownSummary | None = None,
    heartbeat_at: datetime | None = None,
    now: datetime | None = None,
) -> LeaseState:
    """Read a lease without converting Redis failure into expiry.

    A missing key normally means the lease expired, but Redis may also drop it
    under memory pressure. ``heartbeat_at`` (the task's last DB write) guards
    that case: while the row was touched within one lease TTL the worker is
    still alive, so report UNKNOWN and fail closed rather than time the task
    out and release a hold the upstream has already charged for.
    """
    try:
        value = await asyncio.wait_for(
            redis.get(f"task:{task_id}:lease"),
            timeout=LEASE_READ_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        if kind is not None:
            reconciliation_lease_state_total.labels(
                domain=kind,
                state=LeaseState.UNKNOWN.value,
            ).inc()
        if kind is not None and unknowns is not None:
            unknowns.record(kind=kind, task_id=task_id, error=exc)
        return LeaseState.UNKNOWN
    if value is not None:
        state = LeaseState.ACTIVE
    elif _within_lease_window(heartbeat_at, now):
        state = LeaseState.UNKNOWN
        if kind is not None and unknowns is not None:
            unknowns.record(
                kind=kind,
                task_id=task_id,
                error=LeaseKeyMissingWhileFresh(task_id),
            )
    else:
        state = LeaseState.EXPIRED
    if kind is not None:
        reconciliation_lease_state_total.labels(
            domain=kind,
            state=state.value,
        ).inc()
    return state


async def read_lease_states(
    redis: Any,
    tasks: list[Any],
    *,
    kind: str,
    unknowns: LeaseUnknownSummary | None,
    now: datetime,
) -> dict[str, LeaseState]:
    states = await asyncio.gather(
        *(
            read_lease_state(
                redis,
                str(task.id),
                kind=kind,
                unknowns=unknowns,
                heartbeat_at=getattr(task, "updated_at", None),
                now=now,
            )
            for task in tasks
        )
    )
    return {
        str(task.id): state
        for task, state in zip(tasks, states, strict=True)
    }


async def lease_expired(redis: Any, task_id: str) -> bool:
    """Compatibility helper; UNKNOWN deliberately returns ``False``."""
    return await read_lease_state(redis, task_id) is LeaseState.EXPIRED
