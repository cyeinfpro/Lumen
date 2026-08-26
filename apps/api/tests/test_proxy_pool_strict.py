from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app import proxy_pool
from app.routes import telegram
from lumen_core.providers import ProviderProxyDefinition


def _proxy(name: str) -> ProviderProxyDefinition:
    return ProviderProxyDefinition(
        name=name,
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        enabled=True,
    )


@pytest.fixture(autouse=True)
def _clear_local_cooldowns() -> None:
    proxy_pool._proxy_pool_state.local_cooldown.clear()


class _Redis:
    def __init__(
        self,
        *,
        cooled: set[str] | None = None,
        unknown: set[str] | None = None,
    ) -> None:
        self.cooled = cooled or set()
        self.unknown = unknown or set()

    async def exists(self, key: str) -> int:
        name = key.rsplit(":", 1)[-1]
        if name in self.unknown:
            raise RedisConnectionError("redis down")
        return int(name in self.cooled)

    async def incr(self, _key: str) -> int:
        return 1

    async def hgetall(self, _key: str) -> dict[bytes, bytes]:
        return {}


@pytest.mark.asyncio
async def test_all_cooled_returns_none() -> None:
    redis = _Redis(cooled={"a", "b"})

    assert await proxy_pool.pick_proxy(redis, [_proxy("a"), _proxy("b")]) is None


@pytest.mark.asyncio
async def test_all_cooldown_reads_unknown_raise() -> None:
    redis = _Redis(unknown={"a", "b"})

    with pytest.raises(proxy_pool.ProxyStateUnavailable) as exc_info:
        await proxy_pool.pick_proxy(redis, [_proxy("a"), _proxy("b")])

    assert exc_info.value.names == ["a", "b"]


@pytest.mark.asyncio
async def test_known_available_wins_over_unknown() -> None:
    redis = _Redis(unknown={"a"})

    picked = await proxy_pool.pick_proxy(
        redis,
        [_proxy("a"), _proxy("b")],
        strategy="failover",
    )

    assert picked is not None
    assert picked.name == "b"


@pytest.mark.asyncio
async def test_invalid_strategy_and_round_robin_outage_fail_closed() -> None:
    redis = _Redis()
    with pytest.raises(ValueError, match="unsupported proxy strategy"):
        await proxy_pool.pick_proxy(redis, [_proxy("a")], strategy="typo")

    async def fail_incr(_key: str) -> int:
        raise RedisConnectionError("redis down")

    redis.incr = fail_incr  # type: ignore[method-assign]
    with pytest.raises(proxy_pool.ProxyStateUnavailable):
        await proxy_pool.pick_proxy(
            redis,
            [_proxy("a")],
            strategy="round_robin",
        )


@pytest.mark.asyncio
async def test_round_robin_is_independent_per_candidate_pool() -> None:
    class Redis(_Redis):
        def __init__(self) -> None:
            super().__init__()
            self.counters: dict[str, int] = {}

        async def incr(self, key: str) -> int:
            self.counters[key] = self.counters.get(key, 0) + 1
            return self.counters[key]

    redis = Redis()
    pool_a = [_proxy("a"), _proxy("b")]
    pool_b = [_proxy("c"), _proxy("d"), _proxy("e")]
    selected_a: list[str] = []
    selected_b: list[str] = []
    for _index in range(3):
        picked_a = await proxy_pool.pick_proxy(redis, pool_a, strategy="round_robin")
        picked_b = await proxy_pool.pick_proxy(redis, pool_b, strategy="round_robin")
        assert picked_a is not None and picked_b is not None
        selected_a.append(picked_a.name)
        selected_b.append(picked_b.name)

    assert selected_a == ["a", "b", "a"]
    assert selected_b == ["c", "d", "e"]
    assert len(redis.counters) == 2


@pytest.mark.asyncio
async def test_runtime_config_does_not_fall_back_to_direct_when_pool_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _proxy("proxy-a")

    async def fake_get_setting_str(
        _db: object,
        key: str,
        default: str = "",
    ) -> str:
        values = {
            "telegram.bot_enabled": "1",
            "telegram.proxy_names": "proxy-a",
            "telegram.proxy_strategy": "failover",
        }
        return values.get(key, default)

    async def fake_get_setting_int(
        _db: object,
        _key: str,
        default: int,
    ) -> int:
        return default

    async def fake_read_providers(_db: object) -> tuple[str, str]:
        return "configured", "db"

    async def no_proxy(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(telegram, "get_redis", lambda: object())
    monkeypatch.setattr(telegram, "_get_setting_str", fake_get_setting_str)
    monkeypatch.setattr(telegram, "_get_setting_int", fake_get_setting_int)
    monkeypatch.setattr(telegram, "_read_providers", fake_read_providers)
    monkeypatch.setattr(telegram, "_parse_config", lambda _raw: ([], [{}]))
    monkeypatch.setattr(telegram, "parse_proxy_item", lambda *_a, **_kw: candidate)
    monkeypatch.setattr(telegram, "pick_proxy", no_proxy)

    with pytest.raises(Exception) as exc_info:
        await telegram.runtime_config(SimpleNamespace())  # type: ignore[arg-type]

    assert getattr(exc_info.value, "status_code", None) == 503
    assert exc_info.value.detail["error"]["code"] == "proxy_pool_exhausted"
