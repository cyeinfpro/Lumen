"""Best-effort warm-pull trigger for the next update target."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .system_lock import SystemLock
from ..config import settings
from ..redis_client import get_redis


async def maybe_warm_pull(tag: str, lock: SystemLock | None = None) -> bool:
    marker_key = f"lumen:update:warm:{tag}"
    if lock is not None and lock.degraded:
        return False
    redis: Any | None = None
    try:
        redis = get_redis()
        if not await redis.set(marker_key, "1", nx=True, ex=1800):
            return False
    except Exception:  # noqa: BLE001
        redis = None
    try:
        path = Path(settings.backup_root) / ".warm.trigger"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        tmp.write_text(
            f"{tag}\n{datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except OSError:
        if redis is not None:
            try:
                await redis.delete(marker_key)
            except Exception:  # noqa: BLE001
                pass
        return False
