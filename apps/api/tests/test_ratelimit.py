from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.ratelimit import MESSAGES_LIMITER, RateLimiter, client_ip


class OkRedis:
    def __init__(self) -> None:
        self.called = False

    async def eval(self, *_args) -> list[int]:
        self.called = True
        return [1, 0]


class BadRedis:
    async def eval(self, *_args) -> list[int]:
        raise RuntimeError("redis down")


class CaptureRedis:
    def __init__(self) -> None:
        self.args = None

    async def eval(self, *args) -> list[int]:
        self.args = args
        return [1, 0]


@pytest.mark.asyncio
async def test_messages_limiter_runs_in_production_when_flag_false() -> None:
    old_env = settings.app_env
    old_enabled = settings.user_rate_limit_enabled
    settings.app_env = "production"
    settings.user_rate_limit_enabled = False
    redis = OkRedis()
    try:
        await MESSAGES_LIMITER.check(redis, "rl:test")
    finally:
        settings.app_env = old_env
        settings.user_rate_limit_enabled = old_enabled
    assert redis.called is True


@pytest.mark.asyncio
async def test_rate_limiter_fails_closed_when_redis_unavailable() -> None:
    limiter = RateLimiter(capacity=1, refill_per_sec=1, always_on=True)
    with pytest.raises(Exception) as excinfo:
        await limiter.check(BadRedis(), "rl:test")
    assert getattr(excinfo.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_rate_limiter_passes_initial_tokens_to_lua() -> None:
    limiter = RateLimiter(
        capacity=240,
        refill_per_sec=4,
        always_on=True,
        initial_tokens=120,
    )
    redis = CaptureRedis()

    await limiter.check(redis, "rl:test")

    assert redis.args is not None
    assert redis.args[-1] == "120"


def test_client_ip_ignores_xff_without_trusted_proxy() -> None:
    old = settings.trusted_proxies
    settings.trusted_proxies = ""
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.10"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    try:
        assert client_ip(request) == "127.0.0.1"
    finally:
        settings.trusted_proxies = old


def test_client_ip_uses_single_xff_hop_for_trusted_proxy() -> None:
    old = settings.trusted_proxies
    settings.trusted_proxies = "127.0.0.1/32"
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.10"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    try:
        assert client_ip(request) == "198.51.100.10"
    finally:
        settings.trusted_proxies = old


def test_client_ip_skips_trusted_proxy_chain_from_right() -> None:
    old = settings.trusted_proxies
    settings.trusted_proxies = "127.0.0.1/32, 10.0.0.0/8"
    request = SimpleNamespace(
        headers={"x-forwarded-for": "198.51.100.10, 10.0.0.10"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    try:
        assert client_ip(request) == "198.51.100.10"
    finally:
        settings.trusted_proxies = old
