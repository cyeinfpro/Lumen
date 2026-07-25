from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class LeaseState(StrEnum):
    ACQUIRED = "acquired"
    HELD = "held"
    LOST = "lost"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TaskLease:
    task_id: str
    owner: str
    token: str
    ttl_s: int


@dataclass(frozen=True, slots=True)
class LeaseAcquireResult:
    state: LeaseState
    lease: TaskLease | None = None


@dataclass(frozen=True, slots=True)
class LeaseRenewResult:
    state: LeaseState
    lease: TaskLease


class TaskLeasePort(Protocol):
    async def acquire(
        self,
        task_id: str,
        owner: str,
        ttl_s: int,
    ) -> LeaseAcquireResult: ...

    async def renew(self, lease: TaskLease) -> LeaseRenewResult: ...

    async def release(self, lease: TaskLease) -> None: ...


def lease_allows_mutation(state: LeaseState) -> bool:
    """Fail closed when ownership is lost or cannot be established."""

    return state in {LeaseState.ACQUIRED, LeaseState.HELD}
