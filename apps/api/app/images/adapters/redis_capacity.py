from __future__ import annotations

import inspect
import logging
import uuid
from typing import Any

from ..domain.resource_estimate import ImageResourceEstimate
from .local_capacity import (
    CapacityExceeded,
    CapacityLimits,
    CapacityUnavailable,
    ScaledLocalCapacity,
)


logger = logging.getLogger(__name__)


_RESERVE_LUA = """
local leases = KEYS[1]
local weights = KEYS[2]
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local lease_id = ARGV[1]
local weight = tonumber(ARGV[2])
local max_count = tonumber(ARGV[3])
local max_weight = tonumber(ARGV[4])
local ttl_ms = tonumber(ARGV[5])
local expires_ms = now_ms + ttl_ms

local expired = redis.call('ZRANGEBYSCORE', leases, '-inf', now_ms)
if #expired > 0 then
  redis.call('ZREM', leases, unpack(expired))
  redis.call('HDEL', weights, unpack(expired))
end

local current_count = redis.call('ZCARD', leases)
local current_weight = 0
local values = redis.call('HVALS', weights)
for _, value in ipairs(values) do
  current_weight = current_weight + tonumber(value)
end

if current_count >= max_count or current_weight + weight > max_weight then
  return {0, current_count, current_weight}
end

redis.call('ZADD', leases, expires_ms, lease_id)
redis.call('HSET', weights, lease_id, weight)
local ttl = math.max(60, math.ceil(ttl_ms / 1000) * 2)
redis.call('EXPIRE', leases, ttl)
redis.call('EXPIRE', weights, ttl)
return {1, current_count + 1, current_weight + weight}
"""

_RENEW_LUA = """
if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 0 then
  return 0
end
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local ttl_ms = tonumber(ARGV[2])
redis.call('ZADD', KEYS[1], now_ms + ttl_ms, ARGV[1])
local ttl = math.max(60, math.ceil(ttl_ms / 1000) * 2)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""

_RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
"""


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class _RedisCapacityLease:
    def __init__(
        self,
        capacity: "RedisCapacity",
        lease_id: str,
    ) -> None:
        self._capacity = capacity
        self.lease_id = lease_id
        self._released = False

    async def renew(self) -> bool:
        if self._released:
            return False
        return await self._capacity._renew(self.lease_id)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._capacity._release(self.lease_id)


class RedisCapacity:
    def __init__(
        self,
        redis: Any,
        limits: CapacityLimits,
        *,
        namespace: str = "lumen:image-upload:capacity",
    ) -> None:
        self.redis = redis
        self.limits = limits
        self.leases_key = f"{namespace}:leases"
        self.weights_key = f"{namespace}:weights"

    async def reserve(
        self,
        estimate: ImageResourceEstimate,
    ) -> _RedisCapacityLease:
        lease_id = uuid.uuid4().hex
        result = await _resolve(
            self.redis.eval(
                _RESERVE_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
                str(estimate.peak_bytes),
                str(self.limits.max_concurrency),
                str(self.limits.max_peak_bytes),
                str(self.limits.lease_ttl_seconds * 1000),
            )
        )
        if int(result[0]) != 1:
            raise CapacityExceeded("image upload capacity exhausted")
        return _RedisCapacityLease(self, lease_id)

    async def _renew(self, lease_id: str) -> bool:
        result = await _resolve(
            self.redis.eval(
                _RENEW_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
                str(self.limits.lease_ttl_seconds * 1000),
            )
        )
        return int(result) == 1

    async def _release(self, lease_id: str) -> None:
        await _resolve(
            self.redis.eval(
                _RELEASE_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
            )
        )


class ResilientCapacity:
    def __init__(
        self,
        primary: RedisCapacity,
        fallback: ScaledLocalCapacity,
        *,
        degraded_policy: str,
    ) -> None:
        if degraded_policy not in {"fail_closed", "scaled_local"}:
            raise ValueError("invalid image upload degraded capacity policy")
        self.primary = primary
        self.fallback = fallback
        self.degraded_policy = degraded_policy

    async def reserve(self, estimate: ImageResourceEstimate) -> Any:
        try:
            return await self.primary.reserve(estimate)
        except CapacityExceeded:
            raise
        except Exception as exc:
            if self.degraded_policy != "scaled_local":
                raise CapacityUnavailable("image upload capacity unavailable") from exc
            logger.warning(
                "image upload capacity using scaled local fallback error=%r",
                exc,
            )
            return await self.fallback.reserve(estimate)


class _LayeredCapacityLease:
    def __init__(self, global_lease: Any, process_lease: Any) -> None:
        self.global_lease = global_lease
        self.process_lease = process_lease
        self._released = False

    async def renew(self) -> bool:
        global_ok = await self.global_lease.renew()
        process_ok = await self.process_lease.renew()
        return global_ok and process_ok

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self.process_lease.release()
        finally:
            await self.global_lease.release()


class LayeredCapacity:
    def __init__(self, global_capacity: ResilientCapacity, process_guard: Any) -> None:
        self.global_capacity = global_capacity
        self.process_guard = process_guard

    async def reserve(self, estimate: ImageResourceEstimate) -> _LayeredCapacityLease:
        global_lease = await self.global_capacity.reserve(estimate)
        try:
            process_lease = await self.process_guard.reserve(estimate)
        except Exception:
            await global_lease.release()
            raise
        return _LayeredCapacityLease(global_lease, process_lease)


def build_capacity(redis: Any) -> LayeredCapacity:
    limits = CapacityLimits.from_env()
    from ...config import settings

    configured_policy = settings.image_upload_capacity_degraded_policy.strip()
    if configured_policy:
        policy = configured_policy
    else:
        policy = (
            "scaled_local"
            if settings.app_env.strip().lower()
            in {"dev", "development", "local", "test"}
            else "fail_closed"
        )
    # The Redis lease is the cluster-wide budget. Its degraded fallback must be
    # divided across API workers, while the normal process guard must retain the
    # full per-process ceiling: dividing both layers made one legal large upload
    # impossible whenever Uvicorn used more than one worker.
    degraded_fallback = ScaledLocalCapacity(limits)
    process_guard = ScaledLocalCapacity(limits, process_count=1)
    return LayeredCapacity(
        ResilientCapacity(
            RedisCapacity(redis, limits),
            degraded_fallback,
            degraded_policy=policy,
        ),
        process_guard,
    )
