from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import lumen_core
from app.config import Settings
from app.tasks import generation_parts
from app.tasks.generation_parts import (
    lease,
    queue as generation_queue,
    queue_claim,
    runner,
)
from app.tasks.generation_parts.default_runtime import build_generation_runtime
from app.tasks.generation_parts.diagnostics import StageTimer


REMOVED_RESOURCE_KEY_PREFIX = "generation:image_resources:"
PARTS_ROOT = Path(__file__).resolve().parents[1] / "app" / "tasks" / "generation_parts"
CORE_ROOT = Path(__file__).resolve().parents[3] / "packages" / "core" / "lumen_core"


class _QueueService:
    expose_provider_diagnostics = False

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.provider_cooldowns: dict[str, float] = {}

    def configured_capacity(self) -> int:
        return self.capacity

    async def resolve_capacity(self) -> int:
        return self.capacity


class _QueueRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires: dict[str, int] = {}
        self.touched_keys: set[str] = set()
        self.ready_task = ""

    def _touch(self, *values: Any) -> None:
        for value in values:
            text = str(value)
            if text.startswith(REMOVED_RESOURCE_KEY_PREFIX):
                raise AssertionError(f"removed resource key touched: {text}")
            self.touched_keys.add(text)

    async def get(self, key: str) -> str | None:
        self._touch(key)
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
        self._touch(key)
        if nx and key in self.strings:
            return False
        self.strings[key] = str(value)
        ttl = ex if ex is not None else px
        if ttl is not None:
            self.expires[key] = int(ttl)
        return True

    async def delete(self, *keys: str) -> int:
        self._touch(*keys)
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
        self._touch(key)
        self.expires[key] = int(ttl)
        return True

    async def incrby(self, key: str, amount: int) -> int:
        self._touch(key)
        value = int(self.strings.get(key) or "0") + int(amount)
        self.strings[key] = str(value)
        return value

    async def smembers(self, key: str) -> set[str]:
        self._touch(key)
        return set()

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self._touch(key)
        bucket = self.zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[str(member)] = float(score)
        return added

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        self._touch(key)
        items = sorted(self.zsets.get(key, {}).items(), key=lambda item: item[1])
        members = [member for member, _score in items]
        return members[start:] if end == -1 else members[start : end + 1]

    async def zrem(self, key: str, *members: str) -> int:
        self._touch(key)
        bucket = self.zsets.setdefault(key, {})
        removed = 0
        for member in members:
            if member in bucket:
                removed += 1
                bucket.pop(member, None)
        return removed

    async def zscore(self, key: str, member: str) -> float | None:
        self._touch(key)
        return self.zsets.get(key, {}).get(member)

    async def zcard(self, key: str) -> int:
        self._touch(key)
        return len(self.zsets.get(key, {}))

    async def zremrangebyscore(
        self,
        key: str,
        min_score: Any,
        max_score: Any,
    ) -> int:
        self._touch(key)
        low = float("-inf") if str(min_score) == "-inf" else float(min_score)
        high = float("inf") if str(max_score) == "+inf" else float(max_score)
        bucket = self.zsets.setdefault(key, {})
        stale = [
            member
            for member, score in bucket.items()
            if low <= float(score) <= high
        ]
        for member in stale:
            bucket.pop(member, None)
        return len(stale)

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        self._touch(*args)
        if script == generation_queue.RENEW_IMAGE_QUEUE_LOCK_LUA:
            key, token = str(args[0]), str(args[1])
            return int(self.strings.get(key) == token)
        if script == lease.RELEASE_LEASE_LUA:
            key, token = str(args[0]), str(args[1])
            if self.strings.get(key) != token:
                return 0
            return await self.delete(key)
        if script == generation_queue.CLEANUP_IMAGE_QUEUE_ACTIVE_LUA:
            data_key, lock_key, token, now = (str(value) for value in args)
            if self.strings.get(lock_key) != token:
                return -1
            return await self.zremrangebyscore(data_key, "-inf", now)
        if script == generation_queue.CLEANUP_IMAGE_QUEUE_PROVIDER_LUA:
            data_key, lock_key, token, now = (str(value) for value in args)
            if self.strings.get(lock_key) != token:
                return -1
            await self.zremrangebyscore(data_key, "-inf", now)
            return await self.zcard(data_key)
        if script != queue_claim.RESERVE_IMAGE_SLOT_LUA or numkeys != 7:
            raise AssertionError("unexpected Redis script")

        (
            provider_zset,
            global_zset,
            task_provider_key,
            not_before_key,
            lock_key,
            cursor_key,
            reservation_key,
        ) = (str(value) for value in args[:7])
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
            cursor_steps_raw,
            reservation_ttl_raw,
        ) = args[7:]
        if self.strings.get(lock_key) != str(lock_token):
            return -1

        now = float(now_raw)
        expiry = float(expiry_raw)
        await self.zremrangebyscore(provider_zset, "-inf", now)
        await self.zremrangebyscore(global_zset, "-inf", now)
        if await self.zcard(provider_zset) >= int(provider_cap_raw):
            return 0
        if await self.zcard(global_zset) >= int(global_cap_raw):
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
            ex=int(reservation_ttl_raw),
        )
        await self.zadd(global_zset, {str(task_id): expiry})
        await self.delete(not_before_key)
        await self.incrby(cursor_key, int(cursor_steps_raw))
        return 1


