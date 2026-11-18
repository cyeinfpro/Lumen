"""Single-owner default runtime slot with scoped context overrides for core."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Generic, TypeVar


T = TypeVar("T")


class RuntimeSlot(Generic[T]):
    """Own a process-scoped runtime with context-local test overrides."""

    def __init__(
        self,
        name: str,
        *,
        default_factory: Callable[[], T] | None = None,
    ) -> None:
        self._default: T | None = None
        self._default_factory = default_factory
        self._default_lock = Lock()
        self._active: ContextVar[T | None] = ContextVar(name, default=None)

    def install_default(self, runtime: T) -> None:
        with self._default_lock:
            self._default = runtime

    def clear_default(self, runtime: T) -> None:
        with self._default_lock:
            if self._default is runtime:
                self._default = None

    def current(self) -> T:
        runtime = self.current_or_none()
        if runtime is None and self._default_factory is not None:
            with self._default_lock:
                if self._default is None:
                    self._default = self._default_factory()
                runtime = self._default
        if runtime is None:
            raise RuntimeError("Core runtime slot is not configured")
        return runtime

    def current_or_none(self) -> T | None:
        active = self._active.get()
        if active is not None:
            return active
        return self._default

    @contextmanager
    def use(self, runtime: T) -> Iterator[None]:
        token = self._active.set(runtime)
        try:
            yield
        finally:
            self._active.reset(token)


__all__ = ["RuntimeSlot"]
