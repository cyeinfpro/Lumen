from __future__ import annotations

import inspect
import secrets
from dataclasses import dataclass
from typing import Any


_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class VariantLockLease:
    key: str
    token: str
    coordinated: bool


class RedisVariantLocks:
    def __init__(self, redis: Any, *, prefix: str = "image_variant_lock") -> None:
        self.redis = redis
        self.prefix = prefix

    def key(self, image_id: str, kind: str) -> str:
        return f"{self.prefix}:{image_id}:{kind}"

    async def acquire(
        self,
        image_id: str,
        kind: str,
        *,
        ttl_seconds: int,
    ) -> VariantLockLease | None:
        key = self.key(image_id, kind)
        token = secrets.token_urlsafe(24)
        try:
            acquired = await _resolve(
                self.redis.set(
                    key,
                    token,
                    nx=True,
                    ex=ttl_seconds,
                )
            )
        except Exception:
            return VariantLockLease(key=key, token=token, coordinated=False)
        if not acquired:
            return None
        return VariantLockLease(key=key, token=token, coordinated=True)

    async def release(self, lease: VariantLockLease) -> bool:
        if not lease.coordinated:
            return True
        result = await _resolve(
            self.redis.eval(
                _RELEASE_LUA,
                1,
                lease.key,
                lease.token,
            )
        )
        return int(result) == 1
