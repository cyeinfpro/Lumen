from __future__ import annotations

from fastapi import Request
import pytest

from app.ratelimit import AUTH_LOGIN_LIMITER, AUTH_SIGNUP_LIMITER
from app.routes import auth


class CountingRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def eval(self, _lua: str, _keys: int, key: str, capacity: str, *_args):
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] <= int(capacity):
            return [1, 0]
        return [0, 1000]


def _request(ip: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [],
            "client": (ip, 12345),
        }
    )


def _route_dependencies(path: str):
    route = next(r for r in auth.router.routes if getattr(r, "path", None) == path)
    return [dep.dependency for dep in route.dependencies]


def test_auth_routes_mount_ip_limiters() -> None:
    assert AUTH_LOGIN_LIMITER in _route_dependencies("/login")
    assert AUTH_SIGNUP_LIMITER in _route_dependencies("/signup")
    assert AUTH_LOGIN_LIMITER.always_on is True
    assert AUTH_LOGIN_LIMITER.scope == "ip"
    assert AUTH_SIGNUP_LIMITER.always_on is True
    assert AUTH_SIGNUP_LIMITER.scope == "ip"


@pytest.mark.asyncio
async def test_login_rate_limit_blocks_after_10_attempts() -> None:
    redis = CountingRedis()
    key = AUTH_LOGIN_LIMITER.key_for_request(_request("203.0.113.10"))

    for _ in range(10):
        await AUTH_LOGIN_LIMITER.check(redis, key)  # type: ignore[arg-type]

    with pytest.raises(Exception) as excinfo:
        await AUTH_LOGIN_LIMITER.check(redis, key)  # type: ignore[arg-type]

    assert getattr(excinfo.value, "status_code", None) == 429


@pytest.mark.asyncio
async def test_signup_rate_limit_blocks_after_5_attempts() -> None:
    redis = CountingRedis()
    key = AUTH_SIGNUP_LIMITER.key_for_request(_request("203.0.113.11"))

    for _ in range(5):
        await AUTH_SIGNUP_LIMITER.check(redis, key)  # type: ignore[arg-type]

    with pytest.raises(Exception) as excinfo:
        await AUTH_SIGNUP_LIMITER.check(redis, key)  # type: ignore[arg-type]

    assert getattr(excinfo.value, "status_code", None) == 429


@pytest.mark.asyncio
async def test_login_rate_limit_per_ip_not_global() -> None:
    redis = CountingRedis()
    key_a = AUTH_LOGIN_LIMITER.key_for_request(_request("203.0.113.12"))
    key_b = AUTH_LOGIN_LIMITER.key_for_request(_request("203.0.113.13"))

    for _ in range(10):
        await AUTH_LOGIN_LIMITER.check(redis, key_a)  # type: ignore[arg-type]
    await AUTH_LOGIN_LIMITER.check(redis, key_b)  # type: ignore[arg-type]

    with pytest.raises(Exception) as excinfo:
        await AUTH_LOGIN_LIMITER.check(redis, key_a)  # type: ignore[arg-type]

    assert getattr(excinfo.value, "status_code", None) == 429
    assert redis.counts[key_b] == 1
