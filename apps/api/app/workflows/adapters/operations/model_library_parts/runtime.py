"""Late-bound access to the model-library operation compatibility surface."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Any


class ModelLibraryRuntimeAdapter:
    """Resolve an explicit dependency whitelist at call time."""

    def __init__(
        self,
        **bindings: Callable[[], Any],
    ) -> None:
        self._bindings = MappingProxyType(dict(bindings))

    def __getattr__(self, name: str) -> Any:
        try:
            provider = self._bindings[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return provider()
