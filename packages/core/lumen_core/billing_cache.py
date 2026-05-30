"""Async billing cache helpers shared by API and worker code."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserApiCredential, UserWallet


_WINDOW_INCREMENT_LUA = """
local key = KEYS[1]
local now_ts = tonumber(ARGV[1]) or 0
local amount = tonumber(ARGV[2]) or 0
local limit_5h = tonumber(ARGV[3]) or 0
local limit_1d = tonumber(ARGV[4]) or 0
local limit_7d = tonumber(ARGV[5]) or 0
local expire_sec = tonumber(ARGV[6]) or 0

local labels = {"5h", "1d", "7d"}
local ttls = {5 * 3600, 24 * 3600, 7 * 24 * 3600}
local limits = {limit_5h, limit_1d, limit_7d}

for i, label in ipairs(labels) do
  local started_field = "window_" .. label .. "_started_at_unix"
  local usage_field = "usage_" .. label
  local started = tonumber(redis.call("HGET", key, started_field) or "0") or 0
  if started <= 0 or now_ts - started >= ttls[i] then
    redis.call("HSET", key, started_field, now_ts)
    redis.call("HSET", key, usage_field, amount)
  else
    redis.call("HINCRBY", key, usage_field, amount)
  end
  redis.call("HSET", key, "limit_" .. label .. "_micro", limits[i])
end

if expire_sec > 0 then
  redis.call("EXPIRE", key, expire_sec)
