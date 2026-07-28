from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TaskIdentity:
    task_id: str
    owner: str
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class IdempotencyToken:
    task_id: str
    operation: str
    attempt: int = 0
    discriminator: str = ""

    @property
    def key(self) -> str:
        suffix = f":{self.discriminator}" if self.discriminator else ""
        return f"{self.task_id}:{self.attempt}:{self.operation}{suffix}"


class ClockPort(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class AsyncCloseable(Protocol):
    async def close(self) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        import time

        return time.monotonic()
