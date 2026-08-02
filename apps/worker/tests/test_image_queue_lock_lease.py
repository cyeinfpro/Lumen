from __future__ import annotations

# ruff: noqa: E402

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import WatchError

from lumen_core.constants import GenerationErrorCode as EC

from app.provider_runtime.errors import UpstreamError
from app.tasks.generation_parts import (
    admission as generation_admission,
    lease,
    queue,
    queue_claim,
    queue_lock,
    retry_state,
)
from app.tasks.generation_parts.default_runtime import build_generation_runtime


generation_services = build_generation_runtime().deps


class _TimingRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.string_deadlines: dict[str, float] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self._mutex = asyncio.Lock()

        self.first_lock_token: str | None = None
        self.block_first_renewals = False
        self.blocked_renewal_started = asyncio.Event()
        self.allow_first_renewals = asyncio.Event()

        self.blocked_get_key: str | None = None
        self.blocked_get_started = asyncio.Event()
        self.allow_blocked_get = asyncio.Event()
        self._blocked_get_used = False

        self.reservation_writes: list[tuple[str, str, str]] = []
        self.fenced_mutations: list[tuple[str, str]] = []
        self.release_results: list[tuple[str, int]] = []
        self.reservation_release_results: list[tuple[str, int]] = []

        self.block_release_token: str | None = None
        self.blocked_release_started = asyncio.Event()
        self.allow_blocked_release = asyncio.Event()

    def _purge_string(self, key: str) -> None:
        deadline = self.string_deadlines.get(key)
        if deadline is not None and deadline <= time.monotonic():
            self.strings.pop(key, None)
            self.string_deadlines.pop(key, None)

    def _get_string(self, key: str) -> str | None:
        self._purge_string(key)
        return self.strings.get(key)

    def _set_string(
        self,
        key: str,
        value: Any,
        *,
        ex: int | float | None = None,
        px: int | float | None = None,
    ) -> None:
        self.strings[key] = str(value)
        if px is not None:
            self.string_deadlines[key] = time.monotonic() + float(px) / 1000.0
        elif ex is not None:
            self.string_deadlines[key] = time.monotonic() + float(ex)
        else:
            self.string_deadlines.pop(key, None)

    def _delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            self._purge_string(key)
            if key in self.strings:
                deleted += 1
                self.strings.pop(key, None)
                self.string_deadlines.pop(key, None)
            if key in self.zsets:
                deleted += 1
                self.zsets.pop(key, None)
        return deleted

    def _zremrangebyscore(self, key: str, max_score: Any) -> int:
        limit = float(max_score)
        bucket = self.zsets.setdefault(key, {})
        stale = [member for member, score in bucket.items() if score <= limit]
        for member in stale:
            bucket.pop(member, None)
        return len(stale)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        nx: bool = False,
        ex: int | float | None = None,
        px: int | float | None = None,
    ) -> bool:
        async with self._mutex:
            self._purge_string(key)
            if nx and key in self.strings:
                return False
            self._set_string(key, value, ex=ex, px=px)
            if key == queue_lock.IMAGE_QUEUE_LOCK_KEY and self.first_lock_token is None:
                self.first_lock_token = str(value)
            return True

    async def get(self, key: str) -> str | None:
        async with self._mutex:
            value = self._get_string(key)
            should_block = key == self.blocked_get_key and not self._blocked_get_used
            if should_block:
                self._blocked_get_used = True
        if should_block:
            self.blocked_get_started.set()
            await self.allow_blocked_get.wait()
        return value

    async def delete(self, *keys: str) -> int:
        async with self._mutex:
            return self._delete(*keys)

    async def expire(self, key: str, ttl: int | float) -> bool:
        async with self._mutex:
            if self._get_string(key) is None and key not in self.zsets:
                return False
            self.string_deadlines[key] = time.monotonic() + float(ttl)
            return True

    async def incrby(self, key: str, amount: int) -> int:
        async with self._mutex:
            value = int(self._get_string(key) or "0") + int(amount)
            self._set_string(key, value)
            return value

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        async with self._mutex:
            bucket = self.zsets.setdefault(key, {})
            added = sum(1 for member in mapping if member not in bucket)
            bucket.update(
                {str(member): float(score) for member, score in mapping.items()}
            )
            return added

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        async with self._mutex:
            members = [
                member
                for member, _score in sorted(
                    self.zsets.get(key, {}).items(),
                    key=lambda item: item[1],
                )
            ]
        return members[start:] if end == -1 else members[start : end + 1]

    async def zrem(self, key: str, *members: str) -> int:
        async with self._mutex:
            bucket = self.zsets.setdefault(key, {})
            removed = 0
            for member in members:
                if member in bucket:
                    bucket.pop(member, None)
                    removed += 1
            return removed

    async def zscore(self, key: str, member: str) -> float | None:
        async with self._mutex:
            return self.zsets.get(key, {}).get(member)

    async def zcard(self, key: str) -> int:
        async with self._mutex:
            return len(self.zsets.get(key, {}))

    async def zremrangebyscore(
        self,
        key: str,
        _min_score: Any,
        max_score: Any,
    ) -> int:
        async with self._mutex:
            return self._zremrangebyscore(key, max_score)

    async def smembers(self, _key: str) -> set[str]:
        return set()

    async def eval(self, script: str, _numkeys: int, *args: Any) -> Any:
        if script == generation_admission.RESERVE_WEIGHTED_PERMIT_LUA:
            return 1
        if script in {
            generation_admission.RELEASE_WEIGHTED_PERMIT_LUA,
            generation_admission.RENEW_WEIGHTED_PERMIT_LUA,
        }:
            return 1
        if script == queue.RENEW_IMAGE_QUEUE_LOCK_LUA:
            lock_key, token, ttl_ms = str(args[0]), str(args[1]), int(args[2])
            if self.block_first_renewals and token == self.first_lock_token:
                self.blocked_renewal_started.set()
                await self.allow_first_renewals.wait()
            async with self._mutex:
                if self._get_string(lock_key) != token:
                    return 0
                self.string_deadlines[lock_key] = time.monotonic() + ttl_ms / 1000.0
                return 1

        if script == lease.RELEASE_LEASE_LUA:
            lock_key, token = str(args[0]), str(args[1])
            async with self._mutex:
                if self._get_string(lock_key) != token:
                    self.release_results.append((token, 0))
                    return 0
                self._delete(lock_key)
                self.release_results.append((token, 1))
                return 1

        if script == queue_claim.RELEASE_IMAGE_QUEUE_SLOT_LUA:
            (
                provider_key,
                global_key,
                task_provider_key,
                task_lease_key,
                reservation_key,
                legacy_provider_lock_key,
                reservation_token,
                lease_token,
                expected_provider,
                task_id,
                active_member,
            ) = (str(item) for item in args)
            if lease_token == self.block_release_token:
                self.blocked_release_started.set()
                await self.allow_blocked_release.wait()
            async with self._mutex:
                current_reservation = self._get_string(reservation_key)
                owns_slot = (
                    bool(
                        reservation_token
                        and current_reservation == reservation_token
                    )
                    if current_reservation
                    else bool(
                        lease_token
                        and self._get_string(task_lease_key) == lease_token
                    )
                )
                if not owns_slot:
                    self.reservation_release_results.append((lease_token, 0))
                    return 0
                if self._get_string(task_provider_key) != expected_provider:
                    self.reservation_release_results.append((lease_token, 0))
                    return 0
                self.zsets.setdefault(provider_key, {}).pop(task_id, None)
                self.zsets.setdefault(global_key, {}).pop(active_member, None)
                self._delete(task_provider_key)
                self._delete(reservation_key)
                if self._get_string(legacy_provider_lock_key) == task_id:
                    self._delete(legacy_provider_lock_key)
                owner_token = reservation_token if current_reservation else lease_token
                self.reservation_release_results.append((owner_token, 1))
                self.fenced_mutations.append(("release", owner_token))
                return 1

        if script == queue.CLEANUP_IMAGE_QUEUE_ACTIVE_LUA:
            active_key, lock_key, token, now = args
            async with self._mutex:
                if self._get_string(str(lock_key)) != str(token):
                    return -1
                return self._zremrangebyscore(str(active_key), now)

        if script == queue.CLEANUP_IMAGE_QUEUE_PROVIDER_LUA:
            provider_key, lock_key, token, now = args
            async with self._mutex:
                if self._get_string(str(lock_key)) != str(token):
                    return -1
                self._zremrangebyscore(str(provider_key), now)
                return len(self.zsets.get(str(provider_key), {}))

        if script == queue.CLEAR_STALE_IMAGE_QUEUE_RESERVATION_LUA:
            (
                provider_key,
                global_key,
                task_provider_key,
                lock_key,
                token,
                expected_provider,
                task_id,
                active_member,
            ) = (str(item) for item in args)
            async with self._mutex:
                if self._get_string(lock_key) != token:
                    return -1
                if self._get_string(task_provider_key) != expected_provider:
                    return 0
                self.zsets.setdefault(provider_key, {}).pop(task_id, None)
                self.zsets.setdefault(global_key, {}).pop(active_member, None)
                self._delete(task_provider_key)
                self.fenced_mutations.append(("clear", token))
                return 1

        if script == queue_claim.RESERVE_DUAL_RACE_SLOT_LUA:
            (
                task_provider_key,
                global_key,
                not_before_key,
                cursor_key,
                lock_key,
                reservation_key,
                lock_token,
                sentinel,
                expiry,
                task_provider_ttl,
                cursor_steps,
                reservation_ttl,
            ) = args
            async with self._mutex:
                if self._get_string(str(lock_key)) != str(lock_token):
                    return -1
                if self._get_string(str(task_provider_key)) is not None:
                    return 0
                self._set_string(
                    str(task_provider_key),
                    sentinel,
                    ex=float(task_provider_ttl),
                )
                self._set_string(
                    str(reservation_key),
                    lock_token,
                    ex=float(reservation_ttl),
                )
                self.zsets.setdefault(str(global_key), {})[str(sentinel)] = float(
                    expiry
                )
                self._delete(str(not_before_key))
                cursor = int(self._get_string(str(cursor_key)) or "0")
                self._set_string(str(cursor_key), cursor + int(cursor_steps))
                token = str(lock_token)
                self.reservation_writes.append((token, str(sentinel), str(sentinel)))
                self.fenced_mutations.append(("reserve", token))
                return 1

        if script == queue_claim.RESERVE_IMAGE_SLOT_LUA:
            (
                provider_key,
                global_key,
                task_provider_key,
                not_before_key,
                lock_key,
                cursor_key,
                reservation_key,
                now,
                expiry,
                task_id,
                provider_name,
                provider_cap,
                global_cap,
                task_provider_ttl,
                _provider_zset_ttl,
                lock_token,
                cursor_steps,
                reservation_ttl,
            ) = args
            async with self._mutex:
                if self._get_string(str(lock_key)) != str(lock_token):
                    return -1
                self._zremrangebyscore(str(provider_key), now)
                self._zremrangebyscore(str(global_key), now)
                provider_bucket = self.zsets.setdefault(str(provider_key), {})
                global_bucket = self.zsets.setdefault(str(global_key), {})
                if len(provider_bucket) >= int(provider_cap):
                    return 0
                if len(global_bucket) >= int(global_cap):
                    return 0
                provider_bucket[str(task_id)] = float(expiry)
                global_bucket[str(task_id)] = float(expiry)
                self._set_string(
                    str(task_provider_key),
                    provider_name,
                    ex=float(task_provider_ttl),
                )
                self._set_string(
                    str(reservation_key),
                    lock_token,
                    ex=float(reservation_ttl),
                )
                self._delete(str(not_before_key))
                cursor = int(self._get_string(str(cursor_key)) or "0")
                self._set_string(str(cursor_key), cursor + int(cursor_steps))
                token = str(lock_token)
                self.reservation_writes.append(
                    (token, str(task_id), str(provider_name))
                )
                self.fenced_mutations.append(("reserve", token))
                return 1

        if script == queue.DELETE_IMAGE_QUEUE_KEY_IF_OWNER_LUA:
            key, lock_key, token = (str(item) for item in args)
            async with self._mutex:
                if self._get_string(lock_key) != token:
                    return -1
                return self._delete(key)

        if script == queue.SET_IMAGE_QUEUE_VALUE_IF_OWNER_LUA:
            key, lock_key, token, value, ttl_ms = args
            async with self._mutex:
                if self._get_string(str(lock_key)) != str(token):
                    return -1
                self._set_string(str(key), value, px=int(ttl_ms))
                return "OK"

        raise AssertionError(f"unexpected Lua script: {script}")


