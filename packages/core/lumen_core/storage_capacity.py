from __future__ import annotations

import asyncio
import fcntl
import inspect
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


DEFAULT_MIN_STORAGE_FREE_BYTES = 512 * 1024 * 1024

_REDIS_RESERVE_LUA = """
local leases = KEYS[1]
local weights = KEYS[2]
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local lease_id = ARGV[1]
local requested = tonumber(ARGV[2])
local available = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local expires_ms = now_ms + ttl_ms

local expired = redis.call('ZRANGEBYSCORE', leases, '-inf', now_ms)
if #expired > 0 then
  redis.call('ZREM', leases, unpack(expired))
  redis.call('HDEL', weights, unpack(expired))
end

local reserved = 0
local values = redis.call('HVALS', weights)
for _, value in ipairs(values) do
  reserved = reserved + tonumber(value)
end

if requested > available or reserved + requested > available then
  return {0, reserved, available}
end

redis.call('ZADD', leases, expires_ms, lease_id)
redis.call('HSET', weights, lease_id, requested)
local ttl = math.max(60, math.ceil(ttl_ms / 1000) * 2)
redis.call('EXPIRE', leases, ttl)
redis.call('EXPIRE', weights, ttl)
return {1, reserved + requested, available}
"""

_REDIS_RENEW_LUA = """
if redis.call('HEXISTS', KEYS[2], ARGV[1]) == 0 then
  return 0
end
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local ttl_ms = tonumber(ARGV[2])
redis.call('ZADD', KEYS[1], now_ms + ttl_ms, ARGV[1])
local ttl = math.max(60, math.ceil(ttl_ms / 1000) * 2)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return 1
"""

_REDIS_RESIZE_LUA = """
local leases = KEYS[1]
local weights = KEYS[2]
local server_time = redis.call('TIME')
local now_ms = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local lease_id = ARGV[1]
local requested = tonumber(ARGV[2])
local available = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local expired = redis.call('ZRANGEBYSCORE', leases, '-inf', now_ms)
if #expired > 0 then
  redis.call('ZREM', leases, unpack(expired))
  redis.call('HDEL', weights, unpack(expired))
end

local current = redis.call('HGET', weights, lease_id)
if current == false then
  return {-1, 0, available}
end

local reserved = 0
local values = redis.call('HVALS', weights)
for _, value in ipairs(values) do
  reserved = reserved + tonumber(value)
end
local resized_total = reserved - tonumber(current) + requested
if requested > available or resized_total > available then
  return {0, reserved, available}
end

redis.call('HSET', weights, lease_id, requested)
redis.call('ZADD', leases, now_ms + ttl_ms, lease_id)
local ttl = math.max(60, math.ceil(ttl_ms / 1000) * 2)
redis.call('EXPIRE', leases, ttl)
redis.call('EXPIRE', weights, ttl)
return {1, resized_total, available}
"""

