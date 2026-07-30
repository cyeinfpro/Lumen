"""Redis quota, rate-limit, and operation state for Volcano assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from lumen_core.video_providers import VideoProviderDefinition

VOLCANO_ASSET_MAX_GROUPS = 50
VOLCANO_ASSET_MAX_ASSETS = 50
VOLCANO_ASSET_CREATE_QPM = 3
VOLCANO_ASSET_CREATE_WINDOW_SECONDS = 60
VOLCANO_ASSET_OPERATION_TTL_SECONDS = 7 * 24 * 60 * 60
VOLCANO_ASSET_RESERVATION_TTL_SECONDS = 45 * 60

_REDIS_RETRY_ATTEMPTS = 3
_REDIS_RETRY_BASE_DELAY_SECONDS = 0.02

_REDIS_QUOTA_RESERVE_SCRIPT = """
local key = KEYS[1]
local cutoff = tonumber(ARGV[1])
local upstream_total = tonumber(ARGV[2])
local hard_limit = tonumber(ARGV[3])
local score = tonumber(ARGV[4])
local member = ARGV[5]
local ttl = tonumber(ARGV[6])
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local existing = redis.call('ZSCORE', key, member)
local reservations = redis.call('ZCARD', key)
local other_reservations = reservations
if existing then
  other_reservations = math.max(0, reservations - 1)
end
if upstream_total + other_reservations >= hard_limit then
  return {0, other_reservations}
end
redis.call('ZADD', key, score, member)
redis.call('EXPIRE', key, ttl)
return {1, other_reservations}
"""
_REDIS_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local hard_limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
local existing = redis.call('ZSCORE', key, member)
if existing then
  redis.call('PEXPIRE', key, window_ms * 2)
  return {1, 0}
end
local count = redis.call('ZCARD', key)
if count >= hard_limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry_ms = window_ms
  if oldest[2] then
    retry_ms = math.max(1, tonumber(oldest[2]) + window_ms - now_ms)
  end
  return {0, retry_ms}
end
redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window_ms * 2)
return {1, 0}
"""
_REDIS_OPERATION_CAS_SCRIPT = """
local key = KEYS[1]
local owner = ARGV[1]
local expected_status = ARGV[2]
local expected_attempt = tonumber(ARGV[3])
local expected_progress = ARGV[4]
local replacement = ARGV[5]
local ttl = tonumber(ARGV[6])
local raw = redis.call('GET', key)
if not raw then
  return {0, ''}
end
local decoded_ok, current = pcall(cjson.decode, raw)
if not decoded_ok or type(current) ~= 'table' then
  return {-1, ''}
end
if tostring(current['user_id'] or '') ~= owner then
  return {-2, ''}
end
if tostring(current['status'] or '') ~= expected_status
    or tonumber(current['attempt'] or 1) ~= expected_attempt then
  return {2, raw}
end
if expected_progress ~= ''
    and tostring(current['progress_stage'] or '') ~= expected_progress then
  return {2, raw}
end
redis.call('SET', key, replacement, 'EX', ttl)
return {1, replacement}
"""


@dataclass(frozen=True)
class VolcanoAssetQuotaKey:
    provider_name: str
    project_name: str
    region: str


class VolcanoAssetQuotaExceeded(RuntimeError):
    def __init__(
        self,
        *,
        resource: str,
        limit: int,
        upstream_total: int,
        local_reservations: int,
    ) -> None:
        super().__init__(f"Volcano {resource} quota exceeded")
        self.resource = resource
        self.limit = limit
        self.upstream_total = upstream_total
        self.local_reservations = local_reservations


class VolcanoAssetCreateRateLimited(RuntimeError):
    def __init__(self, *, retry_after_ms: int) -> None:
        super().__init__("Volcano CreateAsset rate limited")
        self.retry_after_ms = retry_after_ms


class VolcanoAssetOperationOwnershipError(RuntimeError):
    """Raised when an atomic operation update targets another user's record."""


class VolcanoAssetRedisUnavailable(RuntimeError):
    """Raised after bounded retries cannot complete a Redis operation."""


def volcano_asset_quota_key(
    provider: VideoProviderDefinition,
) -> VolcanoAssetQuotaKey:
    return VolcanoAssetQuotaKey(
        provider_name=provider.name,
        project_name=provider.project_name,
        region=provider.region,
    )


def volcano_asset_quota_scope(key: VolcanoAssetQuotaKey) -> str:
    raw = "\x1f".join((key.provider_name, key.project_name, key.region))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def volcano_asset_operation_key(operation_id: str) -> str:
    return f"video-assets:operation:{operation_id}"


def volcano_asset_reservation_key(
    key: VolcanoAssetQuotaKey,
    *,
    resource: str,
) -> str:
    return (
        f"video-assets:quota:{volcano_asset_quota_scope(key)}:{resource}:reservations"
    )


def volcano_asset_rate_limit_key(
    key: VolcanoAssetQuotaKey,
    *,
    bucket: str,
) -> str:
    return f"video-assets:quota:{volcano_asset_quota_scope(key)}:create-asset:{bucket}"


