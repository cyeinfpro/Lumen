"""Background lifecycle for image artifact recovery."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from lumen_core.storage_capacity import build_storage_capacity

from ...config import settings
from ...db import SessionLocal
from ...redis_client import get_redis
from ..adapters.filesystem_store import FileSystemArtifactStore
from ..adapters.sqlalchemy_repository import SQLAlchemyImageRepository
from .reconcile_policy import ImageArtifactReconciler, ReconcileLeaseLost
from .storage_maintenance import sweep_orphan_image_files


logger = logging.getLogger(__name__)

_RECONCILE_LEASE_KEY = "lock:image-artifact-reconciler"
_RECONCILE_LEASE_TTL_SECONDS = 300
_RECONCILE_LEASE_RENEW_SECONDS = 90.0
_ORPHAN_SWEEP_CURSOR_KEY = "cursor:image-orphan-sweep:v1"
_ORPHAN_SWEEP_MAX_FILES = 500
_ORPHAN_SWEEP_MAX_ENTRIES = 5_000
_ORPHAN_SWEEP_MAX_BYTES = 10 * 1024 * 1024 * 1024
_ORPHAN_SWEEP_MAX_SECONDS = 2.0
_ORPHAN_SWEEP_MINIMUM_AGE_SECONDS = 3600.0
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


@dataclass
class ReconcileLeaseGuard:
    token: str
    fence: int
    ttl_seconds: float
    safety_seconds: float
    lost: asyncio.Event
    _renew: Callable[[], Awaitable[bool]]
    _monotonic: Callable[[], float]
    _last_confirmed_at: float

    @classmethod
    def create(
        cls,
        *,
        token: str,
        fence: int,
        ttl_seconds: float,
        renew: Callable[[], Awaitable[bool]],
        safety_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ReconcileLeaseGuard:
        if ttl_seconds <= 0:
            raise ValueError("reconcile lease TTL must be positive")
        if fence <= 0:
            raise ValueError("reconcile lease fence must be positive")
        safety = ttl_seconds / 4 if safety_seconds is None else safety_seconds
        if safety < 0 or safety >= ttl_seconds:
            raise ValueError("reconcile lease safety must be within the TTL")
        return cls(
            token=token,
            fence=fence,
            ttl_seconds=ttl_seconds,
            safety_seconds=safety,
            lost=asyncio.Event(),
            _renew=renew,
            _monotonic=monotonic,
            _last_confirmed_at=monotonic(),
        )

    def mark_lost(self) -> None:
        self.lost.set()

    async def wait_lost(self) -> None:
        await self.lost.wait()

    def _remaining_seconds(self) -> float:
        deadline = self._last_confirmed_at + self.ttl_seconds - self.safety_seconds
        return deadline - self._monotonic()

    async def assert_owned(self) -> None:
        if self.lost.is_set():
            raise ReconcileLeaseLost("image artifact reconcile lease was lost")
        remaining = self._remaining_seconds()
        if remaining <= 0:
            self.mark_lost()
            raise ReconcileLeaseLost("image artifact reconcile lease expired")
        try:
            owned = await asyncio.wait_for(self._renew(), timeout=remaining)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.mark_lost()
            raise ReconcileLeaseLost(
                "image artifact reconcile lease could not be confirmed"
            ) from exc
        if not owned:
            self.mark_lost()
            raise ReconcileLeaseLost("image artifact reconcile lease ownership changed")
        if self.lost.is_set():
            raise ReconcileLeaseLost("image artifact reconcile lease was lost")
        self._last_confirmed_at = self._monotonic()


def build_image_artifact_reconciler() -> ImageArtifactReconciler:
    redis = get_redis()
    configured_policy = settings.image_upload_capacity_degraded_policy.strip()
    degraded_policy = configured_policy or (
        "scaled_local"
        if settings.app_env.strip().lower() in {"dev", "development", "local", "test"}
        else "fail_closed"
    )
    return ImageArtifactReconciler(
        repository=SQLAlchemyImageRepository(SessionLocal),
        artifacts=FileSystemArtifactStore(settings.storage_root),
        storage_capacity=build_storage_capacity(
            redis,
            settings.storage_root,
            minimum_free_bytes=settings.minimum_storage_free_bytes,
            lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
            degraded_policy=degraded_policy,
        ),
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


async def _next_reconcile_fence() -> int:
    return await SQLAlchemyImageRepository(SessionLocal).next_reconcile_fence()


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


def _redis_cursor(value: Any) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) and value else None


async def _run_orphan_image_sweep(
    redis: Any,
    lease_guard: ReconcileLeaseGuard,
) -> None:
    await lease_guard.assert_owned()
    cursor = _redis_cursor(await redis.get(_ORPHAN_SWEEP_CURSOR_KEY))
    async with SessionLocal() as db:
        result = await sweep_orphan_image_files(
            db,
            storage_root=settings.storage_root,
            dry_run=False,
            cursor=cursor,
            max_files=_ORPHAN_SWEEP_MAX_FILES,
            max_entries=_ORPHAN_SWEEP_MAX_ENTRIES,
            max_bytes=_ORPHAN_SWEEP_MAX_BYTES,
            max_seconds=_ORPHAN_SWEEP_MAX_SECONDS,
            minimum_age_seconds=_ORPHAN_SWEEP_MINIMUM_AGE_SECONDS,
            assert_owned=lease_guard.assert_owned,
        )

    await lease_guard.assert_owned()
    budget_exhausted = bool(result.get("budget_exhausted"))
    next_cursor = _redis_cursor(result.get("next_cursor"))
    if next_cursor is not None:
        await redis.set(_ORPHAN_SWEEP_CURSOR_KEY, next_cursor)
    else:
        await redis.delete(_ORPHAN_SWEEP_CURSOR_KEY)

    logger.info(
        "image orphan sweep scanned=%d deleted=%d failed=%d "
        "budget_exhausted=%s cursor=%s next_cursor=%s",
        int(result.get("scanned") or 0),
        int(result.get("deleted") or 0),
        len(result.get("failed") or ()),
        budget_exhausted,
        cursor,
        next_cursor,
    )


@asynccontextmanager
async def _reconcile_lease(
    redis: Any,
) -> AsyncIterator[ReconcileLeaseGuard | None]:
    try:
        token = await _acquire_reconcile_lease(redis)
    except Exception:
        # Fail closed: running independently in every Uvicorn worker is worse
        # than delaying repair until Redis coordination recovers.
        logger.warning("image artifact reconcile lease unavailable", exc_info=True)
        yield None
        return
    if token is None:
        yield None
        return
    try:
        fence = await _next_reconcile_fence()
    except Exception:
        logger.warning("image artifact reconcile fence unavailable", exc_info=True)
        try:
            await _release_reconcile_lease(redis, token)
        except Exception:
            logger.warning(
                "image artifact reconcile lease release failed",
                exc_info=True,
            )
        yield None
        return

    stop = asyncio.Event()
    guard = ReconcileLeaseGuard.create(
        token=token,
        fence=fence,
        ttl_seconds=_RECONCILE_LEASE_TTL_SECONDS,
        renew=lambda: _renew_reconcile_lease(redis, token),
    )

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
                await guard.assert_owned()
            except ReconcileLeaseLost:
                return

    renew_task = asyncio.create_task(
        renew_loop(),
        name="image-artifact-reconcile-lease-renew",
    )
    try:
        yield guard
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
        if guard.lost.is_set():
            logger.error("image artifact reconcile sweep stopped after lease loss")


async def run_image_artifact_reconciler_once() -> int:
    redis = get_redis()
    async with _reconcile_lease(redis) as lease_guard:
        if lease_guard is None:
            return 0
        try:
            stats = await build_image_artifact_reconciler().run_once(
                lease_guard=lease_guard,
            )
        except ReconcileLeaseLost:
            return 0
        repaired = stats.marked_ready + stats.marked_failed + stats.rebuilt_reference
        quarantined_staged = getattr(stats, "quarantined_staged", 0)
        quarantined_rows = getattr(stats, "quarantined_rows", 0)
        if (
            repaired
            or stats.deleted_staged
            or quarantined_staged
            or quarantined_rows
            or stats.deferred
        ):
            logger.info(
                "image artifact reconciliation scanned=%d repaired=%d "
                "deleted_staged=%d quarantined_staged=%d quarantined_rows=%d "
                "deferred=%d",
                stats.scanned,
                repaired,
                stats.deleted_staged,
                quarantined_staged,
                quarantined_rows,
                stats.deferred,
            )
        try:
            await _run_orphan_image_sweep(redis, lease_guard)
        except ReconcileLeaseLost:
            logger.warning("image orphan sweep stopped after reconcile lease loss")
        except Exception:
            logger.exception("image orphan sweep iteration failed")
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
