"""Application-owned mutable state for the workflow process lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderRoundRobinRuntime:
    counters: dict[int, int] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self) -> None:
        if self.lock.locked():
            raise RuntimeError("provider round-robin state is in use")
        self.counters.clear()


@dataclass(frozen=True)
class WorkflowRuntimeState:
    library_sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    scene_provider_round_robin: ProviderRoundRobinRuntime = field(
        default_factory=ProviderRoundRobinRuntime
    )

    def reset(self) -> None:
        if self.library_sync_lock.locked():
            raise RuntimeError("apparel library synchronization is in progress")
        self.scene_provider_round_robin.reset()


__all__ = ["ProviderRoundRobinRuntime", "WorkflowRuntimeState"]
