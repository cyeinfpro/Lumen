from __future__ import annotations

import json

import pytest

from app import account_limiter, sse_publish


class _FallbackRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.stream_entries: list[tuple[str, dict[str, str]]] = []
        self.deleted: list[str] = []
        self.zadd_calls: list[tuple[str, dict[str, float]]] = []
        self.expire_calls: list[tuple[str, int]] = []
        self.incr_calls: list[str] = []
        self.expireat_calls: list[tuple[str, int]] = []

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

    async def xadd(self, key: str, fields: dict[str, str], **_kwargs: object) -> str:
        stream_id = f"1710000000000-{len(self.stream_entries)}"
        self.stream_entries.append((key, dict(fields)))
        return stream_id

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zadd_calls.append((key, dict(mapping)))
        return 1

    async def expire(self, key: str, ttl: int) -> int:
        self.expire_calls.append((key, ttl))
        return 1

    async def incr(self, key: str) -> int:
        self.incr_calls.append(key)
        return 1

    async def expireat(self, key: str, when: int) -> int:
        self.expireat_calls.append((key, when))
        return 1


@pytest.mark.asyncio
async def test_sse_fallback_waits_for_inflight_dedupe_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _FallbackRedis()
    stream_key = "events:user:user-1"
    event_id = "evt-1"
    dedupe_key = f"{stream_key}:dedupe:{event_id}"
    redis.kv[dedupe_key] = ""

    async def fake_sleep(_delay: float) -> None:
        redis.kv[dedupe_key] = "1710000000000-99"

    monkeypatch.setattr(sse_publish.asyncio, "sleep", fake_sleep)

    stream_id = await sse_publish._xadd_event_without_lua(
        redis,
        stream_key=stream_key,
        event_name="generation.progress",
        event_id=event_id,
        payload_json=json.dumps({"event_id": event_id}),
    )

    assert stream_id == "1710000000000-99"
    assert redis.stream_entries == []
    assert redis.deleted == []


@pytest.mark.asyncio
async def test_record_image_call_normalises_monotonic_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AtomicRedis(_FallbackRedis):
        async def eval(
            self,
            _script: str,
            numkeys: int,
            ts_key: str,
            day_key: str,
            member: str,
            now: str,
            ttl: str,
            expire_at: str,
        ) -> int:
            assert numkeys == 2
            self.zadd_calls.append((ts_key, {member: float(now)}))
            self.expire_calls.append((ts_key, int(ttl)))
            self.incr_calls.append(day_key)
            self.expireat_calls.append((day_key, int(expire_at)))
            return 1

    redis = AtomicRedis()
    monkeypatch.setattr(account_limiter.time, "time", lambda: 1_700_000_000.0)

    await account_limiter.record_image_call(
        redis,
        "acct-1",
        task_id="task-1",
        now=12.34,
    )

    assert redis.zadd_calls == [
        ("lumen:acct:acct-1:image:ts", {"task-1": 1_700_000_000.0})
    ]
    assert redis.incr_calls == ["lumen:acct:acct-1:image:daily:20231114"]
