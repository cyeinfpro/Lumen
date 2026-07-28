from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from .admission import WeightedPermit, renew_weighted_permit
from .errors import LeaseLost
from .queue import (
    IMAGE_QUEUE_ACTIVE_KEY,
    image_inflight_key,
    image_provider_active_key,
    is_dual_race_sentinel,
)
from .runtime_contracts import (
    GENERATION_LEASE_RENEW_S,
    GENERATION_LEASE_TTL_S,
    RELEASE_GENERATION_LEASE_LUA,
)


LEASE_TTL_S = GENERATION_LEASE_TTL_S
LEASE_RENEW_S = GENERATION_LEASE_RENEW_S

RELEASE_LEASE_LUA = RELEASE_GENERATION_LEASE_LUA

RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

LEASE_RENEW_RETRY_S = 1.0
LEASE_EXPIRY_SAFETY_S = 1.0

logger = logging.getLogger(__name__)


async def is_cancelled(redis: Any, task_id: str) -> bool:
    try:
        value = await redis.get(f"task:{task_id}:cancel")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "generation cancel check failed closed task=%s err=%s",
            task_id,
            exc,
        )
        return True
    return bool(value)


async def acquire_lease(
    redis: Any,
    task_id: str,
    worker_token: str,
) -> None:
    ok = await redis.set(
        f"task:{task_id}:lease",
        worker_token,
        ex=LEASE_TTL_S,
        nx=True,
    )
    if not ok:
        raise LeaseLost(f"lease already held task={task_id}")


async def release_lease(
    redis: Any,
    task_id: str,
    worker_token: str,
) -> None:
    try:
        eval_fn = getattr(redis, "eval", None)
        if callable(eval_fn):
            await eval_fn(
                RELEASE_LEASE_LUA,
                1,
                f"task:{task_id}:lease",
                worker_token,
            )
            return
        logger.warning(
            "generation lease release skipped without atomic CAS task=%s",
            task_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "generation lease release failed task=%s worker=%s",
            task_id,
            worker_token,
            exc_info=True,
        )


def _lease_safety_seconds(ttl_s: float) -> float:
    return min(
        max(LEASE_EXPIRY_SAFETY_S, ttl_s / 4.0),
        max(ttl_s / 2.0, 0.001),
    )


def _mark_lease_lost(
    lease_lost: asyncio.Event | None,
    *,
    task_id: str,
    worker_token: str,
    reason: str,
) -> None:
    if lease_lost is not None:
        lease_lost.set()
    logger.error(
        "generation lease lost task=%s worker=%s reason=%s",
        task_id,
        worker_token,
        reason,
    )


async def _refresh_image_queue_ownership(
    redis: Any,
    *,
    task_id: str,
    image_provider_name: str,
    ttl_s: float,
) -> None:
    seconds, microseconds = await redis.time()
    redis_now = float(seconds) + (float(microseconds) / 1_000_000.0)
    new_expiry = redis_now + ttl_s
    if is_dual_race_sentinel(image_provider_name):
        await redis.zadd(
            IMAGE_QUEUE_ACTIVE_KEY,
            {image_provider_name: new_expiry},
        )
        return
    await redis.zadd(IMAGE_QUEUE_ACTIVE_KEY, {task_id: new_expiry})
    await redis.zadd(
        image_provider_active_key(image_provider_name),
        {task_id: new_expiry},
    )


async def _renew_generation_lease_once(
    redis: Any,
    *,
    task_id: str,
    worker_token: str,
    ttl_s: float,
    extra_lease_keys: list[str] | None,
    image_provider_name: str | None,
    weighted_permit: WeightedPermit | None,
) -> bool:
    renewed = await redis.eval(
        RENEW_LEASE_LUA,
        1,
        f"task:{task_id}:lease",
        worker_token,
        LEASE_TTL_S,
    )
    if int(renewed or 0) == 0:
        return False
    for key in extra_lease_keys or []:
        await redis.expire(key, LEASE_TTL_S)
    with suppress(Exception):
        await redis.expire(image_inflight_key(task_id), LEASE_TTL_S * 4)
    if image_provider_name:
        await _refresh_image_queue_ownership(
            redis,
            task_id=task_id,
            image_provider_name=image_provider_name,
            ttl_s=ttl_s,
        )
    if weighted_permit is not None:
        seconds, microseconds = await redis.time()
        redis_now = float(seconds) + (float(microseconds) / 1_000_000.0)
        if not await renew_weighted_permit(
            redis,
            permit=weighted_permit,
            expiry=redis_now + ttl_s,
        ):
            return False
    return True


async def lease_renewer(
    redis: Any,
    task_id: str,
    worker_token: str,
    lease_lost: asyncio.Event | None = None,
    *,
    extra_lease_keys: list[str] | None = None,
    image_provider_name: str | None = None,
    weighted_permit: WeightedPermit | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Renew the worker lease and image queue ownership in lock-step."""
    loop = asyncio.get_running_loop()
    now = monotonic or loop.time
    sleep_fn = sleep or asyncio.sleep
    ttl_s = float(LEASE_TTL_S)
    safety_s = _lease_safety_seconds(ttl_s)
    renew_every_s = min(max(float(LEASE_RENEW_S), 0.0), ttl_s / 3.0, 10.0)
    renewal_deadline = now() + ttl_s - safety_s
    try:
        while True:
            remaining_s = renewal_deadline - now()
            if remaining_s <= 0:
                _mark_lease_lost(
                    lease_lost,
                    task_id=task_id,
                    worker_token=worker_token,
                    reason="renewal_deadline_elapsed",
                )
                return
            try:
                renewed = await asyncio.wait_for(
                    _renew_generation_lease_once(
                        redis,
                        task_id=task_id,
                        worker_token=worker_token,
                        ttl_s=ttl_s,
                        extra_lease_keys=extra_lease_keys,
                        image_provider_name=image_provider_name,
                        weighted_permit=weighted_permit,
                    ),
                    timeout=remaining_s,
                )
                if not renewed:
                    _mark_lease_lost(
                        lease_lost,
                        task_id=task_id,
                        worker_token=worker_token,
                        reason="owner_token_mismatch",
                    )
                    return
                renewal_deadline = now() + ttl_s - safety_s
                await sleep_fn(
                    min(
                        renew_every_s,
                        max(renewal_deadline - now(), 0.0),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                remaining_s = renewal_deadline - now()
                logger.warning(
                    "lease renew failed task=%s err=%s safe_window_remaining_s=%.3f",
                    task_id,
                    exc,
                    max(remaining_s, 0.0),
                )
                if remaining_s <= 0:
                    _mark_lease_lost(
                        lease_lost,
                        task_id=task_id,
                        worker_token=worker_token,
                        reason="renewal_deadline_elapsed_after_failure",
                    )
                    return
                await sleep_fn(min(LEASE_RENEW_RETRY_S, remaining_s))
    except asyncio.CancelledError:
        raise


async def cancel_renewer_task(renewer: asyncio.Task[None] | None) -> None:
    if renewer is None:
        return
    renewer.cancel()
    try:
        await renewer
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug(
            "generation lease renewer cancellation failed",
            exc_info=True,
        )
