"""Shared persistence types and serialization helpers."""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any


DbExec = Callable[[str, tuple[Any, ...]], Awaitable[int]]
DbAll = Callable[[str, tuple[Any, ...]], Awaitable[list[sqlite3.Row]]]
EnqueueJob = Callable[[str], Awaitable[str]]


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return parsed


def strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        parse_constant=reject_json_constant,
        parse_float=parse_finite_float,
    )


def parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def initial_retention_expiry(created_at: str, retention_days: Any) -> str:
    created = parse_utc_datetime(created_at)
    if created is None:
        raise ValueError("created_at must be a valid datetime")
    try:
        days = max(1, int(retention_days))
    except (TypeError, ValueError):
        days = 1
    return (created + timedelta(days=days)).isoformat()


def terminal_retention_expiry(
    finished_at: str,
    *,
    job_ttl_days: int,
    images: list[dict[str, Any]] | None = None,
) -> str:
    finished = parse_utc_datetime(finished_at)
    if finished is None:
        raise ValueError("finished_at must be a valid datetime")
    expiries = [finished + timedelta(days=max(1, int(job_ttl_days)))]
    for image in images or ():
        expires_at = parse_utc_datetime(image.get("expires_at"))
        if expires_at is not None:
            expiries.append(expires_at)
    return min(expiries).isoformat()
