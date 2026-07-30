"""Late-bound access to the model-library operation compatibility surface."""

from __future__ import annotations

from types import ModuleType
from typing import Any


class ModelLibraryRuntimeAdapter:
    """Resolve facade-owned dependencies at call time for monkeypatch compatibility."""

    def __init__(
        self,
        bindings: ModuleType,
        *,
        required_bindings: tuple[object, ...] = (),
    ) -> None:
        self._bindings = bindings
        self._required_bindings = required_bindings

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bindings, name)
