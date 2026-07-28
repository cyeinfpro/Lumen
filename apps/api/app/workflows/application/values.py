"""Small transport- and infrastructure-neutral value helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def dedupe_nonempty(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip() if value is not None else ""
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["dedupe_nonempty", "dict_or_empty"]
