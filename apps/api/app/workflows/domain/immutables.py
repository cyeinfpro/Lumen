"""Immutable value constructors for declarative workflow policy data."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping


def freeze_value(value: Any) -> Any:
    """Recursively freeze declarative policy values without changing lookup APIs."""

    if isinstance(value, dict):
        return MappingProxyType(
            {key: freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_value(item) for item in value)
    return value


def freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expose a recursively immutable mapping for static workflow policies."""

    frozen = freeze_value(dict(value))
    assert isinstance(frozen, Mapping)
    return frozen
