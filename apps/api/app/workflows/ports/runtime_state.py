"""Runtime-state contracts consumed by workflow adapters."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol


class AsyncLockPort(Protocol):
    async def __aenter__(self) -> AsyncLockPort: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...

    def locked(self) -> bool: ...


class ProviderRoundRobinStatePort(Protocol):
    counters: dict[int, int]
    lock: AsyncLockPort


__all__ = ["AsyncLockPort", "ProviderRoundRobinStatePort"]
