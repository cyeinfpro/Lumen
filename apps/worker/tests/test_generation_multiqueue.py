from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import time

import pytest

from app.tasks import generation
from app.tasks.generation_parts import queue as generation_queue


class _QueueRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        nx: bool = False,
        ex: int | float | None = None,
        px: int | float | None = None,
    ) -> bool:
        if nx and key in self.strings:
            return False
        self.strings[key] = str(value)
        if ex is not None:
            self.expires[key] = int(ex)
        if px is not None:
            self.expires[key] = int(px)
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.strings:
                deleted += 1
                self.strings.pop(key, None)
            if key in self.zsets:
                deleted += 1
                self.zsets.pop(key, None)
        return deleted

    async def expire(self, key: str, ttl: int | float) -> bool:
        self.expires[key] = int(ttl)
        return True

    async def incrby(self, key: str, amount: int) -> int:
        value = int(self.strings.get(key) or "0") + amount
        self.strings[key] = str(value)
        return value

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        bucket = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[str(member)] = float(score)
        return added

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        items = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        members = [member for member, _score in items]
        if end == -1:
            return members[start:]
        return members[start : end + 1]

    async def zrem(self, key: str, *members: str) -> int:
        bucket = self.zsets.setdefault(key, {})
        removed = 0
        for member in members:
            if member in bucket:
                removed += 1
                bucket.pop(member, None)
        return removed

    async def zscore(self, key: str, member: str) -> float | None:
        return self.zsets.get(key, {}).get(member)

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))

    async def zremrangebyscore(self, key: str, min_score: Any, max_score: Any) -> int:
        bucket = self.zsets.setdefault(key, {})
        low = float("-inf") if str(min_score) == "-inf" else float(min_score)
        high = float("inf") if str(max_score) == "+inf" else float(max_score)
        stale = [
            member
            for member, score in bucket.items()
            if low <= float(score) <= high
        ]
        for member in stale:
            bucket.pop(member, None)
        return len(stale)

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        if numkeys == 1:
            key, token = keys_and_args[:2]
            if self.strings.get(str(key)) == str(token):
                if script == generation_queue.RENEW_IMAGE_QUEUE_LOCK_LUA:
                    return 1
                await self.delete(str(key))
                return 1
            return 0

        if numkeys == 2:
            data_key, lock_key, token, now = keys_and_args
            if self.strings.get(str(lock_key)) != str(token):
                return -1
            if script == generation_queue.CLEANUP_IMAGE_QUEUE_ACTIVE_LUA:
                return await self.zremrangebyscore(data_key, "-inf", now)
            await self.zremrangebyscore(data_key, "-inf", now)
            return await self.zcard(data_key)

        if numkeys != 7:
            raise AssertionError(f"unexpected eval numkeys={numkeys}")

        (
            provider_zset,
            global_zset,
            task_provider_key,
            not_before_key,
            lock_key,
            cursor_key,
            reservation_key,
        ) = (
            str(item) for item in keys_and_args[:7]
        )
        (
            now_raw,
            expiry_raw,
            task_id,
            provider_name,
            provider_cap_raw,
            global_cap_raw,
            task_provider_ttl_raw,
            provider_zset_ttl_raw,
            lock_token,
            cursor_steps,
            reservation_ttl,
        ) = keys_and_args[7:]
        if self.strings.get(lock_key) != str(lock_token):
            return -1
        now = float(now_raw)
        expiry = float(expiry_raw)
        provider_cap = int(provider_cap_raw)
        global_cap = int(global_cap_raw)

        await self.zremrangebyscore(provider_zset, "-inf", now)
        await self.zremrangebyscore(global_zset, "-inf", now)
        if await self.zcard(provider_zset) >= provider_cap:
            return 0
        if await self.zcard(global_zset) >= global_cap:
            return 0

        await self.zadd(provider_zset, {str(task_id): expiry})
        await self.expire(provider_zset, int(provider_zset_ttl_raw))
        await self.set(
            task_provider_key,
            str(provider_name),
            ex=int(task_provider_ttl_raw),
        )
        await self.set(
            reservation_key,
            str(lock_token),
            ex=int(reservation_ttl),
        )
        await self.zadd(global_zset, {str(task_id): expiry})
        await self.delete(not_before_key)
        await self.incrby(cursor_key, int(cursor_steps))
        return 1


