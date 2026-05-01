from __future__ import annotations

import asyncio
import os
from weakref import WeakKeyDictionary

import app.redis_client as redis_client


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
    monkeypatch.setattr(redis_client, "_redis_by_loop", WeakKeyDictionary())
    monkeypatch.setattr(redis_client, "_redis", None)
    monkeypatch.setattr(redis_client, "_redis_loop", None)
    monkeypatch.setattr(redis_client, "_redis_pid", os.getpid())
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
    assert redis_client._redis is None