class _WatchOnlyPipeline:
    def __init__(self, redis: _WatchOnlyRedis) -> None:
        self.redis = redis
        self.version = 0
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    async def watch(self, _key: str) -> None:
        self.version = self.redis.version

    async def get(self, key: str) -> str | None:
        return await self.redis.get(key)

    def multi(self) -> None:
        return None

    def pexpire(self, key: str, ttl_ms: int) -> None:
        self.commands.append(("pexpire", (key, ttl_ms)))

    def delete(self, key: str) -> None:
        self.commands.append(("delete", (key,)))

    async def execute(self) -> list[int]:
        if self.version != self.redis.version:
            raise WatchError("owner changed")
        results: list[int] = []
        for command, args in self.commands:
            results.append(await getattr(self.redis, command)(*args))
        return results

    async def reset(self) -> None:
        return None


class _WatchOnlyRedis:
    eval = None

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.version = 0
        self.reservation_writes = 0

    async def set(
        self,
        key: str,
        value: Any,
        *,
        nx: bool = False,
        ex: int | float | None = None,
        px: int | float | None = None,
    ) -> bool:
        _ = ex, px
        if nx and key in self.store:
            return False
        self.store[key] = str(value)
        self.version += 1
        return True

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def delete(self, key: str) -> int:
        existed = key in self.store
        self.store.pop(key, None)
        self.version += 1
        return int(existed)

    async def pexpire(self, key: str, _ttl_ms: int) -> int:
        return int(key in self.store)

    def pipeline(self, *, transaction: bool = True) -> _WatchOnlyPipeline:
        assert transaction is True
        return _WatchOnlyPipeline(self)


