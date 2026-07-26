"""Frozen reconciliation invariants and protocol definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import logging
from typing import Any, Callable, Protocol, runtime_checkable

from ..outbox.contracts import PendingOutboxDelivery


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    state: str
    mutate_task: bool
    release_hold: bool
    stage_outbox: bool
    invariant: str


FAILURE_MATRIX = (
    FailurePolicy(
        state="lease_active",
        mutate_task=False,
        release_hold=False,
        stage_outbox=False,
        invariant="an active worker remains the sole task owner",
    ),
    FailurePolicy(
        state="lease_unknown",
        mutate_task=False,
        release_hold=False,
        stage_outbox=False,
        invariant="unknown ownership always fails closed",
    ),
    FailurePolicy(
        state="lease_expired_retryable",
        mutate_task=True,
        release_hold=False,
        stage_outbox=True,
        invariant="requeue and durable event staging commit atomically",
    ),
    FailurePolicy(
        state="lease_expired_exhausted",
        mutate_task=True,
        release_hold=False,
        stage_outbox=True,
        invariant="terminal apply follows the durable upstream receipt",
    ),
    FailurePolicy(
        state="database_rollback",
        mutate_task=False,
        release_hold=False,
        stage_outbox=False,
        invariant="row state and staged events roll back together",
    ),
    FailurePolicy(
        state="post_commit_delivery_failure",
        mutate_task=True,
        release_hold=False,
        stage_outbox=True,
        invariant="unpublished outbox rows remain the delivery recovery source",
    ),
)


class LeaseState(Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ReconcileResult:
    touched: int = 0
    pending_outbox: list[PendingOutboxDelivery] = field(default_factory=list)

    def merge(self, other: ReconcileResult) -> None:
        self.touched += other.touched
        self.pending_outbox.extend(other.pending_outbox)


@dataclass(slots=True)
class ReconcileContext:
    redis: Any
    session: Any
    now: datetime
    billing: Any
    logger: logging.Logger
    lease_unknowns: Any | None
    stage_event: Callable[..., PendingOutboxDelivery]


@runtime_checkable
class DomainReconciler(Protocol):
    name: str

    async def reconcile(self, context: ReconcileContext) -> ReconcileResult:
        """Apply one domain's idempotent reconciliation rules."""
