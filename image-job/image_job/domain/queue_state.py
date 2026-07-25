"""Queue health snapshots."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueueState:
    accepting: bool
    shutdown: bool
    queue_size: int
    queue_max: int
    queued_known: int
    inflight: int
    workers_alive: int
    workers_expected: int
    background_alive: int
    last_worker_heartbeat: float | None
    last_reconcile: float | None
