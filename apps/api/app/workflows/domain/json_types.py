"""JSON-compatible workflow values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
JsonMapping: TypeAlias = Mapping[str, JsonValue]


__all__ = ["JsonMapping", "JsonObject", "JsonScalar", "JsonValue"]
