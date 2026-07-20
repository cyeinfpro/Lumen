"""Late-bound runtime dependencies for Volcano asset task modules."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class VolcanoAssetRuntimeContext:
    """Resolve facade dependencies at call time to preserve monkeypatch behavior."""

    resolve: Callable[[str], Any]

    def bind(
        self,
        owner: str,
        dependencies: Iterable[str],
    ) -> VolcanoAssetRuntimeView:
        return VolcanoAssetRuntimeView(
            owner=owner,
            dependencies=frozenset(dependencies),
            resolve=self.resolve,
        )


@dataclass(frozen=True, slots=True)
class VolcanoAssetRuntimeView:
    """Restricted view of the operations exposed by the task facade."""

    owner: str
    dependencies: frozenset[str]
    resolve: Callable[[str], Any]

    def __getattr__(self, name: str) -> Any:
        if name not in self.dependencies:
            raise AttributeError(
                f"{self.owner} does not declare runtime dependency {name!r}"
            )
        try:
            return self.resolve(name)
        except KeyError as exc:
            raise AttributeError(
                f"Volcano asset runtime dependency {name!r} is unavailable"
            ) from exc


@dataclass(slots=True)
class VolcanoAssetRuntimeSlot:
    """Installable runtime view owned by one leaf task module."""

    owner: str
    dependencies: frozenset[str]
    _view: VolcanoAssetRuntimeView | None = None

    def install(self, context: VolcanoAssetRuntimeContext) -> None:
        self._view = context.bind(self.owner, self.dependencies)

    def get(self) -> VolcanoAssetRuntimeView:
        if self._view is None:
            raise RuntimeError(
                f"Volcano asset runtime is not installed for {self.owner}"
            )
        return self._view


__all__ = [
    "VolcanoAssetRuntimeContext",
    "VolcanoAssetRuntimeSlot",
    "VolcanoAssetRuntimeView",
]