end
return 1
"""


def _hash_value(payload: dict[Any, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    raw_key = key.encode("utf-8")
    if raw_key in payload:
        return payload.get(raw_key)
    return None


@dataclass(frozen=True)
class WindowUsage:
    used_micro: int = 0
    limit_micro: int = 0
    resets_at: datetime | None = None


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class BillingCacheService:
    def __init__(
        self,
        redis: Any | None = None,
        *,
        balance_ttl_sec: int = 300,
        window_ttl_sec: int = 300,
        worker_count: int = 10,
        queue_size: int = 1000,
    ) -> None:
        self.redis = redis
        self.balance_ttl_sec = balance_ttl_sec
        self.window_ttl_sec = window_ttl_sec
        self.worker_count = worker_count
        self._queue: asyncio.Queue[tuple[str, tuple[Any, ...], dict[str, Any]]] = (
            asyncio.Queue(maxsize=queue_size)
        )
        self._locks: dict[str, _LockEntry] = {}
        self._workers: list[asyncio.Task[None]] = []

    @asynccontextmanager
    async def _lock(self, key: str) -> AsyncIterator[None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = _LockEntry(asyncio.Lock())
            self._locks[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users <= 0 and self._locks.get(key) is entry:
                self._locks.pop(key, None)

    def _balance_key(self, user_id: str) -> str:
        return f"lumen:billing:balance:{user_id}"

    def _window_key(self, key_id: str) -> str:
        return f"lumen:billing:rl:{key_id}"

    async def start_workers(self) -> None:
        if self._workers or self.redis is None:
            return
        for _ in range(self.worker_count):
            self._workers.append(asyncio.create_task(self._worker_loop()))

    async def stop_workers(self) -> None:
        workers = self._workers
        self._workers = []
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _worker_loop(self) -> None:
        while True:
            op, args, kwargs = await self._queue.get()
            try:
                if op == "decr":
                    await self._apply_decr(*args, **kwargs)
                elif op == "window":
                    await self._apply_window_increment(*args, **kwargs)
            finally:
                self._queue.task_done()

    async def _apply_decr(self, user_id: str, amount_micro: int) -> None:
        if self.redis is None:
            return
        amount = int(amount_micro)
        if amount <= 0:
            return
        try:
            await self.redis.decrby(self._balance_key(user_id), amount)
        except Exception:
            return

    async def set_balance(self, user_id: str, balance_micro: int) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(
                self._balance_key(user_id),
                int(balance_micro),
                ex=self.balance_ttl_sec,
            )
        except Exception:
            return

    async def _apply_window_increment(
        self,
        key_id: str,
        amount_micro: int,
        limits: dict[str, int] | None = None,
        now: datetime | None = None,
    ) -> None:
        if self.redis is None:
            return
        amount = int(amount_micro)
        if amount <= 0:
            return
        current = now or datetime.now(timezone.utc)
        ts = int(current.timestamp())
        limits = limits or {}
        try:
            key = self._window_key(key_id)
            await self.redis.eval(
                _WINDOW_INCREMENT_LUA,
                1,
                key,
                ts,
                amount,
                int(limits.get("5h") or 0),
                int(limits.get("1d") or 0),
                int(limits.get("7d") or 0),
                7 * 24 * 3600 + self.window_ttl_sec,
            )
        except Exception:
            return

    async def get_balance(self, db: AsyncSession, user_id: str) -> int:
        if self.redis is not None:
            try:
                raw = await self.redis.get(self._balance_key(user_id))
                if raw is not None:
                    return int(raw)
            except Exception:
                pass
        async with self._lock(user_id):
            row = (
                await db.execute(
                    select(UserWallet.balance_micro).where(
                        UserWallet.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            balance = int(row or 0)
        if self.redis is not None:
            try:
                await self.redis.set(
                    self._balance_key(user_id), balance, ex=self.balance_ttl_sec
                )
            except Exception:
                pass
        return balance

    async def queue_deduct(self, user_id: str, micro: int) -> None:
        if self.redis is None:
            return
        amount = int(micro)
        if amount <= 0:
            return
        try:
            self._queue.put_nowait(("decr", (user_id, amount), {}))
        except asyncio.QueueFull:
            try:
                await self._apply_decr(user_id, amount)
            except Exception:
                return

    async def deduct_sync(self, db: AsyncSession, user_id: str, micro: int) -> int:
        amount = int(micro)
        if amount <= 0:
            row = (
                await db.execute(
                    select(UserWallet.balance_micro).where(
                        UserWallet.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            return int(row or 0)
        row = (
            await db.execute(
                select(UserWallet)
                .where(UserWallet.user_id == user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            row = UserWallet(user_id=user_id)
            db.add(row)
            await db.flush()
        row.balance_micro = max(0, row.balance_micro - amount)
        row.version += 1
        await db.flush()
        if self.redis is not None:
            try:
                await self.redis.set(
                    self._balance_key(user_id),
                    row.balance_micro,
                    ex=self.balance_ttl_sec,
                )
            except Exception:
                pass
        return int(row.balance_micro)

    async def invalidate(self, user_id: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.delete(self._balance_key(user_id))
        except Exception:
            return

    async def get_window_usage(
        self,
        key_id: str,
        window: str = "5h",
        *,
        limit_micro: int | None = None,
    ) -> WindowUsage:
        ttl_map = {"5h": 5 * 3600, "1d": 24 * 3600, "7d": 7 * 24 * 3600}
        ttl = ttl_map.get(window)
        if ttl is None:
            return WindowUsage()
        if self.redis is not None:
            try:
                payload = await self.redis.hgetall(self._window_key(key_id))
            except Exception:
                payload = {}
            if payload:
                try:
                    limit = int(
                        limit_micro
                        if limit_micro is not None
                        else _hash_value(payload, f"limit_{window}_micro") or 0
                    )
                    used = int(_hash_value(payload, f"usage_{window}") or 0)
                    started = int(
                        _hash_value(payload, f"window_{window}_started_at_unix") or 0
                    )
                    resets = (
                        datetime.fromtimestamp(started + ttl, tz=timezone.utc)
                        if started > 0
                        else None
                    )
                    return WindowUsage(
                        used_micro=max(0, used),
                        limit_micro=max(0, limit),
                        resets_at=resets,
                    )
                except Exception:
                    return WindowUsage(limit_micro=max(0, int(limit_micro or 0)))
        return WindowUsage(limit_micro=max(0, int(limit_micro or 0)))

    async def queue_window_increment(
        self,
        key_id: str,
        micro: int,
        limits: dict[str, int] | None = None,
    ) -> None:
        if self.redis is None:
            return
        amount = int(micro)
        if amount <= 0:
            return
        try:
            self._queue.put_nowait(("window", (key_id, amount, limits), {}))
        except asyncio.QueueFull:
            await self._apply_window_increment(key_id, amount, limits)

    async def credential_limits(
        self,
        db: AsyncSession,
        key_id: str | None,
    ) -> dict[str, int]:
        if not key_id:
            return {"5h": 0, "1d": 0, "7d": 0}
        row = (
            await db.execute(
                select(
                    UserApiCredential.limit_5h_micro,
                    UserApiCredential.limit_1d_micro,
                    UserApiCredential.limit_7d_micro,
                ).where(UserApiCredential.id == key_id)
            )
        ).one_or_none()
        if row is None:
            return {"5h": 0, "1d": 0, "7d": 0}
        return {
            "5h": int(row[0] or 0),
            "1d": int(row[1] or 0),
            "7d": int(row[2] or 0),
        }

    async def evaluate_rate_limits(
        self,
        db: AsyncSession,
        key_id: str | None,
        projected_micro: int,
    ) -> tuple[bool, str | None, WindowUsage]:
        if not key_id or projected_micro <= 0:
            return True, None, WindowUsage()
        limits = await self.credential_limits(db, key_id)
        for window in ("5h", "1d", "7d"):
            limit = limits.get(window, 0)
            if limit <= 0:
                continue
            usage = await self.get_window_usage(key_id, window, limit_micro=limit)
            if usage.used_micro + int(projected_micro) > limit:
                return False, window, usage
        return True, None, WindowUsage()
