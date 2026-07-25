"""Fail-closed task lease observation and aggregated diagnostics."""

from __future__ import annotations

import logging
from typing import Any

from ..observability import task_reconcile_lease_unknown_total
from .contracts import LeaseState
from .metrics import reconciliation_lease_state_total

logger = logging.getLogger(__name__)
LEASE_UNKNOWN_LOG_SAMPLE = 3


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
) -> LeaseState:
    """Read a lease without converting Redis failure into expiry."""
    try:
        value = await redis.get(f"task:{task_id}:lease")
    except Exception as exc:  # noqa: BLE001
        if kind is not None:
            reconciliation_lease_state_total.labels(
                domain=kind,
                state=LeaseState.UNKNOWN.value,
            ).inc()
        if kind is not None and unknowns is not None:
            unknowns.record(kind=kind, task_id=task_id, error=exc)
        return LeaseState.UNKNOWN
    state = LeaseState.EXPIRED if value is None else LeaseState.ACTIVE
    if kind is not None:
        reconciliation_lease_state_total.labels(
            domain=kind,
            state=state.value,
        ).inc()
    return state


async def lease_expired(redis: Any, task_id: str) -> bool:
    """Compatibility helper; UNKNOWN deliberately returns ``False``."""
    return await read_lease_state(redis, task_id) is LeaseState.EXPIRED
