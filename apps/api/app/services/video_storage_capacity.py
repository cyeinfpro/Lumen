"""Shared storage-capacity reservations for API-side video writes."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from lumen_core.capacity_leases import CapacityLeaseGuard, maintained_capacity_lease
from lumen_core.storage_capacity import StorageCapacityPort, build_storage_capacity

from ..config import settings
from ..redis_client import get_redis
from .poster_styles.capacity import (
    PosterTaggingCapacityUnavailable,
    RedisCapacityLease,
)


logger = logging.getLogger(__name__)

_VIDEO_TRANSCODE_DEFAULT_CONCURRENCY = 1
_VIDEO_TRANSCODE_MAX_CONCURRENCY = 4
_VIDEO_TRANSCODE_DEFAULT_WAIT_SECONDS = 2.0
_VIDEO_TRANSCODE_MAX_WAIT_SECONDS = 300.0
_VIDEO_TRANSCODE_DEFAULT_LEASE_TTL_SECONDS = 180
_VIDEO_TRANSCODE_MAX_LEASE_TTL_SECONDS = 600
VIDEO_REFERENCE_STORAGE_QUOTA_BYTES = 1024 * 1024 * 1024


def _bounded_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %d", name, raw, default)
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default %.1f", name, raw, default)
        return default
    return min(maximum, max(minimum, parsed))


class VideoTranscodeCapacityUnavailable(RuntimeError):
    """No bounded reference-video transcode slot became available."""


class VideoReferenceStorageQuotaExceeded(RuntimeError):
    """A reference-video artifact would increase usage beyond the user quota."""


def enforce_video_reference_storage_quota(
    *,
    current_bytes: int,
    replaced_bytes: int,
    added_bytes: int,
    limit_bytes: int = VIDEO_REFERENCE_STORAGE_QUOTA_BYTES,
) -> int:
    current = max(0, int(current_bytes))
    replaced = min(current, max(0, int(replaced_bytes)))
    added = max(0, int(added_bytes))
    projected = current - replaced + added
    if projected > max(0, int(limit_bytes)) and projected > current:
        raise VideoReferenceStorageQuotaExceeded(
            "reference video storage quota exceeded"
        )
    return projected


class VideoTranscodeCapacityManager:
    """Bound CPU-heavy transcodes globally and per user."""

    def __init__(
        self,
        redis: Any,
        *,
        limit: int,
        wait_timeout_seconds: float,
        lease_ttl_seconds: int,
    ) -> None:
        if limit <= 0:
            raise ValueError("video transcode concurrency must be positive")
        if wait_timeout_seconds <= 0:
            raise ValueError("video transcode wait timeout must be positive")
        if lease_ttl_seconds <= 0:
            raise ValueError("video transcode lease TTL must be positive")
        self.redis = redis
        self.limit = limit
        self.wait_timeout_seconds = wait_timeout_seconds
        self.lease_ttl_seconds = lease_ttl_seconds

    @asynccontextmanager
    async def hold(self, *, user_id: str) -> AsyncIterator[None]:
        global_capacity = RedisCapacityLease(
            self.redis,
            limit=self.limit,
            ttl_seconds=self.lease_ttl_seconds,
            wait_timeout_seconds=self.wait_timeout_seconds,
            key_prefix="lumen:video-reference-transcode:global",
        )
        user_digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        user_capacity = RedisCapacityLease(
            self.redis,
            limit=1,
            ttl_seconds=self.lease_ttl_seconds,
            wait_timeout_seconds=self.wait_timeout_seconds,
            key_prefix=f"lumen:video-reference-transcode:user:{user_digest}",
        )
        global_context = global_capacity.hold()
        user_context = user_capacity.hold()
        global_entered = False
        user_entered = False
        try:
            try:
                await global_context.__aenter__()
                global_entered = True
                await user_context.__aenter__()
                user_entered = True
            except PosterTaggingCapacityUnavailable as exc:
                raise VideoTranscodeCapacityUnavailable(
                    "video transcode distributed capacity is exhausted"
                ) from exc
            except Exception as exc:
                raise VideoTranscodeCapacityUnavailable(
                    "video transcode distributed capacity is unavailable"
                ) from exc
            yield
        finally:
            if user_entered:
                try:
                    await user_context.__aexit__(None, None, None)
                except Exception:
                    logger.warning(
                        "video transcode per-user capacity release failed",
                        exc_info=True,
                    )
            if global_entered:
                try:
                    await global_context.__aexit__(None, None, None)
                except Exception:
                    logger.warning(
                        "video transcode global capacity release failed",
                        exc_info=True,
                    )


class VideoStorageCapacityManager:
    def __init__(
        self,
        capacity: StorageCapacityPort,
        *,
        lease_ttl_seconds: float,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("video storage lease TTL must be positive")
        self.capacity = capacity
        self.lease_ttl_seconds = lease_ttl_seconds

    @asynccontextmanager
    async def reserve(
        self,
        bytes_required: int,
    ) -> AsyncIterator[CapacityLeaseGuard]:
        lease = await self.capacity.reserve(max(0, int(bytes_required)))
        async with maintained_capacity_lease(
            lease,
            ttl_seconds=self.lease_ttl_seconds,
        ) as guard:
            await guard.assert_owned()
            yield guard
            await guard.assert_owned()


def _degraded_policy() -> str:
    configured = settings.image_upload_capacity_degraded_policy.strip()
    if configured:
        return configured
    if settings.app_env.strip().lower() in {"dev", "development", "local", "test"}:
        return "scaled_local"
    return "fail_closed"


def build_video_storage_capacity_manager() -> VideoStorageCapacityManager:
    return VideoStorageCapacityManager(
        build_storage_capacity(
            get_redis(),
            settings.storage_root,
            minimum_free_bytes=settings.minimum_storage_free_bytes,
            lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
            degraded_policy=_degraded_policy(),
        ),
        lease_ttl_seconds=settings.image_upload_lease_ttl_seconds,
    )


def build_video_transcode_capacity_manager() -> VideoTranscodeCapacityManager:
    return VideoTranscodeCapacityManager(
        get_redis(),
        limit=_bounded_int_env(
            "LUMEN_VIDEO_REFERENCE_TRANSCODE_CONCURRENCY",
            _VIDEO_TRANSCODE_DEFAULT_CONCURRENCY,
            minimum=1,
            maximum=_VIDEO_TRANSCODE_MAX_CONCURRENCY,
        ),
        wait_timeout_seconds=_bounded_float_env(
            "LUMEN_VIDEO_REFERENCE_TRANSCODE_WAIT_SECONDS",
            _VIDEO_TRANSCODE_DEFAULT_WAIT_SECONDS,
            minimum=0.1,
            maximum=_VIDEO_TRANSCODE_MAX_WAIT_SECONDS,
        ),
        lease_ttl_seconds=_bounded_int_env(
            "LUMEN_VIDEO_REFERENCE_TRANSCODE_LEASE_TTL_SECONDS",
            _VIDEO_TRANSCODE_DEFAULT_LEASE_TTL_SECONDS,
            minimum=30,
            maximum=_VIDEO_TRANSCODE_MAX_LEASE_TTL_SECONDS,
        ),
    )
