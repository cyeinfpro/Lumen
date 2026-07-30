"""Late-bound access to the authentication route compatibility surface."""

from __future__ import annotations

from types import ModuleType
from typing import Any


class AuthRuntimeAdapter:
    """Resolve facade-owned dependencies at call time for monkeypatch compatibility."""

    def __init__(self, bindings: ModuleType) -> None:
        self._bindings = bindings

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bindings, name)
