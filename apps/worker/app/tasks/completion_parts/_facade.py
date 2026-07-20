"""Late-bound access to the completion task compatibility facade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class CompletionFacade:
    """Resolve completion dependencies at call time.

    The completion task historically exposed many private helpers that tests
    and sibling tasks monkeypatch.  Keeping the resolver late-bound lets the
    new execution modules use those same symbols without importing the facade
    module and creating a cycle.
    """

    def __init__(self) -> None:
        self._resolver: Callable[[], Mapping[str, Any]] | None = None

    def bind(self, resolver: Callable[[], Mapping[str, Any]]) -> None:
        self._resolver = resolver

    def __getattr__(self, name: str) -> Any:
        resolver = self._resolver
        if resolver is None:
            raise RuntimeError("completion facade is not bound")
        namespace = resolver()
        try:
            return namespace[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_g = CompletionFacade()
bind_completion_facade = _g.bind

__all__ = ["CompletionFacade", "_g", "bind_completion_facade"]
