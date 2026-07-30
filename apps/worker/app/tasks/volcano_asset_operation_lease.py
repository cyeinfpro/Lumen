"""Redis lease, fencing, and entrypoint mechanics for Volcano asset operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from arq import Retry

from . import volcano_asset_create as _create_parts
from .volcano_asset_runtime import (
    VolcanoAssetRuntimeContext,
    VolcanoAssetRuntimeSlot,
    VolcanoAssetRuntimeView,
)

logger = logging.getLogger(__name__)

_JOB_NAME = "process_volcano_asset_operation"
_OPERATION_LOCK_TTL_SECONDS = 10 * 60
_OPERATION_LOCK_RENEW_INTERVAL_SECONDS = 60
_OPERATION_FENCING_KEY_PREFIX = "video-assets:operation-fencing:"
_RELEASE_OPERATION_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW_OPERATION_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_ALLOCATE_OPERATION_FENCING_SCRIPT = """
-- volcano-operation-fence-allocate
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return 0
end
local fencing = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return fencing
"""
_CONFIRM_OPERATION_FENCE_SCRIPT = """
-- volcano-operation-fence-confirm
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return -1
end
if tostring(redis.call('GET', KEYS[2]) or '') ~= ARGV[2] then
  return -2
end
if ARGV[3] ~= '' then
  local raw = redis.call('GET', KEYS[3])
  if not raw then
    return -3
  end
  local ok, operation = pcall(cjson.decode, raw)
  if not ok or type(operation) ~= 'table' then
    return -4
  end
  if tonumber(operation['attempt'] or 1) ~= tonumber(ARGV[3]) then
    return -5
  end
end
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""
_SET_FENCED_OPERATION_SCRIPT = """
-- volcano-operation-fence-set
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return -1
end
if tostring(redis.call('GET', KEYS[2]) or '') ~= ARGV[2] then
  return -2
end
local raw = redis.call('GET', KEYS[3])
if not raw then
  return -3
end
local ok, current = pcall(cjson.decode, raw)
if not ok or type(current) ~= 'table' then
  return -4
end
if tonumber(current['attempt'] or 1) ~= tonumber(ARGV[3]) then
  return -5
end
local current_status = tostring(current['status'] or '')
if (current_status == 'succeeded' or current_status == 'failed')
    and ARGV[7] ~= '1' then
  return -6
end
redis.call('SET', KEYS[3], ARGV[4], 'EX', ARGV[5])
redis.call('EXPIRE', KEYS[1], ARGV[6])
return 1
"""

_RUNTIME = VolcanoAssetRuntimeSlot(
    owner=__name__,
    dependencies=frozenset(
        {
            "VOLCANO_ASSET_OPERATION_TTL_SECONDS",
            "VolcanoAssetRedisUnavailable",
            "_ALLOCATE_OPERATION_FENCING_SCRIPT",
            "_CONFIRM_OPERATION_FENCE_SCRIPT",
            "_JOB_NAME",
            "_LeaseLostError",
            "_OPERATION_LOCK_RENEW_INTERVAL_SECONDS",
            "_OPERATION_LOCK_TTL_SECONDS",
            "_RELEASE_OPERATION_LOCK_SCRIPT",
            "_SET_FENCED_OPERATION_SCRIPT",
            "_SuccessPersistenceError",
            "_process_locked",
            "_retry_redis_call",
            "_utc_iso",
            "volcano_asset_operation_key",
        }
    ),
)


def install_runtime(context: VolcanoAssetRuntimeContext) -> None:
    _RUNTIME.install(context)


def _runtime() -> VolcanoAssetRuntimeView:
    return _RUNTIME.get()