@pytest.mark.asyncio
async def test_image_queue_lock_heartbeat_keeps_slow_critical_section_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _TimingRedis()
    monkeypatch.setattr(queue_lock, "IMAGE_QUEUE_LOCK_TTL_S", 0.09)
    monkeypatch.setattr(queue_lock, "IMAGE_QUEUE_LOCK_WAIT_S", 0.8)

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    intervals: dict[str, tuple[float, float]] = {}

    async def first_owner() -> None:
        async with queue_lock.image_queue_lock(redis) as lock:
            started = time.monotonic()
            first_entered.set()
            await asyncio.sleep(0.28)
            assert await redis.get(queue_lock.IMAGE_QUEUE_LOCK_KEY) == lock.token
            intervals["first"] = (started, time.monotonic())

    async def second_owner() -> None:
        await first_entered.wait()
        async with queue_lock.image_queue_lock(redis):
            started = time.monotonic()
            second_entered.set()
            intervals["second"] = (started, time.monotonic())

    first = asyncio.create_task(first_owner())
    second = asyncio.create_task(second_owner())
    await first_entered.wait()
    await asyncio.sleep(0.18)
    assert second_entered.is_set() is False
    await asyncio.gather(first, second)

    assert intervals["second"][0] >= intervals["first"][1]


