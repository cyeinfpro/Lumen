"""Pricing resolver cache invalidation helpers."""

from __future__ import annotations

from ..redis_client import get_redis


async def invalidate_pricing_cache(model: str, variant: str) -> None:
    try:
        redis = get_redis()
        if any(token in model for token in ("*", "?")):
            keys = [key async for key in redis.scan_iter(match="lumen:pricing:v1:*")]
            if keys:
                await redis.delete(*keys)
            return
        await redis.delete(
            f"lumen:pricing:v1:{variant}:{model}",
            f"lumen:pricing:v1:default:{model}",
        )
    except Exception:  # noqa: BLE001
        return
