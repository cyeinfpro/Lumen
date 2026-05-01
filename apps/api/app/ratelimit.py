"""简单令牌桶限流（Redis + Lua 原子）。

用法：
    rl = RateLimiter(capacity=20, refill_per_sec=20/60)
    await rl.check(redis, key=f"rl:msg:{user_id}")

超限抛 HTTPException 429，含 retry_after_ms。
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Literal

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis

from .config import settings

logger = logging.getLogger(__name__)


_DEV_ENVS = {"dev", "development", "local", "test"}


def _is_dev_env() -> bool:
    """dev / local / test 环境判断。"""
    env = getattr(settings, "app_env", "dev").strip().lower()
    return env in _DEV_ENVS


# KEYS[1] = bucket key
# ARGV[1] = capacity; ARGV[2] = refill_per_sec; ARGV[3] = now_ms; ARGV[4] = cost
# ARGV[5] = initial_tokens
# Stored as hash { tokens, ts }. Returns { allowed(0/1), retry_after_ms }.
_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local initial = tonumber(ARGV[5])
if initial == nil then
  initial = cost
end

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  -- Most buckets start with just enough for this request. High-fanout public
  -- reads can opt into a larger initial allowance.
  tokens = math.min(capacity, math.max(cost, initial))
  ts = now
end

local delta = (now - ts) / 1000.0
tokens = math.min(capacity, tokens + delta * refill)

local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  local needed = cost - tokens
  if refill > 0 then
    retry_ms = math.ceil((needed / refill) * 1000)
  else
    retry_ms = 1000
  end
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
-- TTL: ~ 2 * time to fully refill, min 60s
local ttl = math.max(60, math.ceil((capacity / math.max(refill, 0.001)) * 2))
redis.call('EXPIRE', key, ttl)

return {allowed, retry_ms}
"""


RateLimitScope = Literal["manual", "ip"]