def _redis_pair(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise RuntimeError("invalid Redis quota response")
    try:
        return int(value[0]), int(value[1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid Redis quota response") from exc


async def _retry_redis_call(
    call: Callable[[], Awaitable[Any]],
) -> Any:
    last_error: Exception | None = None
    for attempt in range(_REDIS_RETRY_ATTEMPTS):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 >= _REDIS_RETRY_ATTEMPTS:
                break
            base_delay = _REDIS_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            await asyncio.sleep(base_delay + random.uniform(0, base_delay))
    if last_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("Redis operation failed")
    raise VolcanoAssetRedisUnavailable(
        f"Volcano asset Redis operation failed ({type(last_error).__name__})"
    ) from None


async def reserve_volcano_asset_quota(
    redis: Any,
    key: VolcanoAssetQuotaKey,
    *,
    resource: str,
    operation_id: str,
    upstream_total: int,
    limit: int,
    now_ms: int,
) -> None:
    redis_key = volcano_asset_reservation_key(key, resource=resource)
    result = await _retry_redis_call(
        lambda: redis.eval(
            _REDIS_QUOTA_RESERVE_SCRIPT,
            1,
            redis_key,
            now_ms - VOLCANO_ASSET_RESERVATION_TTL_SECONDS * 1000,
            upstream_total,
            limit,
            now_ms,
            operation_id,
            VOLCANO_ASSET_RESERVATION_TTL_SECONDS,
        )
    )
    accepted, local_reservations = _redis_pair(result)
    if not accepted:
        raise VolcanoAssetQuotaExceeded(
            resource=resource,
            limit=limit,
            upstream_total=upstream_total,
            local_reservations=local_reservations,
        )


async def release_volcano_asset_quota(
    redis: Any,
    key: VolcanoAssetQuotaKey,
    *,
    resource: str,
    operation_id: str,
) -> None:
    await _retry_redis_call(
        lambda: redis.zrem(
            volcano_asset_reservation_key(key, resource=resource),
            operation_id,
        )
    )


async def acquire_volcano_create_rate_limit(
    redis: Any,
    key: VolcanoAssetQuotaKey,
    *,
    bucket: str,
    operation_id: str,
    now_ms: int,
) -> None:
    result = await _retry_redis_call(
        lambda: redis.eval(
            _REDIS_RATE_LIMIT_SCRIPT,
            1,
            volcano_asset_rate_limit_key(key, bucket=bucket),
            now_ms,
            VOLCANO_ASSET_CREATE_WINDOW_SECONDS * 1000,
            VOLCANO_ASSET_CREATE_QPM,
            operation_id,
        )
    )
    accepted, retry_after_ms = _redis_pair(result)
    if not accepted:
        raise VolcanoAssetCreateRateLimited(
            retry_after_ms=max(1, retry_after_ms),
        )


async def release_volcano_create_rate_limit(
    redis: Any,
    key: VolcanoAssetQuotaKey,
    *,
    bucket: str,
    operation_id: str,
) -> None:
    await _retry_redis_call(
        lambda: redis.zrem(
            volcano_asset_rate_limit_key(key, bucket=bucket),
            operation_id,
        )
    )


def _decode_operation_json(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def compare_and_set_volcano_asset_operation(
    redis: Any,
    operation_id: str,
    *,
    owner_user_id: str,
    expected_status: str,
    expected_attempt: int,
    replacement: dict[str, Any],
    expected_progress_stage: str | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    serialized = json.dumps(
        replacement,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = await _retry_redis_call(
        lambda: redis.eval(
            _REDIS_OPERATION_CAS_SCRIPT,
            1,
            volcano_asset_operation_key(operation_id),
            owner_user_id,
            expected_status,
            expected_attempt,
            expected_progress_stage or "",
            serialized,
            VOLCANO_ASSET_OPERATION_TTL_SECONDS,
        )
    )
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        raise RuntimeError("invalid Redis operation compare-and-set response")
    try:
        code = int(result[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid Redis operation compare-and-set response") from exc
    if code == -2:
        raise VolcanoAssetOperationOwnershipError("operation owner does not match")
    if code == -1:
        raise RuntimeError("stored Volcano asset operation is invalid")
    current = _decode_operation_json(result[1])
    return code == 1, current


__all__ = [
    "VOLCANO_ASSET_CREATE_QPM",
    "VOLCANO_ASSET_CREATE_WINDOW_SECONDS",
    "VOLCANO_ASSET_MAX_ASSETS",
    "VOLCANO_ASSET_MAX_GROUPS",
    "VOLCANO_ASSET_OPERATION_TTL_SECONDS",
    "VOLCANO_ASSET_RESERVATION_TTL_SECONDS",
    "VolcanoAssetCreateRateLimited",
    "VolcanoAssetOperationOwnershipError",
    "VolcanoAssetQuotaExceeded",
    "VolcanoAssetQuotaKey",
    "VolcanoAssetRedisUnavailable",
    "acquire_volcano_create_rate_limit",
    "compare_and_set_volcano_asset_operation",
    "release_volcano_asset_quota",
    "release_volcano_create_rate_limit",
    "reserve_volcano_asset_quota",
    "volcano_asset_operation_key",
    "volcano_asset_quota_key",
    "volcano_asset_quota_scope",
    "volcano_asset_rate_limit_key",
    "volcano_asset_reservation_key",
]
