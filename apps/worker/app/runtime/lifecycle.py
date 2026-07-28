from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


CleanupCallback = Callable[[], Awaitable[None] | None]


class LifecycleState(StrEnum):
    NEW = "new"
    STARTED = "started"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    resource: str
    error_type: str


@dataclass(frozen=True, slots=True)
class LifecycleDiagnostics:
    owner: str
    state: LifecycleState
    resources: tuple[str, ...]
    cleanup_failures: tuple[CleanupFailure, ...]


class RuntimeLifecycle:
    """Own worker resources and close them deterministically without fail-fast."""

    def __init__(self, owner: str, *, logger: logging.Logger | None = None) -> None:
        self._owner = owner
        self._logger = logger or logging.getLogger(__name__)
        self._state = LifecycleState.NEW
        self._resources: list[tuple[str, CleanupCallback]] = []
        self._cleanup_failures: list[CleanupFailure] = []
        self._close_lock = asyncio.Lock()

    @property
    def state(self) -> LifecycleState:
        return self._state

    def own(self, name: str, cleanup: CleanupCallback) -> None:
        if self._state in {LifecycleState.CLOSING, LifecycleState.CLOSED}:
            raise RuntimeError(f"{self._owner} runtime is already closing")
        if any(resource_name == name for resource_name, _ in self._resources):
            raise ValueError(f"duplicate runtime resource: {name}")
        self._resources.append((name, cleanup))

    def start(self) -> None:
        if self._state is LifecycleState.CLOSED:
            raise RuntimeError(f"{self._owner} runtime is already closed")
        if self._state is LifecycleState.CLOSING:
            raise RuntimeError(f"{self._owner} runtime is closing")
        self._state = LifecycleState.STARTED

    async def close(self) -> None:
        async with self._close_lock:
            if self._state is LifecycleState.CLOSED:
                return
            self._state = LifecycleState.CLOSING
            for name, cleanup in reversed(self._resources):
                try:
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:  # noqa: BLE001
                    self._cleanup_failures.append(
                        CleanupFailure(name, type(exc).__name__)
                    )
                    self._logger.warning(
                        "%s runtime cleanup failed resource=%s",
                        self._owner,
                        name,
                        exc_info=True,
                    )
            self._state = LifecycleState.CLOSED

    def diagnostics(self) -> LifecycleDiagnostics:
        return LifecycleDiagnostics(
            owner=self._owner,
            state=self._state,
            resources=tuple(name for name, _ in self._resources),
            cleanup_failures=tuple(self._cleanup_failures),
        )
