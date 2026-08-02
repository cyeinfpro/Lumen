from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

import app.redis_client as redis_client
from app.redis_client import ReconnectingRedis


class FakeRedis:
    def __init__(self):
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _reset_redis_state(monkeypatch) -> list[FakeRedis]:
    created: list[FakeRedis] = []

    def fake_new_redis() -> FakeRedis:
        client = FakeRedis()
        created.append(client)
        return client

    monkeypatch.setattr(redis_client, "_new_redis", fake_new_redis)
    monkeypatch.setattr(redis_client, "_redis_state", redis_client._RedisState())
    return created


def test_get_redis_reuses_clients_per_event_loop(monkeypatch) -> None:
    created = _reset_redis_state(monkeypatch)
    loop1 = asyncio.new_event_loop()
    loop2 = asyncio.new_event_loop()

    async def get_client():
        return redis_client.get_redis()

    try:
        first = loop1.run_until_complete(get_client())
        again = loop1.run_until_complete(get_client())
        second = loop2.run_until_complete(get_client())
    finally:
        loop1.close()
        loop2.close()

    assert first is again
    assert second is not first
    assert len(created) == 2
    assert [client.closed for client in created] == [False, False]


def test_close_redis_closes_current_loop_client(monkeypatch) -> None:
    created = _reset_redis_state(monkeypatch)
    loop = asyncio.new_event_loop()

    async def use_and_close_client():
        client = redis_client.get_redis()
        await redis_client.close_redis()
        return client

    try:
        client = loop.run_until_complete(use_and_close_client())
    finally:
        loop.close()

    assert client is created[0]
    assert client.closed is True
    assert redis_client._redis_state.client is None


def _make_flaky_execute(fail_times: int, error: Exception, result: object = "ok"):
    """替身 super().execute_command:前 fail_times 次抛错,之后返回固定值。"""
    calls: list[tuple] = []

    async def fake_execute(self, *args, **options):  # type: ignore[no-untyped-def]
        calls.append(args)
        if len(calls) <= fail_times:
            raise error
        return result

    return fake_execute, calls


def test_execute_command_retries_idempotent_command(monkeypatch) -> None:
    fake, calls = _make_flaky_execute(1, RedisConnectionError("boom"))
    monkeypatch.setattr(redis_client.redis.Redis, "execute_command", fake)

    async def run():
        client = ReconnectingRedis()
        return await client.execute_command("GET", "k")

    result = asyncio.run(run())
    assert result == "ok"
    assert [c[0] for c in calls] == ["GET", "GET"]


def test_execute_command_gives_up_after_retries_exhausted(monkeypatch) -> None:
    fake, calls = _make_flaky_execute(10, RedisConnectionError("boom"))
    monkeypatch.setattr(redis_client.redis.Redis, "execute_command", fake)

    async def run():
        client = ReconnectingRedis()
        return await client.execute_command("GET", "k")

    with pytest.raises(RedisConnectionError):
        asyncio.run(run())
    # 1 次原发 + 3 次重试,与 _REDIS_RETRY_DELAYS 长度一致
    assert len(calls) == len(redis_client._REDIS_RETRY_DELAYS) + 1


@pytest.mark.parametrize(
    ("error", "command"),
    [
        (RedisConnectionError("boom"), "INCR"),
        (RedisTimeoutError("boom"), "INCR"),
        (RedisTimeoutError("boom"), "SET"),
        (RedisConnectionError("boom"), "PUBLISH"),
        (RedisTimeoutError("boom"), "EVAL"),
    ],
)
def test_execute_command_does_not_retry_non_idempotent_command(
    monkeypatch, error, command
) -> None:
    fake, calls = _make_flaky_execute(3, error)
    monkeypatch.setattr(redis_client.redis.Redis, "execute_command", fake)

    async def run():
        client = ReconnectingRedis()
        return await client.execute_command(command, "k")

    with pytest.raises(type(error)):
        asyncio.run(run())
    assert len(calls) == 1
