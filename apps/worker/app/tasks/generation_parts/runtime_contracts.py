from __future__ import annotations

GENERATION_LEASE_TTL_S = 60
GENERATION_LEASE_RENEW_S = 30
GENERATION_RUN_TIMEOUT_S = 1500.0

RELEASE_GENERATION_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
