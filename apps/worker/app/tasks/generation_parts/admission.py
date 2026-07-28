"""Atomic weighted resource permits for image-generation execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_core.generation_resources import ResourceDemand


RESOURCE_PERMITS_KEY = "generation:image_resources:permits"
RESOURCE_EXPIRY_KEY = "generation:image_resources:expiry"
RESOURCE_USED_KEY = "generation:image_resources:used"
RESOURCE_EXTERNAL_USED_KEY = "generation:image_resources:external_used"
RESOURCE_USER_USED_KEY = "generation:image_resources:user_used"

RESERVE_WEIGHTED_PERMIT_LUA = """
if redis.call('GET', KEYS[6]) ~= ARGV[1] then
  return -1
end
local now = tonumber(ARGV[2])
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, task_id in ipairs(expired) do
  local payload = redis.call('HGET', KEYS[1], task_id)
  if payload then
    local total = tonumber(string.match(payload, '^(%d+)|')) or 0
    local external = tonumber(string.match(payload, '^%d+|(%d+)|')) or 0
    local user_id = string.match(payload, '^%d+|%d+|%d+|%d+|([^|]+)|')
    redis.call('DECRBY', KEYS[3], total)
    redis.call('DECRBY', KEYS[4], external)
    if user_id then
      local user_total = redis.call('HINCRBY', KEYS[5], user_id, -total)
      if user_total <= 0 then
        redis.call('HDEL', KEYS[5], user_id)
      end
    end
  end
  redis.call('HDEL', KEYS[1], task_id)
  redis.call('ZREM', KEYS[2], task_id)
end

if redis.call('HEXISTS', KEYS[1], ARGV[3]) == 1 then
  return 0
end
local total = tonumber(ARGV[7])
local external = tonumber(ARGV[8])
local global_used = tonumber(redis.call('GET', KEYS[3]) or '0')
local external_used = tonumber(redis.call('GET', KEYS[4]) or '0')
local user_used = tonumber(redis.call('HGET', KEYS[5], ARGV[4]) or '0')
if global_used + total > tonumber(ARGV[9])
or external_used + external > tonumber(ARGV[10])
or user_used + total > tonumber(ARGV[11]) then
  return 0
end
redis.call('SET', KEYS[3], tostring(global_used + total))
redis.call('SET', KEYS[4], tostring(external_used + external))
redis.call('HINCRBY', KEYS[5], ARGV[4], total)
redis.call(
  'HSET',
  KEYS[1],
  ARGV[3],
  ARGV[12] .. '|' .. tostring(total) .. '|' .. tostring(external) .. '|' .. ARGV[4] .. '|' .. ARGV[13]
)
redis.call('ZADD', KEYS[2], tonumber(ARGV[14]), ARGV[3])
return 1
"""

RELEASE_WEIGHTED_PERMIT_LUA = """
local payload = redis.call('HGET', KEYS[1], ARGV[1])
if not payload then
  return 0
end
local prefix = ARGV[2] .. '|' .. ARGV[3] .. '|'
if string.sub(payload, 1, string.len(prefix)) ~= prefix then
  return 0
end
local total = tonumber(string.match(payload, '^%d+|%d+|(%d+)|')) or 0
local external = tonumber(string.match(payload, '^%d+|%d+|%d+|(%d+)|')) or 0
local user_id = string.match(payload, '^%d+|%d+|%d+|%d+|([^|]+)|')
redis.call('DECRBY', KEYS[3], total)
redis.call('DECRBY', KEYS[4], external)
if user_id then
  local user_total = redis.call('HINCRBY', KEYS[5], user_id, -total)
  if user_total <= 0 then
    redis.call('HDEL', KEYS[5], user_id)
  end
end
redis.call('HDEL', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
return 1
"""

RENEW_WEIGHTED_PERMIT_LUA = """
local payload = redis.call('HGET', KEYS[1], ARGV[1])
if not payload then
  return 0
end
local prefix = ARGV[2] .. '|' .. ARGV[3] .. '|'
if string.sub(payload, 1, string.len(prefix)) ~= prefix then
  return 0
end
redis.call('ZADD', KEYS[2], tonumber(ARGV[4]), ARGV[1])
return 1
"""


@dataclass(frozen=True, slots=True)
class WeightedPermit:
    task_id: str
    attempt: int
    revision: int
    demand: ResourceDemand
    user_id: str


async def reserve_weighted_permit(
    redis: Any,
    *,
    permit: WeightedPermit,
    owner: str,
    now: float,
    expiry: float,
    global_budget: int,
    external_budget: int,
    user_budget: int,
    lock_key: str,
) -> bool:
    result = await redis.eval(
        RESERVE_WEIGHTED_PERMIT_LUA,
        6,
        RESOURCE_PERMITS_KEY,
        RESOURCE_EXPIRY_KEY,
        RESOURCE_USED_KEY,
        RESOURCE_EXTERNAL_USED_KEY,
        RESOURCE_USER_USED_KEY,
        lock_key,
        owner,
        str(now),
        permit.task_id,
        permit.user_id,
        str(permit.attempt),
        str(permit.revision),
        str(permit.demand.total),
        str(permit.demand.external_lane_units),
        str(global_budget),
        str(external_budget),
        str(user_budget),
        f"{permit.attempt}|{permit.revision}",
        owner,
        str(expiry),
    )
    return int(result or 0) == 1


async def release_weighted_permit(
    redis: Any,
    *,
    permit: WeightedPermit,
) -> bool:
    result = await redis.eval(
        RELEASE_WEIGHTED_PERMIT_LUA,
        5,
        RESOURCE_PERMITS_KEY,
        RESOURCE_EXPIRY_KEY,
        RESOURCE_USED_KEY,
        RESOURCE_EXTERNAL_USED_KEY,
        RESOURCE_USER_USED_KEY,
        permit.task_id,
        str(permit.attempt),
        str(permit.revision),
    )
    return int(result or 0) == 1


async def renew_weighted_permit(
    redis: Any,
    *,
    permit: WeightedPermit,
    expiry: float,
) -> bool:
    result = await redis.eval(
        RENEW_WEIGHTED_PERMIT_LUA,
        2,
        RESOURCE_PERMITS_KEY,
        RESOURCE_EXPIRY_KEY,
        permit.task_id,
        str(permit.attempt),
        str(permit.revision),
        str(expiry),
    )
    return int(result or 0) == 1


__all__ = [
    "RESOURCE_EXPIRY_KEY",
    "RESOURCE_EXTERNAL_USED_KEY",
    "RESOURCE_PERMITS_KEY",
    "RESOURCE_USED_KEY",
    "RESOURCE_USER_USED_KEY",
    "RELEASE_WEIGHTED_PERMIT_LUA",
    "RENEW_WEIGHTED_PERMIT_LUA",
    "RESERVE_WEIGHTED_PERMIT_LUA",
    "WeightedPermit",
    "release_weighted_permit",
    "renew_weighted_permit",
    "reserve_weighted_permit",
]
