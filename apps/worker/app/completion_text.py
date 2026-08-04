"""Shared completion text truth semantics."""

from __future__ import annotations

from typing import Any


def completion_text_or_empty(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


__all__ = ["completion_text_or_empty"]