@pytest.mark.asyncio
async def test_slow_provider_select_loses_lock_without_cancellation_or_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool

    task_id = "gen-lock-race"
    provider = SimpleNamespace(name="provider-new", image_concurrency=1)
    redis = _TimingRedis()
    task_provider_key = queue.image_task_provider_key(task_id)
    slow_select_started = asyncio.Event()
    allow_slow_select = asyncio.Event()
    select_calls = 0

    async def capacity(*, services: Any) -> int:
        _ = services
        return 1

    async def ready(
        _redis: Any,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
        return [task_id]

    class Pool:
        async def select(self, **_kwargs: Any) -> list[Any]:
            nonlocal select_calls
            select_calls += 1
            if select_calls == 1:
                slow_select_started.set()
                await allow_slow_select.wait()
            return [provider]

    pool = Pool()

    async def get_pool() -> Pool:
        return pool

    monkeypatch.setattr(queue_lock, "IMAGE_QUEUE_LOCK_TTL_S", 0.12)
    monkeypatch.setattr(queue_lock, "IMAGE_QUEUE_LOCK_WAIT_S", 1.0)
    monkeypatch.setattr(
        queue_claim,
        "resolve_image_queue_capacity",
        capacity,
    )
    monkeypatch.setattr(queue_claim, "ready_queued_generation_ids", ready)
    monkeypatch.setattr(provider_pool, "get_pool", get_pool)

    old_owner = asyncio.create_task(
        queue_claim.reserve_image_queue_slot(
            redis,
            task_id,
            services=generation_services,
        )
    )
    await asyncio.wait_for(slow_select_started.wait(), timeout=1.0)
    assert redis.first_lock_token is not None

    redis.block_first_renewals = True
    await asyncio.wait_for(redis.blocked_renewal_started.wait(), timeout=1.0)
    await asyncio.sleep(0.15)

    new_owner_result = await asyncio.wait_for(
        queue_claim.reserve_image_queue_slot(
            redis,
            task_id,
            services=generation_services,
        ),
        timeout=1.0,
    )
    assert new_owner_result is provider

    allow_slow_select.set()
    await asyncio.sleep(0)
    redis.allow_first_renewals.set()
    old_result = (await asyncio.gather(old_owner, return_exceptions=True))[0]

    assert isinstance(old_result, UpstreamError)
    assert not isinstance(old_result, asyncio.CancelledError)
    assert old_result.error_code == EC.LOCAL_QUEUE_FULL.value
    assert old_result.payload["retry_after"] > 0
    assert retry_state.classify_exception(old_result, False).retriable is True
    assert redis.strings[task_provider_key] == provider.name
    assert list(redis.zsets[queue.image_provider_active_key(provider.name)]) == [
        task_id
    ]
    assert list(redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]) == [task_id]
    assert len(redis.reservation_writes) == 1
    assert redis.reservation_writes[0][1:] == (task_id, provider.name)
    assert (
        redis.strings[queue_claim._image_queue_reservation_token_key(task_id)]
        == redis.reservation_writes[0][0]
    )
    assert all(
        token != redis.first_lock_token for _operation, token in redis.fenced_mutations
    )
    assert (redis.first_lock_token, 0) in redis.release_results


