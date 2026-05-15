"""Redis system-operation lock with file-marker fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from ..redis_client import get_redis


@dataclass(frozen=True)
class SystemLock:
    operation: str
    owner: str
    token: str
    degraded: bool = False


class LockBusy(RuntimeError):
    pass


class SystemOperationLockService:
    def __init__(
        self,
        *,
        key: str = "lumen:update:lock",
        fallback_busy: Callable[[], bool] | None = None,
    ) -> None:
        self.key = key
        self.fallback_busy = fallback_busy

    async def acquire(
        self,
        *,
        operation: str,
        owner: str,
        ttl_sec: int = 1800,
    ) -> SystemLock:
        token = f"{owner}:{operation}:{datetime.now(timezone.utc).isoformat()}"
        try:
            ok = await get_redis().set(self.key, token, nx=True, ex=ttl_sec)
        except Exception:
            if self.fallback_busy is not None and self.fallback_busy():
                raise LockBusy("system operation already running")
            return SystemLock(operation=operation, owner=owner, token=token, degraded=True)
        if not ok:
            raise LockBusy("system operation already running")
        return SystemLock(operation=operation, owner=owner, token=token)

    async def release(
        self,
        lock: SystemLock,
        *,
        succeeded: bool = True,
        reason: str | None = None,
    ) -> None:
        if lock.degraded:
            return
        script = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""
        try:
            await get_redis().eval(script, 1, self.key, lock.token)
        except Exception:
            return

    async def heartbeat(self, lock: SystemLock, *, ttl_sec: int = 1800) -> bool:
        if lock.degraded:
            return False
        script = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""
        try:
            return bool(await get_redis().eval(script, 1, self.key, lock.token, ttl_sec))
        except Exception:
            return False

    async def with_lock(
        self,
        *,
        operation: str,
        owner: str,
        ttl_sec: int,
        fn: Callable[[SystemLock], Awaitable[object]],
    ) -> object:
        lock = await self.acquire(operation=operation, owner=owner, ttl_sec=ttl_sec)
        try:
            result = await fn(lock)
        except Exception:
            await self.release(lock, succeeded=False, reason="exception")
            raise
        await self.release(lock, succeeded=True, reason="success")
        return result
