"""Projection helpers for persisted message content."""

from __future__ import annotations

from typing import Any


def public_message_content(content: Any) -> dict[str, Any]:
    """Project persisted message JSON onto the user-visible content contract."""

    if not isinstance(content, dict):
        return {}
    return {
        key: value
        for key, value in content.items()
        if not (isinstance(key, str) and key.startswith("_"))
    }


__all__ = ["public_message_content"]