@pytest.mark.asyncio
async def test_dual_race_reservation_token_survives_lease_free_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-dual-reservation"
    redis = _TimingRedis()

    async def capacity(*, services: Any) -> int:
        _ = services
        return 1

    async def ready(
        _redis: Any,
        _limit: int,
        **_kwargs: Any,
    ) -> list[str]:
        return [task_id]

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        queue_claim,
        "resolve_image_queue_capacity",
        capacity,
    )
    monkeypatch.setattr(queue_claim, "ready_queued_generation_ids", ready)
    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    reserved = await queue_claim.reserve_image_queue_slot(
        redis,
        task_id,
        dual_race=True,
        services=generation_services,
    )

    assert reserved is not None
    sentinel = queue_claim.dual_race_sentinel_name(task_id)
    reservation_key = queue_claim._image_queue_reservation_token_key(task_id)
    reservation_token = redis.strings[reservation_key]
    assert reserved.name == sentinel
    assert redis.strings[queue.image_task_provider_key(task_id)] == sentinel
    assert sentinel in redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]

    await queue_claim.release_image_queue_slot(
        redis,
        task_id=task_id,
        provider_name=sentinel,
        reservation_token=reservation_token,
        services=generation_services,
    )

    assert reservation_key not in redis.strings
    assert queue.image_task_provider_key(task_id) not in redis.strings
    assert sentinel not in redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]
    assert ("release", reservation_token) in redis.fenced_mutations


