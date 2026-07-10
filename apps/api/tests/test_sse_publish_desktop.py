from __future__ import annotations

import json
from typing import Any

import pytest

from app import sse_publish


class GarnetNoStreamRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.eval_calls = 0
        self.xadd_calls = 0

    async def eval(
        self,
        _lua: str,
        _num_keys: int,
        _stream_key: str,
        dedupe_key: str,
        *_args: str,
    ) -> str:
        self.eval_calls += 1
        self.kv[dedupe_key] = ""
        raise RuntimeError("Unknown Redis command called from script")

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
        if nx and key in self.kv:
            return False
        if xx and key not in self.kv:
            return False
        self.kv[key] = value
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1 if self.kv.pop(key, None) is not None else 0

    async def xadd(
        self,
        _key: str,
        _fields: dict[str, str],
        **_kwargs: Any,
    ) -> str:
        self.xadd_calls += 1
        raise RuntimeError("unknown command")

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


@pytest.mark.asyncio
async def test_api_uses_live_only_id_for_desktop_garnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUMEN_RUNTIME", "desktop")
    redis = GarnetNoStreamRedis()

    stream_id = await sse_publish.publish_sse_event(
        redis,
        user_id="user-1",
        channel="conv:conv-1",
        event_name="conv.message.appended",
        data={"conversation_id": "conv-1", "message_id": "msg-1"},
    )

    dedupe_key = next(iter(redis.kv))
    published = json.loads(redis.published[0][1])
    assert redis.eval_calls == 1
    assert redis.xadd_calls == 1
    assert redis.deleted == [dedupe_key]
    assert stream_id.startswith("live-")
    assert redis.kv[dedupe_key] == stream_id
    assert published["sse_id"] == stream_id
