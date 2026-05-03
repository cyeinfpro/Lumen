"""Local-cooldown fallback for proxy_pool when Redis writes fail."""
from __future__ import annotations

import pytest

from app import proxy_pool
from lumen_core.providers import ProviderProxyDefinition


def _make_proxy(name: str) -> ProviderProxyDefinition:
    return ProviderProxyDefinition(
        name=name,
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        enabled=True,
    )


@pytest.fixture(autouse=True)
def _clear_local_cooldown():
    proxy_pool._local_cooldown.clear()
    yield
    proxy_pool._local_cooldown.clear()


class _SetFailRedis:
    """Redis that lets INCR/EXPIRE pass but blows up on SET (cooldown write)."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.exists_calls: list[str] = []

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, ttl: int) -> None:
        return None

    async def set(self, *_args, **_kwargs) -> None:
        raise RuntimeError("redis SET unavailable")

    async def delete(self, *_args) -> None:
        return None

    async def exists(self, key: str) -> int:
        # Real Redis would have nothing because SET failed.
        self.exists_calls.append(key)
        return 0


class _AllFailRedis:
    """Redis that fails on every command (full outage)."""

    async def incr(self, *_a, **_kw):
        raise RuntimeError("redis down")

    async def expire(self, *_a, **_kw):
        raise RuntimeError("redis down")

    async def set(self, *_a, **_kw):
        raise RuntimeError("redis down")

    async def delete(self, *_a, **_kw):
        raise RuntimeError("redis down")

    async def exists(self, *_a, **_kw):
        raise RuntimeError("redis down")


@pytest.mark.asyncio
async def test_report_failure_marks_local_cooldown_when_set_fails() -> None:
    redis = _SetFailRedis()
    triggered = await proxy_pool.report_failure(
        redis, "p1", failure_threshold=1, cooldown_seconds=120
    )
    assert triggered is True
    assert proxy_pool._local_cooldown_active("p1") is True

    # _is_in_cooldown must honour local fallback even though the real Redis
    # SET never landed (so EXISTS would say "not in cooldown").
    in_cd = await proxy_pool._is_in_cooldown(redis, "p1")
    assert in_cd is True


@pytest.mark.asyncio
async def test_report_failure_marks_local_cooldown_when_redis_fully_down() -> None:
    redis = _AllFailRedis()
    await proxy_pool.report_failure(
        redis, "p2", failure_threshold=3, cooldown_seconds=60
    )
    assert proxy_pool._local_cooldown_active("p2") is True


@pytest.mark.asyncio
async def test_pick_proxy_skips_locally_cooled_proxy() -> None:
    redis = _SetFailRedis()
    a = _make_proxy("a")
    b = _make_proxy("b")
    await proxy_pool.report_failure(
        redis, "a", failure_threshold=1, cooldown_seconds=60
    )

    picked = await proxy_pool.pick_proxy(redis, [a, b], strategy="failover")
    assert picked is not None and picked.name == "b"


@pytest.mark.asyncio
async def test_report_success_clears_local_cooldown() -> None:
    redis = _SetFailRedis()
    await proxy_pool.report_failure(
        redis, "p3", failure_threshold=1, cooldown_seconds=60
    )
    assert proxy_pool._local_cooldown_active("p3") is True

    await proxy_pool.report_success(redis, "p3")
    assert proxy_pool._local_cooldown_active("p3") is False


@pytest.mark.asyncio
async def test_local_cooldown_capped_to_short_window() -> None:
    """Even if Redis cooldown is hours long, local fallback stays short so a
    process restart / Redis recovery doesn't carry a stale local block."""
    redis = _SetFailRedis()
    await proxy_pool.report_failure(
        redis, "p4", failure_threshold=1, cooldown_seconds=3600
    )
    expiry = proxy_pool._local_cooldown.get("p4")
    assert expiry is not None
    import time as _t
    remaining = expiry - _t.monotonic()
    assert remaining <= proxy_pool._LOCAL_COOLDOWN_TTL_SECONDS + 1
