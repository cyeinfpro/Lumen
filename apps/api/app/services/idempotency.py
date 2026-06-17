"""Redis-backed idempotency helpers for admin/system operations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from ..redis_client import get_redis


def derive_idempotency_key(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"derived:{digest}"


def _jsonable(value: Any) -> Any:
    """Convert nested values to JSON primitives accepted by Redis cache.

    Pydantic models can appear nested inside dictionaries (for example billing
    idempotency responses). Calling ``json.dumps`` on the outer dict would raise
    ``TypeError`` and disable the cache silently, so normalise recursively.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(inner) for key, inner in value.items()}
    if isinstance(value, (str, bytes, bytearray)):
        if isinstance(value, str):
            return value
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Sequence):
        return [_jsonable(inner) for inner in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _dump(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def get_cached_json(namespace: str, key: str) -> dict[str, Any] | None:
    try:
        raw = await get_redis().get(f"{namespace}:{key}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def cache_json(namespace: str, key: str, value: Any, ttl_sec: int) -> None:
    try:
        await get_redis().set(f"{namespace}:{key}", _dump(value), ex=ttl_sec)
    except Exception:
        return
