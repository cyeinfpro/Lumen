from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class CancellationState(StrEnum):
    ACTIVE = "active"
    REQUESTED = "requested"
    UNKNOWN = "unknown"


class CancellationPort(Protocol):
    async def state(self, task_id: str) -> CancellationState: ...
