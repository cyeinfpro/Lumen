from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from lumen_core import sse_durable


class WatchError(RuntimeError):
    pass


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.watched: dict[str, str | None] = {}
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def watch(self, *keys: str) -> None:
        self.watched = {key: self.redis.kv.get(key) for key in keys}

    async def get(self, key: str) -> str | None:
        return self.redis.kv.get(key)

    def multi(self) -> None:
        return None

    def delete(self, key: str) -> None:
        self.commands.append(("delete", (key,), {}))

    def set(self, key: str, value: str, **kwargs: Any) -> None:
        self.commands.append(("set", (key, value), kwargs))

    def xadd(self, key: str, fields: dict[str, str], **kwargs: Any) -> None:
        self.commands.append(("xadd", (key, fields), kwargs))

    def expire(self, key: str, ttl: int) -> None:
        self.commands.append(("expire", (key, ttl), {}))

    async def execute(self) -> list[Any]:
        async with self.redis.lock:
            if {key: self.redis.kv.get(key) for key in self.watched} != self.watched:
                raise WatchError("watched value changed")
            results: list[Any] = []
            included_xadd = False
            for command, args, kwargs in self.commands:
                if command == "delete":
                    results.append(int(self.redis.kv.pop(args[0], None) is not None))
                elif command == "set":
                    key, value = args
                    if kwargs.get("xx") and key not in self.redis.kv:
                        results.append(False)
                    else:
                        self.redis.kv[key] = value
                        results.append(True)
                elif command == "xadd":
                    key, fields = args
                    stream_id = f"1710000000000-{len(self.redis.stream_entries)}"
                    self.redis.stream_entries.append((key, dict(fields)))
                    results.append(stream_id)
                    included_xadd = True
                elif command == "expire":
                    results.append(1)
                else:
                    raise AssertionError(f"unexpected command: {command}")
            if included_xadd and self.redis.lose_xadd_response:
                self.redis.lose_xadd_response = False
                raise RuntimeError("connection dropped after EXEC")
            return results

    async def reset(self) -> None:
        return None


class _Redis:
    eval = None

    def __init__(self, *, lose_xadd_response: bool = False) -> None:
        self.kv: dict[str, str] = {}
        self.stream_entries: list[tuple[str, dict[str, str]]] = []
        self.lock = asyncio.Lock()
        self.lose_xadd_response = lose_xadd_response

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool:
        _ = ex
        async with self.lock:
            if nx and key in self.kv:
                return False
            if xx and key not in self.kv:
                return False
            self.kv[key] = value
            return True

    async def pttl(self, _key: str) -> int:
        return 86_400_000

    async def xrevrange(self, key: str, *, count: int) -> list[Any]:
        return [
            (f"1710000000000-{index}", fields)
            for index, (stream_key, fields) in reversed(
                list(enumerate(self.stream_entries))
            )
            if stream_key == key
        ][:count]

    def pipeline(self, *, transaction: bool = True) -> _Pipeline:
        assert transaction is True
        return _Pipeline(self)


def _config() -> sse_durable.DurableSseAppendConfig:
    return sse_durable.DurableSseAppendConfig(
        maxlen=1000,
        dedupe_ttl_seconds=86_400,
        stream_ttl_seconds=86_400,
        reservation_wait_seconds=0.05,
        reservation_poll_seconds=0.001,
    )


@pytest.mark.asyncio
async def test_concurrent_same_event_id_appends_once() -> None:
    redis = _Redis()
    kwargs = {
        "stream_key": "events:user:user-1",
        "event_name": "generation.progress",
        "event_id": "evt-stable",
        "payload_json": json.dumps({"event_id": "evt-stable"}),
        "config": _config(),
    }

    first, second = await asyncio.gather(
        sse_durable.append_sse_event_once(redis, **kwargs),
        sse_durable.append_sse_event_once(redis, **kwargs),
    )

    assert first == second == "1710000000000-0"
    assert len(redis.stream_entries) == 1
    assert redis.kv["events:user:user-1:dedupe:evt-stable"] == "1710000000000-0"


@pytest.mark.asyncio
async def test_response_loss_recovers_stream_id_without_second_xadd() -> None:
    redis = _Redis(lose_xadd_response=True)
    config = _config()
    kwargs = {
        "stream_key": "events:user:user-1",
        "event_name": "generation.progress",
        "event_id": "evt-response-loss",
        "payload_json": json.dumps({"event_id": "evt-response-loss"}),
        "config": config,
    }

    with pytest.raises(RuntimeError, match="connection dropped after EXEC"):
        await sse_durable.append_sse_event_once(redis, **kwargs)

    stream_id = await sse_durable.append_sse_event_once(redis, **kwargs)

    assert stream_id == "1710000000000-0"
    assert len(redis.stream_entries) == 1
    assert redis.kv["events:user:user-1:dedupe:evt-response-loss"] == "1710000000000-0"


def test_lua_establishes_stream_ttl_before_returning_id() -> None:
    lua = " ".join(sse_durable.XADD_IDEMPOTENT_LUA.split())

    xadd_index = lua.index("local stream_id = redis.call( 'XADD'")
    expire_index = lua.index("local ttl_set = redis.call('EXPIRE'")
    store_index = lua.index("redis.call('SET', KEYS[2], stream_id")

    assert xadd_index < expire_index < store_index
