"""Small constructors for immutable module-level lookup tables."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TypeVar


_K = TypeVar("_K")
_V = TypeVar("_V")
_InnerK = TypeVar("_InnerK")


def immutable_mapping(values: Mapping[_K, _V]) -> Mapping[_K, _V]:
    return MappingProxyType(dict(values))


def immutable_nested_mapping(
    values: Mapping[_K, Mapping[_InnerK, _V]],
) -> Mapping[_K, Mapping[_InnerK, _V]]:
    return MappingProxyType(
        {key: MappingProxyType(dict(inner)) for key, inner in values.items()}
    )


__all__ = ["immutable_mapping", "immutable_nested_mapping"]
