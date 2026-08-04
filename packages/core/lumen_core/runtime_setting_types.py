"""Shared types for runtime setting metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SettingSpec:
    key: str
    description: str
    sensitive: bool
    parser: type
    env_fallback: str
    min_value: int | float | None = None
    max_value: int | float | None = None
    allowed_values: tuple[str, ...] | None = None
