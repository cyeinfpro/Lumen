from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


class RuntimeSlot(Generic[T]):
    """Single-owner default runtime with task-local explicit overrides."""

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
        if self._default is not None and self._default is not runtime:
            raise RuntimeError("default runtime is already installed")
        self._default = runtime

    def clear_default(self, runtime: T) -> None:
        if self._default is runtime:
            self._default = None

    def current(self) -> T:
        runtime = self._active.get() or self._default
        if runtime is None and self._default_factory is not None:
            with self._default_lock:
                if self._default is None:
                    self._default = self._default_factory()
                runtime = self._default
        if runtime is None:
            raise RuntimeError("task runtime is not configured")
        return runtime

    @contextmanager
    def use(self, runtime: T) -> Iterator[None]:
        token = self._active.set(runtime)
        try:
            yield
        finally:
            self._active.reset(token)