def _candidate(task_id: str, lane: str, ordinal: int) -> SimpleNamespace:
    return SimpleNamespace(id=task_id, queue_lane=lane, created_at=ordinal)


def _candidate_id(item: Any) -> str:
    return str(getattr(item, "id", item))


@pytest.mark.asyncio
async def test_weighted_fair_lane_ordering_inside_scan_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        generation,
        "_IMAGE_QUEUE_LANE_WEIGHTS",
        {
            "image:interactive": 2,
            "image:workflow:large": 1,
        },
    )
    monkeypatch.setattr(
        generation,
        "_IMAGE_QUEUE_LANE_RANK",
        {
            "image:interactive": 0,
            "image:workflow:large": 1,
        },
    )
    ready_by_lane = {
        "image:workflow:large": [
            _candidate("wf-1", "image:workflow:large", 1),
            _candidate("wf-2", "image:workflow:large", 2),
            _candidate("wf-3", "image:workflow:large", 3),
            _candidate("wf-4", "image:workflow:large", 4),
        ],
        "image:interactive": [
            _candidate("int-1", "image:interactive", 5),
            _candidate("int-2", "image:interactive", 6),
            _candidate("int-3", "image:interactive", 7),
            _candidate("int-4", "image:interactive", 8),
        ],
    }

    selected = await generation._select_ready_generation_ids_by_lane(
        _QueueRedis(),
        ready_by_lane,
        limit=6,
    )

    assert [_candidate_id(item) for item in selected] == [
        "int-1",
        "wf-1",
        "int-2",
        "int-3",
        "wf-2",
        "int-4",
    ]


@pytest.mark.asyncio
async def test_ready_scan_skips_active_not_before_and_local_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _QueueRedis()
    now = time.time()
    redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY] = {
        "active-old": now + 120.0,
    }
    redis.strings[generation._image_queue_not_before_key("redis-wait")] = str(
        now + 120.0
    )
    monkeypatch.setattr(
        generation,
        "_PROVIDER_COOLDOWN_LOCAL",
        {"local-wait": time.monotonic() + 120.0},
    )

    async def fake_queued_generation_ids(limit: int) -> list[str]:
        return [
            "active-old",
            "redis-wait",
            "local-wait",
            "ready-a",
            "ready-b",
        ][:limit]

    monkeypatch.setattr(
        generation,
        "_queued_generation_ids",
        fake_queued_generation_ids,
    )

    ready = await generation._ready_queued_generation_ids(redis, 2)

    assert ready == ["ready-a", "ready-b"]


@pytest.mark.asyncio
async def test_reserve_admits_task_inside_ready_window_when_capacity_gt_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _QueueRedis()
    ready_limits: list[int] = []
    provider = SimpleNamespace(name="provider-a", image_concurrency=2)

    async def fake_capacity() -> int:
        return 2

    async def fake_ready_queued_generation_ids(
        _redis: Any,
        limit: int,
    ) -> list[str]:
        ready_limits.append(limit)
        return ["oldest", "target"][:limit]

    monkeypatch.setattr(generation, "_resolve_image_queue_capacity", fake_capacity)
    monkeypatch.setattr(
        generation,
        "_ready_queued_generation_ids",
        fake_ready_queued_generation_ids,
    )

    admitted = await generation._reserve_image_queue_slot(
        redis,
        "target",
        endpoint_kind="generations",
        provider_override=provider,
    )

    assert admitted is provider
    assert max(ready_limits) >= 2
    assert redis.strings[generation._image_task_provider_key("target")] == "provider-a"
    assert "target" in redis.zsets[generation._IMAGE_QUEUE_ACTIVE_KEY]
    assert "target" in redis.zsets[generation._image_provider_active_key("provider-a")]
