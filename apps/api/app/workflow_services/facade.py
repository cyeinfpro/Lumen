from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Callable, Iterator, ParamSpec, TypeVar, cast


P = ParamSpec("P")
R = TypeVar("R")


class FacadeRuntime:
    """Task-local facade lookup used by extracted workflow services."""

    def __init__(self, name: str) -> None:
        self._active: ContextVar[Any | None] = ContextVar(name, default=None)

    def current(self, default: Any) -> Any:
        return self._active.get() or default

    @contextmanager
    def use(self, facade: Any) -> Iterator[None]:
        token = self._active.set(facade)
        try:
            yield
        finally:
            self._active.reset(token)


def bind_facade(
    function: Callable[P, R],
    *,
    facade: Any,
    runtime: FacadeRuntime,
) -> Callable[P, R]:
    """Bind a route module facade without changing the callable signature."""

    if iscoroutinefunction(function):

        @wraps(function)
        async def async_bound(*args: P.args, **kwargs: P.kwargs) -> Any:
            with runtime.use(facade):
                return await function(*args, **kwargs)

        return cast(Callable[P, R], async_bound)

    @wraps(function)
    def bound(*args: P.args, **kwargs: P.kwargs) -> R:
        with runtime.use(facade):
            return function(*args, **kwargs)

    return bound