_REDIS_RELEASE_LUA = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('HDEL', KEYS[2], ARGV[1])
return 1
"""


class StorageCapacityExceeded(RuntimeError):
    pass


class StorageCapacityUnavailable(RuntimeError):
    pass


class StorageCapacityLeasePort(Protocol):
    async def renew(self) -> bool: ...

    async def resize(self, bytes_required: int) -> bool: ...

    async def release(self) -> None: ...


class StorageCapacityPort(Protocol):
    async def reserve(self, bytes_required: int) -> StorageCapacityLeasePort: ...


def storage_usage_path(root: Path) -> Path:
    current = root
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def available_storage_bytes(root: Path, *, minimum_free_bytes: int) -> int:
    usage = shutil.disk_usage(storage_usage_path(root))
    return max(0, int(usage.free) - max(0, minimum_free_bytes))


def ensure_storage_free_space(
    root: Path,
    incoming_bytes: int,
    *,
    minimum_free_bytes: int,
) -> None:
    required = max(0, incoming_bytes)
    if (
        available_storage_bytes(
            root,
            minimum_free_bytes=minimum_free_bytes,
        )
        < required
    ):
        raise StorageCapacityExceeded("not enough free storage")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class StorageCapacityLimits:
    minimum_free_bytes: int
    lease_ttl_seconds: int


class _RedisStorageCapacityLease:
    def __init__(
        self,
        capacity: RedisStorageCapacity,
        lease_id: str,
    ) -> None:
        self._capacity = capacity
        self.lease_id = lease_id
        self._released = False

    async def renew(self) -> bool:
        if self._released:
            return False
        return await self._capacity._renew(self.lease_id)

    async def resize(self, bytes_required: int) -> bool:
        if self._released:
            return False
        return await self._capacity._resize(self.lease_id, bytes_required)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._capacity._release(self.lease_id)


class RedisStorageCapacity:
    def __init__(
        self,
        redis: Any,
        root: str | Path,
        limits: StorageCapacityLimits,
        *,
        namespace: str = "lumen:image-storage:capacity",
    ) -> None:
        self.redis = redis
        self.root = Path(root).resolve()
        self.limits = limits
        self.leases_key = f"{namespace}:leases"
        self.weights_key = f"{namespace}:weights"

    async def reserve(self, bytes_required: int) -> _RedisStorageCapacityLease:
        requested = max(0, int(bytes_required))
        available = await asyncio.to_thread(
            available_storage_bytes,
            self.root,
            minimum_free_bytes=self.limits.minimum_free_bytes,
        )
        lease_id = uuid.uuid4().hex
        result = await _resolve(
            self.redis.eval(
                _REDIS_RESERVE_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
                str(requested),
                str(available),
                str(self.limits.lease_ttl_seconds * 1000),
            )
        )
        if int(result[0]) != 1:
            raise StorageCapacityExceeded("image storage capacity exhausted")
        return _RedisStorageCapacityLease(self, lease_id)

    async def _renew(self, lease_id: str) -> bool:
        result = await _resolve(
            self.redis.eval(
                _REDIS_RENEW_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
                str(self.limits.lease_ttl_seconds * 1000),
            )
        )
        return int(result) == 1

    async def _resize(self, lease_id: str, bytes_required: int) -> bool:
        requested = max(0, int(bytes_required))
        available = await asyncio.to_thread(
            available_storage_bytes,
            self.root,
            minimum_free_bytes=self.limits.minimum_free_bytes,
        )
        result = await _resolve(
            self.redis.eval(
                _REDIS_RESIZE_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
                str(requested),
                str(available),
                str(self.limits.lease_ttl_seconds * 1000),
            )
        )
        status = int(result[0])
        if status == 0:
            raise StorageCapacityExceeded("image storage capacity exhausted")
        return status == 1

    async def _release(self, lease_id: str) -> None:
        await _resolve(
            self.redis.eval(
                _REDIS_RELEASE_LUA,
                2,
                self.leases_key,
                self.weights_key,
                lease_id,
            )
        )


class _FileStorageCapacityLease:
    def __init__(
        self,
        capacity: FileStorageCapacity,
        lease_id: str,
    ) -> None:
        self._capacity = capacity
        self.lease_id = lease_id
        self._released = False

    async def renew(self) -> bool:
        if self._released:
            return False
        return await asyncio.to_thread(self._capacity._renew_sync, self.lease_id)

    async def resize(self, bytes_required: int) -> bool:
        if self._released:
            return False
        return await asyncio.to_thread(
            self._capacity._resize_sync,
            self.lease_id,
            bytes_required,
        )

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await asyncio.to_thread(self._capacity._release_sync, self.lease_id)


class FileStorageCapacity:
    """Crash-tolerant local fallback shared by all processes on one filesystem."""

    def __init__(
        self,
        root: str | Path,
        limits: StorageCapacityLimits,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root).resolve()
        self.limits = limits
        self.clock = clock
        self.state_dir = self.root / ".lumen-capacity"
        self.lock_path = self.state_dir / "storage.lock"
        self.state_path = self.state_dir / "storage-leases.json"

    def _ensure_state_dir(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.lstat()
        if not self.state_dir.is_dir() or self.state_dir.is_symlink():
            raise StorageCapacityUnavailable("unsafe storage capacity state directory")
        if info.st_mode & 0o077:
            self.state_dir.chmod(0o700)

    def _read_state(self) -> dict[str, dict[str, int | float]]:
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.state_path, flags)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StorageCapacityUnavailable(
                "invalid storage capacity lease state"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise StorageCapacityUnavailable("invalid storage capacity lease state")
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                value = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            raise StorageCapacityUnavailable(
                "invalid storage capacity lease state"
            ) from exc
        finally:
            if fd >= 0:
                os.close(fd)
        leases = value.get("leases") if isinstance(value, dict) else None
        if not isinstance(leases, dict):
            raise StorageCapacityUnavailable("invalid storage capacity lease state")
        result: dict[str, dict[str, int | float]] = {}
        for lease_id, payload in leases.items():
            if not isinstance(lease_id, str) or not isinstance(payload, dict):
                continue
            bytes_reserved = payload.get("bytes")
            expires_at = payload.get("expires_at")
            if isinstance(bytes_reserved, int) and isinstance(
                expires_at,
                (int, float),
            ):
                result[lease_id] = {
                    "bytes": max(0, bytes_reserved),
                    "expires_at": float(expires_at),
                }
        return result

    def _write_state(self, leases: dict[str, dict[str, int | float]]) -> None:
        fd, raw_path = tempfile.mkstemp(
            prefix=".storage-leases-",
            suffix=".tmp",
            dir=str(self.state_dir),
        )
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "leases": leases},
                    handle,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.state_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _with_locked_state(
        self,
        operation: Callable[
            [dict[str, dict[str, int | float]], float],
            tuple[Any, bool],
        ],
    ) -> Any:
        self._ensure_state_dir()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise StorageCapacityUnavailable(
                "storage capacity lock is unavailable"
            ) from exc
        with os.fdopen(lock_fd, "a+b") as lock_handle:
            info = os.fstat(lock_handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StorageCapacityUnavailable("storage capacity lock is unsafe")
            os.fchmod(lock_handle.fileno(), 0o600)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                now = self.clock()
                leases = self._read_state()
                expired = [
                    lease_id
                    for lease_id, payload in leases.items()
                    if float(payload["expires_at"]) <= now
                ]
                for lease_id in expired:
                    leases.pop(lease_id, None)
                result, changed = operation(leases, now)
                if expired or changed:
                    self._write_state(leases)
                return result
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _reserve_sync(self, bytes_required: int) -> str:
        requested = max(0, int(bytes_required))

        def reserve(
            leases: dict[str, dict[str, int | float]],
            now: float,
        ) -> tuple[str, bool]:
            available = available_storage_bytes(
                self.root,
                minimum_free_bytes=self.limits.minimum_free_bytes,
            )
            reserved = sum(int(payload["bytes"]) for payload in leases.values())
            if requested > available or reserved + requested > available:
                raise StorageCapacityExceeded("image storage capacity exhausted")
            lease_id = uuid.uuid4().hex
            leases[lease_id] = {
                "bytes": requested,
                "expires_at": now + self.limits.lease_ttl_seconds,
            }
            return lease_id, True

        return str(self._with_locked_state(reserve))

    def _renew_sync(self, lease_id: str) -> bool:
        def renew(
            leases: dict[str, dict[str, int | float]],
            now: float,
        ) -> tuple[bool, bool]:
            payload = leases.get(lease_id)
            if payload is None:
                return False, False
            payload["expires_at"] = now + self.limits.lease_ttl_seconds
            return True, True

        return bool(self._with_locked_state(renew))

    def _resize_sync(self, lease_id: str, bytes_required: int) -> bool:
        requested = max(0, int(bytes_required))

        def resize(
            leases: dict[str, dict[str, int | float]],
            now: float,
        ) -> tuple[bool, bool]:
            payload = leases.get(lease_id)
            if payload is None:
                return False, False
            available = available_storage_bytes(
                self.root,
                minimum_free_bytes=self.limits.minimum_free_bytes,
            )
            reserved_without_current = sum(
                int(item["bytes"])
                for current_id, item in leases.items()
                if current_id != lease_id
            )
            if (
                requested > available
                or reserved_without_current + requested > available
            ):
                raise StorageCapacityExceeded("image storage capacity exhausted")
            payload["bytes"] = requested
            payload["expires_at"] = now + self.limits.lease_ttl_seconds
            return True, True

        return bool(self._with_locked_state(resize))

    def _release_sync(self, lease_id: str) -> None:
        def release(
            leases: dict[str, dict[str, int | float]],
            _now: float,
        ) -> tuple[None, bool]:
            return None, leases.pop(lease_id, None) is not None

        self._with_locked_state(release)

    async def reserve(self, bytes_required: int) -> _FileStorageCapacityLease:
        lease_id = await asyncio.to_thread(self._reserve_sync, bytes_required)
        return _FileStorageCapacityLease(self, lease_id)


class ResilientStorageCapacity:
    def __init__(
        self,
        primary: RedisStorageCapacity,
        fallback: FileStorageCapacity,
        *,
        degraded_policy: str,
    ) -> None:
        if degraded_policy not in {"fail_closed", "scaled_local"}:
            raise ValueError("invalid image storage degraded capacity policy")
        self.primary = primary
        self.fallback = fallback
        self.degraded_policy = degraded_policy

    async def reserve(self, bytes_required: int) -> Any:
        if self.degraded_policy == "scaled_local":
            # Select one ledger for the process lifetime. Dynamically falling
            # back after Redis has already granted leases would let the file
            # ledger reserve the same physical bytes a second time.
            return await self.fallback.reserve(bytes_required)
        try:
            return await self.primary.reserve(bytes_required)
        except StorageCapacityExceeded:
            raise
        except Exception as exc:
            raise StorageCapacityUnavailable(
                "image storage capacity unavailable"
            ) from exc


def build_storage_capacity(
    redis: Any,
    storage_root: str | Path,
    *,
    minimum_free_bytes: int,
    lease_ttl_seconds: int,
    degraded_policy: str,
) -> ResilientStorageCapacity:
    limits = StorageCapacityLimits(
        minimum_free_bytes=minimum_free_bytes,
        lease_ttl_seconds=lease_ttl_seconds,
    )
    return ResilientStorageCapacity(
        RedisStorageCapacity(redis, storage_root, limits),
        FileStorageCapacity(storage_root, limits),
        degraded_policy=degraded_policy,
    )
