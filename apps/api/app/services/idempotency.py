"""Redis-backed idempotency helpers for admin/system operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from ..redis_client import get_redis


def derive_idempotency_key(*parts: object) -> str:
    encoded = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"derived:{digest}"


def _dump(value: Any) -> str:
    if isinstance(value, BaseModel):
        data = value.model_dump(mode="json")
    else:
        data = value
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