@pytest.mark.asyncio
async def test_stale_worker_finally_cannot_delete_takeover_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-race"
    provider_name = "provider-shared"
    old_lease = "worker-old:lease"
    new_lease = "worker-new:lease"
    old_reservation = "reservation-old"
    new_reservation = "reservation-new"
    redis = _TimingRedis()
    task_lease_key = f"task:{task_id}:lease"
    task_provider_key = queue.image_task_provider_key(task_id)
    provider_key = queue.image_provider_active_key(provider_name)
    reservation_key = queue_claim._image_queue_reservation_token_key(task_id)
    old_expiry = time.time() + 30.0
    new_expiry = time.time() + 90.0

    await redis.set(task_lease_key, old_lease)
    await redis.set(task_provider_key, provider_name)
    await redis.set(reservation_key, old_reservation)
    await redis.zadd(provider_key, {task_id: old_expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: old_expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)
    redis.block_release_token = old_lease
    old_cleanup = asyncio.create_task(
        queue_claim.release_generation_runtime_resources(
            redis,
            task_id=task_id,
            lease_token=old_lease,
            provider_name=provider_name,
            reservation_token=old_reservation,
            clear_avoided_providers=False,
            services=generation_services,
        )
    )
    await asyncio.wait_for(redis.blocked_release_started.wait(), timeout=1.0)

    await redis.set(task_lease_key, new_lease)
    await redis.set(task_provider_key, provider_name)
    await redis.set(reservation_key, new_reservation)
    await redis.zadd(provider_key, {task_id: new_expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: new_expiry})

    redis.allow_blocked_release.set()
    await asyncio.wait_for(old_cleanup, timeout=1.0)

    assert redis.strings[task_lease_key] == new_lease
    assert redis.strings[task_provider_key] == provider_name
    assert redis.strings[reservation_key] == new_reservation
    assert redis.zsets[provider_key][task_id] == new_expiry
    assert redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY][task_id] == new_expiry
    assert (old_lease, 0) in redis.reservation_release_results
    assert ("release", old_lease) not in redis.fenced_mutations


