from __future__ import annotations

from typing import Protocol

from ..domain.resource_estimate import ImageResourceEstimate


class CapacityLeasePort(Protocol):
    async def renew(self) -> bool: ...

    async def release(self) -> None: ...


class CapacityPort(Protocol):
    async def reserve(
        self,
        estimate: ImageResourceEstimate,
    ) -> CapacityLeasePort: ...
