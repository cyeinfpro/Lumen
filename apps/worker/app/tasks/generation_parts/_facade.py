from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


class GenerationFacade:
    """Late-bound access to the compatibility module's globals."""

    def __init__(self) -> None:
        self._resolver: Callable[[], Mapping[str, Any]] | None = None

    def bind(self, resolver: Callable[[], Mapping[str, Any]]) -> None:
        self._resolver = resolver

    def __getattr__(self, name: str) -> Any:
        resolver = self._resolver
        if resolver is None:
            raise RuntimeError("generation facade is not bound")
        namespace = resolver()
        try:
            return namespace[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