class RateLimiter:
    def __init__(
        self,
        capacity: int,
        refill_per_sec: float,
        *,
        always_on: bool = False,
        key_prefix: str | None = None,
        scope: RateLimitScope = "manual",
        initial_tokens: int | None = None,
    ):
        if scope not in ("manual", "ip"):
            raise ValueError("rate-limit scope must be 'manual' or 'ip'")
        if initial_tokens is not None and initial_tokens < 1:
            raise ValueError("initial_tokens must be positive")
        self.capacity = capacity
        self.refill = refill_per_sec
        # always_on=True 的限流不受 settings.user_rate_limit_enabled 影响，
        # 用于公开未认证端点（invite/share 预览）防 brute force。
        self.always_on = always_on
        self.key_prefix = key_prefix
        self.scope = scope
        self.initial_tokens = initial_tokens

    async def __call__(self, request: Request) -> None:
        if self.key_prefix is None:
            raise RuntimeError(
                "key_prefix is required when RateLimiter is used as a dependency"
            )
        from .redis_client import get_redis

        await self.check(get_redis(), self.key_for_request(request))

    def key_for_request(self, request: Request) -> str:
        if self.key_prefix is None:
            raise RuntimeError(
                "key_prefix is required to build a request rate-limit key"
            )
        if self.scope == "ip":
            return f"{self.key_prefix}:{require_client_ip(request)}"
        raise RuntimeError("manual rate limiters require callers to pass an explicit key")

    async def check(self, redis: Redis, key: str, cost: int = 1) -> None:
        import time

        is_dev = _is_dev_env()
        if not self.always_on:
            # dev/local/test 仍可通过 USER_RATE_LIMIT_ENABLED 开关开启；
            # 生产环境默认强制开启（fail-closed），避免忘开等于无限流。
            if is_dev and not getattr(settings, "user_rate_limit_enabled", False):
                return

        now_ms = int(time.time() * 1000)
        try:
            res = await redis.eval(
                _LUA,
                1,
                key,
                str(self.capacity),
                str(self.refill),
                str(now_ms),
                str(cost),
                str(self.initial_tokens if self.initial_tokens is not None else cost),
            )
        except Exception as exc:
            # always_on 限流（公开端点防刷）总是 fail-closed，避免 dev 漏放被刷。
            # dev 下其他限流保留 fail-open 但 ERROR 级别日志方便定位。
            if is_dev and not self.always_on:
                logger.error(
                    "rate limiter redis failure (dev fail-open) key=%s err=%r",
                    key,
                    exc,
                )
                return
            logger.error(
                "rate limiter redis failure (fail-closed) key=%s err=%r",
                key,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": {
                        "code": "rate_limiter_unavailable",
                        "message": "rate limiter unavailable",
                    }
                },
                headers={"Retry-After": "1"},
            ) from exc
        allowed = int(res[0])
        retry_ms = int(res[1])
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": {
                        "code": "rate_limit",
                        "message": "rate limit exceeded",
                        "retry_after_ms": retry_ms,
                    }
                },
                headers={"Retry-After": str(max(1, retry_ms // 1000))},
            )


# Pre-configured limiters (per DESIGN §6.7 + task spec).
# messages POST: 20/min per user.
MESSAGES_LIMITER = RateLimiter(capacity=20, refill_per_sec=20 / 60)
# uploads: 30/min per user. Always on because upload accepts large request bodies.
UPLOADS_LIMITER = RateLimiter(capacity=30, refill_per_sec=30 / 60, always_on=True)
# Public unauthenticated reads (invite/share previews): 60/min per IP.
# always_on=True：公开端点的防刷始终启用，不受 user_rate_limit_enabled 开关影响。
PUBLIC_PREVIEW_LIMITER = RateLimiter(
    capacity=60, refill_per_sec=60 / 60, always_on=True
)
# Public binary image reads need a real first-page burst: a shared gallery can
# legitimately open 100 thumbnails at once, while metadata/invite lookups should
# remain much tighter.
PUBLIC_IMAGE_LIMITER = RateLimiter(
    capacity=240,
    refill_per_sec=240 / 60,
    always_on=True,
    initial_tokens=120,
)
# Auth endpoints are unauthenticated, so scope by client IP and keep active even
# in dev/test unless the dependency is bypassed by direct unit calls.
AUTH_LOGIN_LIMITER = RateLimiter(
    capacity=10,
    refill_per_sec=10 / 60,
    always_on=True,
    key_prefix="rl:auth:login",
    scope="ip",
)
AUTH_SIGNUP_LIMITER = RateLimiter(
    capacity=5,
    refill_per_sec=5 / 300,
    always_on=True,
    key_prefix="rl:auth:signup",
    scope="ip",
)


def client_ip(request) -> str:  # type: ignore[no-untyped-def]
    """Best-effort client IP for per-IP rate-limit keys.

    Honors one X-Forwarded-For hop only when the direct peer is trusted;
    otherwise falls back to the direct peer address.
    """
    remote = None
    if request and request.client and request.client.host:
        remote = request.client.host

    xff = request.headers.get("x-forwarded-for") if request else None
    if xff and remote and _is_trusted_proxy(remote):
        for token in reversed(xff.split(",")):
            parsed = _parse_ip_token(token)
            if parsed is None:
                continue
            if not _is_trusted_proxy(parsed):
                return parsed

    if remote:
        return remote
    return "unknown"


def require_client_ip(request) -> str:  # type: ignore[no-untyped-def]
    """Like client_ip but rejects anonymous requests (no peer / no XFF).

    Public unauthenticated endpoints must call this to avoid the "unknown"
    bucket — otherwise all clients without a discoverable IP share a single
    rate-limit token bucket and one attacker can lock out everyone else.
    """
    ip = client_ip(request)
    if ip == "unknown":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": {
                    "code": "client_ip_required",
                    "message": "client address could not be determined",
                }
            },
        )
    return ip


def _is_trusted_proxy(remote: str) -> bool:
    parsed_remote = _parse_ip_token(remote)
    if parsed_remote is None:
        return False

    raw = getattr(settings, "trusted_proxies", "")
    if not raw.strip():
        return False

    ip = ipaddress.ip_address(parsed_remote)
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            continue
        if ip in network:
            return True
    return False


def _parse_ip_token(token: str) -> str | None:
    value = token.strip().strip('"')
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1 and "." in value:
        value = value.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None
