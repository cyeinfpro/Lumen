"""Shared utility functions used across Lumen services."""

from __future__ import annotations

from datetime import datetime, timezone


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC.

    Naive datetimes are assumed to be UTC (common with some DB drivers).
    Aware datetimes are converted to UTC regardless of their source timezone.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