@pytest.mark.asyncio
async def test_old_cleanup_cannot_adopt_current_reservation_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-after-takeover"
    provider_name = "provider-shared"
    old_lease = "worker-old:lease"
    new_lease = "worker-new:lease"
    new_reservation = "reservation-new"
    redis = _TimingRedis()
    task_lease_key = f"task:{task_id}:lease"
    task_provider_key = queue.image_task_provider_key(task_id)
    provider_key = queue.image_provider_active_key(provider_name)
    reservation_key = queue_claim._image_queue_reservation_token_key(task_id)
    expiry = time.time() + 90.0

    await redis.set(task_lease_key, new_lease)
    await redis.set(task_provider_key, provider_name)
    await redis.set(reservation_key, new_reservation)
    await redis.zadd(provider_key, {task_id: expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    await queue_claim.release_generation_runtime_resources(
        redis,
        task_id=task_id,
        lease_token=old_lease,
        provider_name=provider_name,
        clear_avoided_providers=False,
        services=generation_services,
    )

    assert redis.strings[task_lease_key] == new_lease
    assert redis.strings[task_provider_key] == provider_name
    assert redis.strings[reservation_key] == new_reservation
    assert redis.zsets[provider_key][task_id] == expiry
    assert redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY][task_id] == expiry
    assert (old_lease, 0) in redis.reservation_release_results


@pytest.mark.asyncio
async def test_unfenced_cleanup_cannot_release_tokenized_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-unfenced"
    provider_name = "provider-owned"
    reservation_token = "reservation-owned"
    redis = _TimingRedis()
    task_provider_key = queue.image_task_provider_key(task_id)
    provider_key = queue.image_provider_active_key(provider_name)
    reservation_key = queue_claim._image_queue_reservation_token_key(task_id)
    expiry = time.time() + 60.0

    await redis.set(task_provider_key, provider_name)
    await redis.set(reservation_key, reservation_token)
    await redis.zadd(provider_key, {task_id: expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    await queue_claim.release_image_queue_slot(
        redis,
        task_id=task_id,
        provider_name=provider_name,
        services=generation_services,
    )

    assert redis.strings[task_provider_key] == provider_name
    assert redis.strings[reservation_key] == reservation_token
    assert redis.zsets[provider_key][task_id] == expiry
    assert redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY][task_id] == expiry


@pytest.mark.asyncio
async def test_matching_lease_token_releases_legacy_formatted_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-owned"
    provider_name = "provider-owned"
    lease_token = "worker-owned:lease"
    redis = _TimingRedis()
    task_provider_key = queue.image_task_provider_key(task_id)
    provider_key = queue.image_provider_active_key(provider_name)
    legacy_lock_key = queue.image_provider_lock_key(provider_name)
    expiry = time.time() + 60.0

    await redis.set(f"task:{task_id}:lease", lease_token)
    await redis.set(task_provider_key, provider_name)
    await redis.set(legacy_lock_key, task_id)
    await redis.zadd(provider_key, {task_id: expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    await queue_claim.release_image_queue_slot(
        redis,
        task_id=task_id,
        provider_name=provider_name,
        lease_token=lease_token,
        services=generation_services,
    )

    assert task_provider_key not in redis.strings
    assert legacy_lock_key not in redis.strings
    assert task_id not in redis.zsets[provider_key]
    assert task_id not in redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]
    assert (lease_token, 1) in redis.reservation_release_results


@pytest.mark.asyncio
async def test_reservation_token_releases_after_worker_lease_was_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-after-lease"
    provider_name = "provider-retry"
    reservation_token = "reservation-retry"
    redis = _TimingRedis()
    task_provider_key = queue.image_task_provider_key(task_id)
    provider_key = queue.image_provider_active_key(provider_name)
    reservation_key = queue_claim._image_queue_reservation_token_key(task_id)
    expiry = time.time() + 60.0

    await redis.set(task_provider_key, provider_name)
    await redis.set(reservation_key, reservation_token)
    await redis.zadd(provider_key, {task_id: expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    await queue_claim.release_image_queue_slot(
        redis,
        task_id=task_id,
        provider_name=provider_name,
        lease_token="already-released-worker-lease",
        reservation_token=reservation_token,
        services=generation_services,
    )

    assert task_provider_key not in redis.strings
    assert reservation_key not in redis.strings
    assert task_id not in redis.zsets[provider_key]
    assert task_id not in redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]


@pytest.mark.asyncio
async def test_reservation_token_releases_dual_race_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-dual"
    sentinel = queue_claim.dual_race_sentinel_name(task_id)
    reservation_token = "reservation-dual"
    redis = _TimingRedis()
    task_provider_key = queue.image_task_provider_key(task_id)
    reservation_key = queue_claim._image_queue_reservation_token_key(task_id)
    expiry = time.time() + 60.0

    await redis.set(task_provider_key, sentinel)
    await redis.set(reservation_key, reservation_token)
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {sentinel: expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    await queue_claim.release_image_queue_slot(
        redis,
        task_id=task_id,
        provider_name=sentinel,
        reservation_token=reservation_token,
        services=generation_services,
    )

    assert task_provider_key not in redis.strings
    assert reservation_key not in redis.strings
    assert sentinel not in redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]


@pytest.mark.asyncio
async def test_unmarked_legacy_reservation_keeps_cleanup_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = "gen-release-legacy"
    provider_name = "provider-legacy"
    redis = _TimingRedis()
    task_provider_key = queue.image_task_provider_key(task_id)
    provider_key = queue.image_provider_active_key(provider_name)
    expiry = time.time() + 60.0

    await redis.set(task_provider_key, provider_name)
    await redis.zadd(provider_key, {task_id: expiry})
    await redis.zadd(queue.IMAGE_QUEUE_ACTIVE_KEY, {task_id: expiry})

    async def no_kick(_redis: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(queue_claim, "kick_image_queue", no_kick)

    await queue_claim.release_image_queue_slot(
        redis,
        task_id=task_id,
        provider_name=provider_name,
        services=generation_services,
    )

    assert task_provider_key not in redis.strings
    assert task_id not in redis.zsets[provider_key]
    assert task_id not in redis.zsets[queue.IMAGE_QUEUE_ACTIVE_KEY]


@pytest.mark.asyncio
async def test_watch_only_lock_fails_closed_before_queue_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _WatchOnlyRedis()

    async def capacity(*, services: Any) -> int:
        _ = services
        return 1

    monkeypatch.setattr(
        queue_claim,
        "resolve_image_queue_capacity",
        capacity,
    )

    with pytest.raises(
        UpstreamError,
        match="reservation requires Redis EVAL",
    ) as exc_info:
        await queue_claim.reserve_image_queue_slot(
            redis,
            "gen-watch-only",
            provider_override=SimpleNamespace(
                name="provider-watch",
                image_concurrency=1,
            ),
            services=generation_services,
        )

    assert exc_info.value.error_code == EC.LOCAL_QUEUE_FULL.value
    assert redis.reservation_writes == 0
    assert redis.store == {}


class _ConflictingPipeline:
    """WATCH 事务：前 ``conflicts`` 次 execute 抛 WatchError，之后成功。"""

    def __init__(self, redis: _ConflictingWatchRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    async def watch(self, _key: str) -> None:
        return None

    async def get(self, key: str) -> str | None:
        return self.redis.store.get(key)

    def multi(self) -> None:
        return None

    def pexpire(self, key: str, ttl_ms: int) -> None:
        self.commands.append(("pexpire", (key, ttl_ms)))

    def delete(self, key: str) -> None:
        self.commands.append(("delete", (key,)))

    async def execute(self) -> list[int]:
        self.redis.executes += 1
        if self.redis.executes <= self.redis.conflicts:
            raise WatchError("owner changed")
        return [1 for _command in self.commands]

    async def reset(self) -> None:
        self.redis.resets += 1


class _ConflictingWatchRedis:
    eval = None

    def __init__(self, conflicts: int) -> None:
        self.conflicts = conflicts
        self.executes = 0
        self.resets = 0
        self.store: dict[str, str] = {}

    def pipeline(self, *, transaction: bool = True) -> _ConflictingPipeline:
        assert transaction is True
        return _ConflictingPipeline(self)


@pytest.mark.asyncio
async def test_watch_fallback_survives_four_consecutive_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审计 D-15：重试上限 3 在高并发下太容易耗尽并把任务拒成 LOCAL_QUEUE_FULL。"""
    redis = _ConflictingWatchRedis(conflicts=4)
    redis.store[queue_lock.IMAGE_QUEUE_LOCK_KEY] = "token-1"
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(queue_lock.asyncio, "sleep", record_sleep)

    assert await queue_lock._renew_watch(redis, "token-1") is True
    assert redis.executes == 5
    assert redis.resets == 5
    # 每次冲突之后都退避，且退避是递增的（指数 + 抖动，抖动区间不重叠）。
    assert len(delays) == 4
    assert all(delay > 0 for delay in delays)
    assert delays == sorted(delays)
    assert sum(delays) < queue_lock._renew_interval()


@pytest.mark.asyncio
async def test_watch_fallback_release_backs_off_between_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _ConflictingWatchRedis(conflicts=4)
    redis.store[queue_lock.IMAGE_QUEUE_LOCK_KEY] = "token-1"
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(queue_lock.asyncio, "sleep", record_sleep)

    assert await queue_lock._release_watch(redis, "token-1") is True
    assert redis.executes == 5
    assert len(delays) == 4


@pytest.mark.asyncio
async def test_watch_fallback_still_fails_closed_when_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = _ConflictingWatchRedis(conflicts=99)
    redis.store[queue_lock.IMAGE_QUEUE_LOCK_KEY] = "token-1"

    async def record_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(queue_lock.asyncio, "sleep", record_sleep)

    with pytest.raises(UpstreamError) as exc_info:
        await queue_lock._renew_watch(redis, "token-1")

    assert exc_info.value.error_code == EC.LOCAL_QUEUE_FULL.value
    assert redis.executes == queue_lock.FALLBACK_RETRIES


@pytest.mark.asyncio
async def test_generation_release_lease_failure_is_logged_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """审计 D-14：释放失败只打 debug，生产默认不打印，租约泄漏无从排查。"""

    class ExplodingRedis:
        async def eval(self, *_args: Any) -> int:
            raise RuntimeError("redis down")

    with caplog.at_level(
        logging.WARNING,
        logger=lease.logger.name,
    ):
        await lease.release_lease(ExplodingRedis(), "gen-9", "worker-1:token-1")

    records = [
        record
        for record in caplog.records
        if "generation lease release failed" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "gen-9" in records[0].getMessage()
    assert "worker-1:token-1" in records[0].getMessage()