@dataclass
class _OperationFence:
    operation_id: str
    lock_key: str
    lock_token: str
    fencing_key: str
    fencing: int
    lease_lost: asyncio.Event
    lease_deadline: float
    attempt: int | None = None

    def bind(self, operation: dict[str, Any]) -> None:
        attempt = max(1, int(operation.get("attempt") or 1))
        if self.attempt is None:
            self.attempt = attempt
            return
        if self.attempt != attempt:
            self.mark_lost()
            raise _runtime()._LeaseLostError(
                "Volcano asset operation attempt fence was superseded"
            )

    def mark_confirmed(self) -> None:
        self.lease_deadline = (
            time.monotonic() + _runtime()._OPERATION_LOCK_TTL_SECONDS
        )

    def mark_lost(self) -> None:
        self.lease_lost.set()

    def expired(self) -> bool:
        return time.monotonic() >= self.lease_deadline

    def details(self) -> dict[str, Any]:
        if self.attempt is None:
            raise RuntimeError("Volcano asset operation fence is not bound")
        return {
            "lock_token": self.lock_token,
            "attempt": self.attempt,
            "fencing": self.fencing,
        }


@dataclass
class _OperationPersistence:
    redis: Any
    fence: _OperationFence

    def bind(self, operation: dict[str, Any]) -> None:
        self.fence.bind(operation)

    async def confirm(self) -> None:
        await _confirm_operation_fence(self.redis, self.fence)

    async def update(
        self,
        operation: dict[str, Any],
        **changes: Any,
    ) -> None:
        candidate = {
            **operation,
            **changes,
            "updated_at": _runtime()._utc_iso(),
        }
        await self.replace(operation, candidate)

    async def replace(
        self,
        operation: dict[str, Any],
        candidate: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        self.bind(candidate)
        await _set_fenced_operation(
            self.redis,
            self.fence,
            candidate,
            terminal=terminal,
        )
        operation.clear()
        operation.update(candidate)


def _operation_fencing_key(operation_id: str) -> str:
    return f"{_OPERATION_FENCING_KEY_PREFIX}{operation_id}"


async def _allocate_operation_fencing(
    redis: Any,
    *,
    lock_key: str,
    lock_token: str,
    fencing_key: str,
) -> int:
    runtime = _runtime()
    fencing = await runtime._retry_redis_call(
        lambda: redis.eval(
            runtime._ALLOCATE_OPERATION_FENCING_SCRIPT,
            2,
            lock_key,
            fencing_key,
            lock_token,
            runtime.VOLCANO_ASSET_OPERATION_TTL_SECONDS,
        )
    )
    return max(0, int(fencing or 0))


def _raise_lost_fence(fence: _OperationFence) -> None:
    fence.mark_lost()
    raise _runtime()._LeaseLostError("Volcano asset operation lease was lost")


async def _confirm_operation_fence(
    redis: Any,
    fence: _OperationFence,
) -> None:
    runtime = _runtime()
    if fence.lease_lost.is_set() or fence.expired():
        _raise_lost_fence(fence)
    try:
        confirmed = await runtime._retry_redis_call(
            lambda: redis.eval(
                runtime._CONFIRM_OPERATION_FENCE_SCRIPT,
                3,
                fence.lock_key,
                fence.fencing_key,
                runtime.volcano_asset_operation_key(fence.operation_id),
                fence.lock_token,
                fence.fencing,
                "" if fence.attempt is None else fence.attempt,
                runtime._OPERATION_LOCK_TTL_SECONDS,
            )
        )
    except runtime.VolcanoAssetRedisUnavailable:
        if fence.expired():
            _raise_lost_fence(fence)
        raise
    if int(confirmed or 0) != 1:
        _raise_lost_fence(fence)
    fence.mark_confirmed()


async def _set_fenced_operation(
    redis: Any,
    fence: _OperationFence,
    operation: dict[str, Any],
    *,
    terminal: bool,
) -> None:
    runtime = _runtime()
    if fence.attempt is None:
        fence.bind(operation)
    if fence.lease_lost.is_set() or fence.expired():
        _raise_lost_fence(fence)
    payload = json.dumps(
        operation,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        stored = await runtime._retry_redis_call(
            lambda: redis.eval(
                runtime._SET_FENCED_OPERATION_SCRIPT,
                3,
                fence.lock_key,
                fence.fencing_key,
                runtime.volcano_asset_operation_key(fence.operation_id),
                fence.lock_token,
                fence.fencing,
                fence.attempt,
                payload,
                runtime.VOLCANO_ASSET_OPERATION_TTL_SECONDS,
                runtime._OPERATION_LOCK_TTL_SECONDS,
                1 if terminal else 0,
            )
        )
    except runtime.VolcanoAssetRedisUnavailable:
        if fence.expired():
            _raise_lost_fence(fence)
        raise
    if int(stored or 0) != 1:
        _raise_lost_fence(fence)
    fence.mark_confirmed()


async def _renew_operation_lock(
    redis: Any,
    lock_key: str,
    lock_token: str,
) -> bool:
    runtime = _runtime()
    renewed = await runtime._retry_redis_call(
        lambda: redis.eval(
            _RENEW_OPERATION_LOCK_SCRIPT,
            1,
            lock_key,
            lock_token,
            runtime._OPERATION_LOCK_TTL_SECONDS,
        )
    )
    return bool(renewed)


async def _operation_lock_heartbeat(
    persistence: _OperationPersistence,
) -> None:
    runtime = _runtime()
    while True:
        remaining = persistence.fence.lease_deadline - time.monotonic()
        if remaining <= 0:
            persistence.fence.mark_lost()
            return
        await asyncio.sleep(
            min(runtime._OPERATION_LOCK_RENEW_INTERVAL_SECONDS, remaining)
        )
        if persistence.fence.expired():
            persistence.fence.mark_lost()
            return
        try:
            await persistence.confirm()
        except runtime.VolcanoAssetRedisUnavailable:
            logger.warning(
                "video_asset.operation_lock_renew_unavailable",
                exc_info=True,
            )
            continue
        except runtime._LeaseLostError:
            return


async def _confirm_operation_lock(
    persistence: _OperationPersistence,
) -> None:
    await persistence.confirm()


async def _schedule_lock_recovery(
    redis: Any,
    operation_id: str,
    expected_attempt: int | None,
    expected_delivery_generation: int | None,
    lock_key: str,
) -> bool:
    runtime = _runtime()
    current_token = await runtime._retry_redis_call(lambda: redis.get(lock_key))
    if isinstance(current_token, bytes):
        current_token = current_token.decode("utf-8", errors="replace")
    if not isinstance(current_token, str) or not current_token:
        return False
    token_digest = hashlib.sha256(current_token.encode("utf-8")).hexdigest()[:12]
    attempt_key = expected_attempt if expected_attempt is not None else "current"
    delivery_key = (
        expected_delivery_generation
        if expected_delivery_generation is not None
        else "current"
    )
    marker_key = (
        f"video-assets:operation-lock-recovery:{operation_id}:"
        f"{attempt_key}:{delivery_key}:{token_digest}"
    )
    marker_token = secrets.token_hex(12)
    claimed = await runtime._retry_redis_call(
        lambda: redis.set(
            marker_key,
            marker_token,
            nx=True,
            ex=runtime._OPERATION_LOCK_TTL_SECONDS,
        )
    )
    if not claimed:
        return False
    try:
        await runtime._retry_redis_call(
            lambda: redis.enqueue_job(
                runtime._JOB_NAME,
                operation_id,
                expected_attempt,
                expected_delivery_generation,
                _defer_by=timedelta(
                    seconds=runtime._OPERATION_LOCK_TTL_SECONDS + 5
                ),
                _job_id=(
                    f"volcano-asset:{operation_id}:lock-recovery:"
                    f"{token_digest}:{marker_token[:8]}"
                ),
            )
        )
    except runtime.VolcanoAssetRedisUnavailable:
        with suppress(runtime.VolcanoAssetRedisUnavailable):
            await runtime._retry_redis_call(
                lambda: redis.eval(
                    runtime._RELEASE_OPERATION_LOCK_SCRIPT,
                    1,
                    marker_key,
                    marker_token,
                )
            )
        raise
    return True


async def process_volcano_asset_operation(
    ctx: dict[str, Any],
    operation_id: str,
    expected_attempt: int | None = None,
    expected_delivery_generation: int | None = None,
) -> dict[str, Any]:
    """Run one operation under a Redis lease to prevent duplicate submits."""
    runtime = _runtime()
    redis = ctx.get("redis")
    if redis is None:
        raise RuntimeError("Redis is required for Volcano asset operations")
    lock_key = f"video-assets:operation-lock:{operation_id}"
    lock_token = secrets.token_hex(16)
    try:
        locked = await runtime._retry_redis_call(
            lambda: redis.set(
                lock_key,
                lock_token,
                nx=True,
                ex=runtime._OPERATION_LOCK_TTL_SECONDS,
            )
        )
    except runtime.VolcanoAssetRedisUnavailable as exc:
        raise Retry(defer=5 + random.uniform(0, 2)) from exc
    if not locked:
        try:
            recovery_scheduled = await _schedule_lock_recovery(
                redis,
                operation_id,
                expected_attempt,
                expected_delivery_generation,
                lock_key,
            )
        except runtime.VolcanoAssetRedisUnavailable as exc:
            raise Retry(defer=5 + random.uniform(0, 2)) from exc
        return {
            "status": "locked",
            "operation_id": operation_id,
            "retry_after_seconds": runtime._OPERATION_LOCK_TTL_SECONDS + 5,
            "recovery_scheduled": recovery_scheduled,
        }
    lease_lost = asyncio.Event()
    fencing_key = _operation_fencing_key(operation_id)
    try:
        fencing = await _allocate_operation_fencing(
            redis,
            lock_key=lock_key,
            lock_token=lock_token,
            fencing_key=fencing_key,
        )
    except runtime.VolcanoAssetRedisUnavailable as exc:
        with suppress(runtime.VolcanoAssetRedisUnavailable):
            await runtime._retry_redis_call(
                lambda: redis.eval(
                    runtime._RELEASE_OPERATION_LOCK_SCRIPT,
                    1,
                    lock_key,
                    lock_token,
                )
            )
        raise Retry(defer=5 + random.uniform(0, 2)) from exc
    if fencing <= 0:
        lease_lost.set()
        raise Retry(defer=5 + random.uniform(0, 2))
    persistence = _OperationPersistence(
        redis=redis,
        fence=_OperationFence(
            operation_id=operation_id,
            lock_key=lock_key,
            lock_token=lock_token,
            fencing_key=fencing_key,
            fencing=fencing,
            lease_lost=lease_lost,
            lease_deadline=time.monotonic() + runtime._OPERATION_LOCK_TTL_SECONDS,
        ),
    )
    heartbeat = asyncio.create_task(_operation_lock_heartbeat(persistence))
    try:
        try:
            return await runtime._process_locked(
                ctx,
                operation_id,
                expected_attempt,
                expected_delivery_generation,
                persistence=persistence,
            )
        except _create_parts._IntentLockBusyError as exc:
            delay = exc.retry_after_seconds
            raise Retry(defer=delay + random.uniform(0, delay / 4)) from exc
        except (
            runtime._LeaseLostError,
            runtime._SuccessPersistenceError,
            runtime.VolcanoAssetRedisUnavailable,
        ) as exc:
            job_try = max(1, int(ctx.get("job_try") or 1))
            delay = min(60.0, 5.0 * (2 ** min(job_try - 1, 3)))
            raise Retry(defer=delay + random.uniform(0, delay / 4)) from exc
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        try:
            await runtime._retry_redis_call(
                lambda: redis.eval(
                    runtime._RELEASE_OPERATION_LOCK_SCRIPT,
                    1,
                    lock_key,
                    lock_token,
                )
            )
        except runtime.VolcanoAssetRedisUnavailable:
            logger.warning(
                "video_asset.operation_lock_release_failed operation_id=%s",
                operation_id,
                exc_info=True,
            )


__all__ = ["process_volcano_asset_operation"]
