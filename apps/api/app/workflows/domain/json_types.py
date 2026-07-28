"""JSON-compatible workflow values."""

from __future__ import annotations

from typing import TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


__all__ = ["JsonObject", "JsonScalar", "JsonValue"]