def test_removed_resource_admission_surface_stays_deleted() -> None:
    assert not (PARTS_ROOT / "admission.py").exists()
    assert not (PARTS_ROOT / "queue_permit.py").exists()
    assert not (CORE_ROOT / "generation_resources.py").exists()
    for field_name in (
        "image_generation_resource_units",
        "image_generation_external_lane_units",
        "image_generation_user_resource_units",
    ):
        assert field_name not in Settings.model_fields
    assert not hasattr(build_generation_runtime().deps.queue, "resource_budgets")
    assert not hasattr(lumen_core, "ResourceDemand")
    assert {
        "default_weighted_permit",
        "reserve_generation_permit",
        "release_generation_permit",
    }.isdisjoint(generation_parts.__all__)


@pytest.mark.asyncio
async def test_same_user_4k_reference_tasks_follow_explicit_concurrency_caps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _QueueRedis()
    provider = SimpleNamespace(name="provider-wide", image_concurrency=32)
    services = replace(
        build_generation_runtime().deps,
        queue=_QueueService(16),
    )

    async def ready(
        fake_redis: _QueueRedis,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
        return [fake_redis.ready_task]

    monkeypatch.setattr(queue_claim, "ready_queued_generation_ids", ready)
    admitted: list[str] = []
    for index in range(17):
        task_id = f"same-user-4k-ref-{index}"
        redis.ready_task = task_id
        state = SimpleNamespace(
            redis=redis,
            task_id=task_id,
            is_dual_race=False,
            endpoint_kind="generations",
            requires_mask_provider=False,
            user_runtime_provider=provider,
            services=services,
            stage_timer=StageTimer(),
            reserved_provider=None,
            size_requested="3840x2160",
            input_image_ids=[f"reference-{item}" for item in range(30)],
            user_id="same-user",
            attempt=1,
            dispatch_identity=None,
        )

        delay = await runner._reserve_provider(
            state,
            {
                "queue_lane": "image:interactive:large",
                "size_bucket": "large",
                "cost_class": "high",
            },
        )

        assert delay == 0
        if state.reserved_provider is not None:
            admitted.append(task_id)

    assert admitted == [f"same-user-4k-ref-{index}" for index in range(16)]
    assert len(redis.zsets[generation_queue.IMAGE_QUEUE_ACTIVE_KEY]) == 16
    assert (
        len(
            redis.zsets[
                generation_queue.image_provider_active_key(provider.name)
            ]
        )
        == 16
    )
    all_keys = (
        set(redis.strings)
        | set(redis.zsets)
        | set(redis.expires)
        | redis.touched_keys
    )
    assert not any(
        key.startswith(REMOVED_RESOURCE_KEY_PREFIX)
        for key in all_keys
    )
