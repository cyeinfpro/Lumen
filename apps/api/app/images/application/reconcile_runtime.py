"""Background lifecycle for image artifact recovery."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ...config import settings
from ...db import SessionLocal
from ...redis_client import get_redis
from ..adapters.filesystem_store import FileSystemArtifactStore
from ..adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from .reconcile_policy import ImageArtifactReconciler


logger = logging.getLogger(__name__)

_RECONCILE_LEASE_KEY = "lock:image-artifact-reconciler"
_RECONCILE_LEASE_TTL_SECONDS = 300
_RECONCILE_LEASE_RENEW_SECONDS = 90.0
_RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def build_image_artifact_reconciler() -> ImageArtifactReconciler:
    return ImageArtifactReconciler(
        repository=SQLAlchemyImageRepository(SessionLocal),
        artifacts=FileSystemArtifactStore(settings.storage_root),
    )


async def _acquire_reconcile_lease(redis: Any) -> str | None:
    token = secrets.token_hex(16)
    acquired = await redis.set(
        _RECONCILE_LEASE_KEY,
        token,
        ex=_RECONCILE_LEASE_TTL_SECONDS,
        nx=True,
    )
    return token if acquired else None


async def _renew_reconcile_lease(redis: Any, token: str) -> bool:
    result = await redis.eval(
        _RENEW_LEASE_LUA,
        1,
        _RECONCILE_LEASE_KEY,
        token,
        str(_RECONCILE_LEASE_TTL_SECONDS),
    )
    return int(result or 0) == 1


async def _release_reconcile_lease(redis: Any, token: str) -> None:
    await redis.eval(
        _RELEASE_LEASE_LUA,
        1,
        _RECONCILE_LEASE_KEY,
        token,
    )


@asynccontextmanager
async def _reconcile_lease(redis: Any) -> AsyncIterator[bool]:
    try:
        token = await _acquire_reconcile_lease(redis)
    except Exception:
        # Fail closed: running independently in every Uvicorn worker is worse
        # than delaying repair until Redis coordination recovers.
        logger.warning("image artifact reconcile lease unavailable", exc_info=True)
        yield False
        return
    if token is None:
        yield False
        return

    stop = asyncio.Event()
    lost = asyncio.Event()

    async def renew_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=_RECONCILE_LEASE_RENEW_SECONDS,
                )
                return
            except TimeoutError:
                pass
            try:
                if not await _renew_reconcile_lease(redis, token):
                    lost.set()
                    return
            except Exception:
                lost.set()
                logger.warning(
                    "image artifact reconcile lease renewal failed",
                    exc_info=True,
                )
                return

    renew_task = asyncio.create_task(
        renew_loop(),
        name="image-artifact-reconcile-lease-renew",
    )
    try:
        yield True
    finally:
        stop.set()
        await asyncio.gather(renew_task, return_exceptions=True)
        try:
            await _release_reconcile_lease(redis, token)
        except Exception:
            logger.warning(
                "image artifact reconcile lease release failed",
                exc_info=True,
            )
        if lost.is_set():
            logger.error("image artifact reconcile lease was lost during a sweep")


async def run_image_artifact_reconciler_once() -> int:
    redis = get_redis()
    async with _reconcile_lease(redis) as acquired:
        if not acquired:
            return 0
        stats = await build_image_artifact_reconciler().run_once()
        repaired = stats.marked_ready + stats.marked_failed + stats.rebuilt_reference
        if repaired or stats.deleted_staged or stats.deferred:
            logger.info(
                "image artifact reconciliation scanned=%d repaired=%d "
                "deleted_staged=%d deferred=%d",
                stats.scanned,
                repaired,
                stats.deleted_staged,
                stats.deferred,
            )
        return repaired


async def image_artifact_reconciler_loop(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = 60.0,
) -> None:
    """Keep artifact rows/files convergent after process or commit failures."""
    while not stop_event.is_set():
        try:
            await run_image_artifact_reconciler_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("image artifact reconciliation iteration failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=interval_seconds,
            )
        except TimeoutError:
            pass
