"""Video generation worker tasks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from arq.cron import cron
from sqlalchemy import or_, select

from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    EV_VIDEO_FAILED,
    EV_VIDEO_FETCHING,
    EV_VIDEO_PROGRESS,
    EV_VIDEO_SUBMITTED,
    EV_VIDEO_SUCCEEDED,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import Image, Video, VideoGeneration, new_uuid7
from lumen_core.video_providers import (
    parse_video_provider_config_json,
    select_video_provider,
)

from .. import runtime_settings, video_submit_cache
from ..db import SessionLocal
from ..storage import StorageDiskFullError, storage
from ..video_billing import resolve_video_billing
from ..video_events import (
    publish_video_event as _publish,
    publish_video_event_after_commit as _publish_after_commit,
    queue_video_event as _queue_video_event,
)
from ..video_submit_cache import (
    CachedSubmitResult as _CachedSubmitResult,
    cached_submit_provider_kind as _cached_submit_provider_kind,
    cached_submit_provider_name as _cached_submit_provider_name,
    cached_submit_result as _cached_submit_result,
    load_submit_result as _load_submit_result,
    store_submit_result as _store_submit_result,
)
from ..video_upstream import (
    PollResult,
    SubmitResult,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
    adapter_for_provider,
)


logger = logging.getLogger(__name__)

_SUBMIT_RESULT_CACHE_TTL_S = video_submit_cache.SUBMIT_RESULT_CACHE_TTL_S
_LEASE_TTL_S = 120
_LEASE_RENEW_S = 30
_LEASE_RENEW_MAX_TRANSIENT_FAILURES = 3
_POLL_INTERVAL_S = 8
_MAX_POLL_DURATION_S = 30 * 60
_MAX_POLL_COUNT = max(1, _MAX_POLL_DURATION_S // _POLL_INTERVAL_S)
_EXTENDED_POLL_INTERVAL_S = 60
_MAX_PROVIDER_POLL_DURATION_S = 48 * 60 * 60
_MAX_SUBMIT_ATTEMPTS = 4
_SUBMIT_RETRY_DELAYS_S = (8, 24, 60)
_POLL_RETRY_DELAY_S = 12
_RECON_STALE_AFTER_S = 30
_SUBMIT_UNKNOWN_AFTER_S = max(_LEASE_TTL_S * 2, 5 * 60)
_SUBMIT_UNKNOWN_FINALIZE_AFTER_S = 60 * 60
_RECON_LIMIT = 100
_VIDEO_PROVIDER_SLOT_STALE_AFTER_S = _MAX_PROVIDER_POLL_DURATION_S + 5 * 60
_VIDEO_PROVIDER_SLOT_TTL_S = _VIDEO_PROVIDER_SLOT_STALE_AFTER_S + 60 * 60
_VIDEO_PROVIDER_SLOT_PREFIX = "video:provider_slot:"
_VIDEO_PROVIDER_SLOT_LOCK_PREFIX = "video:provider_slot_lock:"
_TERMINAL_STATUSES = {
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
}
_NON_RESUBMIT_STATUSES = {
    *_TERMINAL_STATUSES,
    VideoGenerationStatus.SUBMIT_UNKNOWN.value,
}
_RETRYABLE_VIDEO_ERROR_CODES = {
    "capacity",
    "fetch_failed",
    "provider_error",
    "upstream_network_error",
    "upstream_not_ready",
    "upstream_timeout",
    "upstream_unknown",
}


@dataclass(frozen=True)
class _StoredVideo:
    video: Video
    diagnostics: dict[str, Any]


class _VideoLeaseLost(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _poll_window_exhausted(generation: VideoGeneration, now: datetime) -> bool:
    if generation.poll_count >= _MAX_POLL_COUNT:
        return True
    submitted_at = generation.submitted_at
    if submitted_at is None:
        return False
    return submitted_at + timedelta(seconds=_MAX_POLL_DURATION_S) <= now


def _provider_tracking_window_exhausted(
    generation: VideoGeneration, now: datetime
) -> bool:
    submitted_at = generation.submitted_at
    if submitted_at is None:
        return False
    return submitted_at + timedelta(seconds=_MAX_PROVIDER_POLL_DURATION_S) <= now


def _poll_elapsed_s(generation: VideoGeneration, now: datetime) -> int | None:
    submitted_at = generation.submitted_at
    if submitted_at is None:
        return None
    return int((now - submitted_at).total_seconds())


def _video_exception_code(exc: Exception, *, default: str) -> str:
    if isinstance(exc, VideoUpstreamError):
        value = (exc.error_code or "").strip()
        return value or default
    if isinstance(exc, httpx.TimeoutException) or isinstance(exc, asyncio.TimeoutError):
        return "upstream_timeout"
    if isinstance(exc, httpx.TransportError):
        return "upstream_network_error"
    return default


def _video_exception_message(exc: Exception, *, phase: str) -> str:
    raw = str(exc).strip()
    if raw:
        return raw[:1000]
    code = _video_exception_code(exc, default="provider_unavailable")
    status_code = getattr(exc, "status_code", None)
    suffix = f" status={status_code}" if status_code else ""
    return f"video upstream {phase} failed: {code} ({exc.__class__.__name__}){suffix}"[
        :1000
    ]


def _is_retryable_video_exception(exc: Exception) -> bool:
    if isinstance(exc, VideoUpstreamError):
        if exc.status_code in {408, 409, 425, 429}:
            return True
        if exc.status_code is not None and exc.status_code >= 500:
            return True
        return exc.error_code in _RETRYABLE_VIDEO_ERROR_CODES
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return True
    return isinstance(exc, httpx.TransportError)


def _submit_outcome_unknown(exc: Exception) -> bool:
    if isinstance(
        exc, (httpx.TimeoutException, asyncio.TimeoutError, httpx.TransportError)
    ):
        return True
    if not isinstance(exc, VideoUpstreamError):
        return False
    if exc.status_code in {408, 409}:
        return True
    if exc.status_code is not None and exc.status_code >= 500:
        return True
    return exc.error_code in {"bad_response", "upstream_unknown"}


def _submit_retry_delay_s(attempt: int) -> int:
    index = max(0, min(attempt - 1, len(_SUBMIT_RETRY_DELAYS_S) - 1))
    return _SUBMIT_RETRY_DELAYS_S[index]


def _generation_attempt(generation: VideoGeneration) -> int:
    return int(getattr(generation, "attempt", 0) or 0)


def _generation_diagnostics(generation: VideoGeneration) -> dict[str, Any]:
    raw = getattr(generation, "diagnostics", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _submit_failure_billable_hint(exc: Exception) -> bool | None:
    if _is_retryable_video_exception(exc):
        return None
    if isinstance(exc, VideoUpstreamError) and exc.error_code in {
        "bad_response",
        "upstream_unknown",
    }:
        return None
    return False


def _exception_log_info(exc: Exception):
    return (type(exc), exc, exc.__traceback__)


def _append_bounded_history(
    diagnostics: dict[str, Any], key: str, item: dict[str, Any], *, limit: int = 10
) -> None:
    raw = diagnostics.get(key)
    history = list(raw) if isinstance(raw, list) else []
    history.append(item)
    diagnostics[key] = history[-limit:]


async def _acquire_lease(redis: Any, task_id: str, token: str) -> bool:
    return bool(
        await redis.set(f"video:{task_id}:lease", token, ex=_LEASE_TTL_S, nx=True)
    )


async def _renew_lease(redis: Any, task_id: str, token: str) -> bool | None:
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('EXPIRE', KEYS[1], ARGV[2])
    end
    return 0
    """
    try:
        renewed = await redis.eval(
            lua,
            1,
            f"video:{task_id}:lease",
            token,
            str(_LEASE_TTL_S),
        )
        return int(renewed or 0) == 1
    except Exception:
        logger.warning("video lease renew failed task=%s", task_id, exc_info=True)
        return None


async def _lease_renewer(
    redis: Any,
    task_id: str,
    token: str,
    *,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    transient_failures = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_LEASE_RENEW_S)
            return
        except TimeoutError:
            pass
        renewed = await _renew_lease(redis, task_id, token)
        if renewed is True:
            transient_failures = 0
            continue
        if renewed is False:
            lost.set()
            return
        transient_failures += 1
        if transient_failures >= _LEASE_RENEW_MAX_TRANSIENT_FAILURES:
            lost.set()
            return


async def _lease_active(redis: Any, task_id: str) -> bool:
    try:
        return await redis.get(f"video:{task_id}:lease") is not None
    except Exception:
        logger.warning(
            "video lease status unavailable task=%s; keeping task fenced",
            task_id,
            exc_info=True,
        )
        return True


def _raise_if_video_lease_lost(lease_lost: asyncio.Event, message: str) -> None:
    if lease_lost.is_set():
        raise _VideoLeaseLost(message)


async def _release_lease(redis: Any, task_id: str, token: str) -> None:
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    try:
        await redis.eval(lua, 1, f"video:{task_id}:lease", token)
    except Exception:
        logger.debug("video lease release failed task=%s", task_id, exc_info=True)


def _enqueue_job_id(kind: str, task_id: str, defer_s: int) -> str:
    delay = max(0, int(defer_s or 0))
    due_at = time.time() + delay
    bucket_s = max(1, delay)
    bucket = int(due_at // bucket_s)
    return f"lumen:{kind}:{task_id}:{bucket_s}:{bucket}"


async def _enqueue_poll(
    redis: Any, task_id: str, *, defer_s: int = _POLL_INTERVAL_S
) -> None:
    await redis.enqueue_job(
        "run_video_poll",
        task_id,
        _defer_by=defer_s,
        _job_id=_enqueue_job_id("video_poll", task_id, defer_s),
    )


async def _enqueue_submit(
    redis: Any, task_id: str, *, defer_s: int = _POLL_INTERVAL_S
) -> None:
    await redis.enqueue_job(
        "run_video_generation",
        task_id,
        _defer_by=defer_s,
        _job_id=_enqueue_job_id("video_generation", task_id, defer_s),
    )


async def _enqueue_cached_submit_recovery(
    redis: Any,
    task_id: str,
    *,
    defer_s: int,
) -> bool:
    try:
        cached_submit = await _load_submit_result(redis, task_id)
    except Exception:
        logger.warning(
            "video cached submit lookup failed task=%s",
            task_id,
            exc_info=True,
        )
        return False
    if cached_submit is None:
        return False
    try:
        await _enqueue_submit(redis, task_id, defer_s=defer_s)
    except Exception:
        logger.warning(
            "video cached submit recovery enqueue failed task=%s",
            task_id,
            exc_info=True,
        )
        return False
    return True


async def _provider_config():
    raw_video = await runtime_settings.resolve("video.providers")
    raw_shared = await runtime_settings.resolve("providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return providers


async def _provider_for_generation(generation: VideoGeneration):
    providers = await _provider_config()
    if generation.provider_name:
        for provider in providers:
            if provider.name == generation.provider_name:
                return provider
    provider = select_video_provider(
        providers,
        model=generation.model,
        action=generation.action,
    )
    if provider is None:
        raise RuntimeError("no enabled video provider supports this model/action")
    return provider


async def _acquire_provider_slot(
    redis: Any, provider_name: str, concurrency: int, task_id: str
) -> bool:
    lock_key = f"{_VIDEO_PROVIDER_SLOT_LOCK_PREFIX}{provider_name}"
    lock_token = f"{task_id}:{new_uuid7()}"
    ok = await redis.set(lock_key, lock_token, ex=10, nx=True)
    if not ok:
        return False
    zkey = f"{_VIDEO_PROVIDER_SLOT_PREFIX}{provider_name}"
    try:
        cutoff = time.time() - _VIDEO_PROVIDER_SLOT_STALE_AFTER_S
        await redis.zremrangebyscore(zkey, 0, cutoff)
        if await redis.zscore(zkey, task_id) is not None:
            await redis.zadd(zkey, {task_id: time.time()})
            await redis.expire(zkey, _VIDEO_PROVIDER_SLOT_TTL_S)
            return True
        active = await redis.zcard(zkey)
        if int(active or 0) >= max(1, int(concurrency)):
            return False
        await redis.zadd(zkey, {task_id: time.time()})
        await redis.expire(zkey, _VIDEO_PROVIDER_SLOT_TTL_S)
        return True
    finally:
        await _release_slot_lock(redis, lock_key, lock_token)


async def _release_slot_lock(redis: Any, lock_key: str, token: str) -> None:
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    try:
        await redis.eval(lua, 1, lock_key, token)
    except Exception:
        logger.debug("video provider slot lock release failed key=%s", lock_key)


async def _release_provider_slot(redis: Any, provider_name: str, task_id: str) -> None:
    try:
        await redis.zrem(f"{_VIDEO_PROVIDER_SLOT_PREFIX}{provider_name}", task_id)
    except Exception:
        logger.warning(
            "video provider slot release failed provider=%s task=%s",
            provider_name,
            task_id,
            exc_info=True,
        )


async def _input_image_bytes(
    session, generation: VideoGeneration
) -> tuple[bytes | None, str | None]:
    if generation.action != "i2v":
        return None, None
    key = generation.input_image_storage_key
    mime: str | None = None
    if generation.input_image_id:
        img = (
            await session.execute(
                select(Image).where(Image.id == generation.input_image_id)
            )
        ).scalar_one_or_none()
        if img is not None:
            mime = img.mime
            key = key or img.storage_key
    if not key:
        raise RuntimeError("i2v input image storage key missing")
    return await storage.aget_bytes(key), mime


def _input_image_url(generation: VideoGeneration) -> str | None:
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    raw = request.get("input_image_url")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


async def _reference_media_bytes(
    generation: VideoGeneration,
) -> list[VideoReferenceMedia]:
    raw = (generation.upstream_request or {}).get("reference_media")
    if generation.action != "reference":
        return []
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("reference media snapshot missing")
    result: list[VideoReferenceMedia] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in {"image", "video", "audio"}:
            continue
        url = item.get("url")
        clean_url = url.strip() if isinstance(url, str) else ""
        label = item.get("label")
        clean_label = (
            label.strip() if isinstance(label, str) and label.strip() else None
        )
        ref_id = item.get("ref_id")
        clean_ref_id = (
            ref_id.strip().lower()
            if isinstance(ref_id, str) and ref_id.strip()
            else None
        )
        mime = item.get("mime") if isinstance(item.get("mime"), str) else None
        upstream_mime = item.get("upstream_reference_mime")
        if isinstance(upstream_mime, str) and upstream_mime.strip():
            mime = upstream_mime.strip()
        storage_key = item.get("upstream_reference_storage_key") or item.get(
            "storage_key"
        )
        clean_storage_key = (
            storage_key.strip() if isinstance(storage_key, str) else None
        )
        data: bytes | None = None
        if kind == "image" and clean_storage_key:
            if clean_url:
                # The public URL is the primary upstream input; these bytes are
                # only an optimization for the data-URL fallback retry. A
                # missing/expired variant must not fail a generation the URL can
                # still satisfy on its own.
                try:
                    data = await storage.aget_bytes(clean_storage_key)
                except Exception:
                    logger.warning(
                        "reference image variant bytes unavailable; "
                        "falling back to url storage_key=%s",
                        clean_storage_key,
                        exc_info=True,
                    )
                    data = None
            else:
                data = await storage.aget_bytes(clean_storage_key)
        if clean_url:
            result.append(
                VideoReferenceMedia(  # type: ignore[arg-type]
                    kind=kind,
                    data=data,
                    mime=mime,
                    url=clean_url,
                    label=clean_label,
                    ref_id=clean_ref_id,
                )
            )
            continue
        if kind == "audio":
            raise RuntimeError("reference audio snapshot missing public URL")
        if kind == "video":
            raise RuntimeError("reference video snapshot missing public URL")
        if not clean_storage_key:
            raise RuntimeError("reference media storage key missing")
        result.append(
            VideoReferenceMedia(
                kind=kind,  # type: ignore[arg-type]
                data=(
                    data
                    if data is not None
                    else await storage.aget_bytes(clean_storage_key)
                ),
                mime=mime,
                label=clean_label,
                ref_id=clean_ref_id,
            )
        )
    if not result:
        raise RuntimeError("reference media snapshot has no usable entries")
    return result


async def run_video_generation(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-submit:{new_uuid7()}"
    if not await _acquire_lease(redis, task_id, token):
        return
    stop_renewer = asyncio.Event()
    lease_lost = asyncio.Event()
    renewer = asyncio.create_task(
        _lease_renewer(
            redis,
            task_id,
            token,
            stop=stop_renewer,
            lost=lease_lost,
        )
    )
    try:
        await _run_video_generation_with_lease(
            ctx,
            task_id,
            token=token,
            lease_lost=lease_lost,
        )
    finally:
        stop_renewer.set()
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        await _release_lease(redis, task_id, token)


async def _handle_existing_pre_submit_state(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    *,
    cached_submit: _CachedSubmitResult | None,
    task_id: str,
    token: str,
) -> bool:
    if cached_submit is not None:
        return False
    if generation.status == VideoGenerationStatus.SUBMITTING.value:
        now = _now()
        submit_started_at = getattr(
            generation,
            "submit_started_at",
            None,
        ) or getattr(generation, "updated_at", None)
        if submit_started_at is not None and submit_started_at > now - timedelta(
            seconds=_SUBMIT_UNKNOWN_AFTER_S
        ):
            generation.next_poll_at = submit_started_at + timedelta(
                seconds=_SUBMIT_UNKNOWN_AFTER_S
            )
        else:
            _transition_submit_unknown(
                session,
                generation,
                now=now,
                reason="duplicate_worker_observed_stale_submitting",
            )
        await session.commit()
        await _release_lease(redis, task_id, token)
        return True
    if generation.deadline_at <= _now():
        await _mark_pre_submit_expired(
            session,
            generation,
            reason="deadline_expired_before_submit",
        )
        await session.commit()
        await worker_flush_balance_cache(session)
        await _release_lease(redis, task_id, token)
        return True
    if (
        generation.cancel_requested_at is not None
        and generation.status == VideoGenerationStatus.QUEUED.value
        and not generation.provider_task_id
    ):
        await _mark_pre_submit_canceled(session, generation)
        await session.commit()
        await worker_flush_balance_cache(session)
        await _release_lease(redis, task_id, token)
        return True
    return False


async def _resume_existing_provider_task(
    redis: Any,
    generation: VideoGeneration,
    *,
    task_id: str,
    token: str,
) -> bool:
    if not generation.provider_task_id:
        return False
    try:
        await _enqueue_poll(redis, generation.id, defer_s=0)
    except Exception:
        logger.warning(
            "video poll enqueue failed task=%s",
            generation.id,
            exc_info=True,
        )
    await _release_lease(redis, task_id, token)
    return True


def _restore_cached_provider_identity(
    generation: VideoGeneration,
    cached_submit: _CachedSubmitResult,
) -> SubmitResult:
    cached_provider_name = _cached_submit_provider_name(cached_submit)
    cached_provider_kind = _cached_submit_provider_kind(cached_submit)
    if cached_provider_name and not generation.provider_name:
        generation.provider_name = cached_provider_name
    if cached_provider_kind and not generation.provider_kind:
        generation.provider_kind = cached_provider_kind
    return _cached_submit_result(cached_submit)


async def _reserve_video_submit_slot(
    redis: Any,
    generation: VideoGeneration,
    provider: Any,
    *,
    task_id: str,
    token: str,
    cached: bool,
) -> bool:
    acquired = await _acquire_provider_slot(
        redis,
        provider.name,
        provider.concurrency,
        generation.id,
    )
    if acquired:
        return True
    try:
        await _enqueue_submit(
            redis,
            generation.id,
            defer_s=_POLL_INTERVAL_S,
        )
    except Exception:
        label = "cached submit" if cached else "submit"
        logger.warning(
            "video %s re-enqueue failed task=%s",
            label,
            generation.id,
            exc_info=True,
        )
    await _release_lease(redis, task_id, token)
    return False


async def _restore_pre_submit_after_lease_loss(
    redis: Any,
    task_id: str,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
) -> None:
    should_requeue = False
    try:
        async with SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                generation is not None
                and not generation.provider_task_id
                and generation.status
                in {
                    VideoGenerationStatus.QUEUED.value,
                    VideoGenerationStatus.SUBMITTING.value,
                }
            ):
                generation.status = VideoGenerationStatus.QUEUED.value
                generation.progress_stage = VideoGenerationStage.QUEUED.value
                generation.next_poll_at = _now()
                generation.submit_started_at = None
                diagnostics = _generation_diagnostics(generation)
                _append_bounded_history(
                    diagnostics,
                    "submit_recovery_history",
                    {
                        "at": _now().isoformat(),
                        "reason": "lease_lost_before_upstream",
                        "submission_epoch": submission_epoch,
                    },
                )
                generation.diagnostics = diagnostics
                should_requeue = True
            await session.commit()
    except Exception:
        logger.error(
            "video pre-submit lease recovery failed task=%s epoch=%s",
            task_id,
            submission_epoch,
            exc_info=True,
        )
    if provider_name:
        await _release_provider_slot(redis, provider_name, task_id)
    if should_requeue:
        try:
            await _enqueue_submit(redis, task_id, defer_s=0)
        except Exception:
            logger.warning(
                "video pre-submit lease recovery enqueue failed task=%s",
                task_id,
                exc_info=True,
            )


async def _run_video_generation_with_lease(
    ctx: dict[str, Any],
    task_id: str,
    *,
    token: str,
    lease_lost: asyncio.Event,
) -> None:
    redis = ctx["redis"]
    slot_provider_name: str | None = None
    submission_epoch: int | None = None
    upstream_invoked = False
    provider_supports_idempotency = False
    try:
        async with SessionLocal() as session:
            generation = (
                await session.execute(
                    select(VideoGeneration)
                    .where(VideoGeneration.id == task_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if generation is None or generation.status in _NON_RESUBMIT_STATUSES:
                await _release_lease(redis, task_id, token)
                return
            if await _resume_existing_provider_task(
                redis,
                generation,
                task_id=task_id,
                token=token,
            ):
                return
            cached_submit = await _load_submit_result(redis, generation.id)
            if await _handle_existing_pre_submit_state(
                session,
                redis,
                generation,
                cached_submit=cached_submit,
                task_id=task_id,
                token=token,
            ):
                return
            if cached_submit is not None:
                submission_epoch = int(getattr(generation, "submission_epoch", 0) or 0)
                result = _restore_cached_provider_identity(
                    generation,
                    cached_submit,
                )
                await session.commit()
            else:
                provider = await _provider_for_generation(generation)
                if not await _reserve_video_submit_slot(
                    redis,
                    generation,
                    provider,
                    task_id=task_id,
                    token=token,
                    cached=False,
                ):
                    return
                slot_provider_name = provider.name
                provider_supports_idempotency = bool(
                    getattr(provider, "supports_idempotency", False)
                )
                generation.provider_name = provider.name
                generation.provider_kind = provider.kind
                _raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost before state transition",
                )
                input_bytes, input_mime = await _input_image_bytes(session, generation)
                reference_media = await _reference_media_bytes(generation)
                generation.status = VideoGenerationStatus.SUBMITTING.value
                generation.progress_stage = VideoGenerationStage.SUBMITTING.value
                generation.progress_pct = max(generation.progress_pct, 5)
                submit_started_at = _now()
                generation.started_at = generation.started_at or submit_started_at
                generation.attempt += 1
                generation.submission_epoch = (
                    int(getattr(generation, "submission_epoch", 0) or 0) + 1
                )
                submission_epoch = generation.submission_epoch
                generation.submit_started_at = submit_started_at
                generation.provider_idempotency_key = (
                    getattr(generation, "provider_idempotency_key", None)
                    or f"video:{generation.id}"
                )
                generation.next_poll_at = submit_started_at + timedelta(
                    seconds=_SUBMIT_UNKNOWN_AFTER_S
                )
                await session.commit()

                _raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost before upstream call",
                )
                upstream_model = provider.upstream_model_for(
                    generation.model, generation.action
                )
                if not upstream_model:
                    raise RuntimeError("provider model mapping missing")
                adapter = adapter_for_provider(provider)
                upstream_invoked = True
                result = await adapter.submit(
                    VideoSubmitRequest(
                        task_id=generation.id,
                        user_id=generation.user_id,
                        action=generation.action,  # type: ignore[arg-type]
                        model=generation.model,
                        upstream_model=upstream_model,
                        prompt=generation.prompt,
                        duration_s=generation.duration_s,
                        resolution=generation.resolution,
                        aspect_ratio=generation.aspect_ratio,
                        generate_audio=generation.generate_audio,
                        seed=generation.seed,
                        watermark=generation.watermark,
                        input_image_url=_input_image_url(generation),
                        input_image_bytes=input_bytes,
                        input_image_mime=input_mime,
                        reference_media=reference_media,
                        idempotency_key=(
                            getattr(generation, "provider_idempotency_key", None)
                            or f"video:{generation.id}"
                        ),
                    )
                )
                try:
                    await _store_submit_result(
                        redis,
                        task_id,
                        result,
                        provider_name=provider.name,
                        provider_kind=provider.kind,
                    )
                except Exception:
                    logger.warning(
                        "video submit cache store failed task=%s",
                        task_id,
                        exc_info=True,
                    )
                _raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost after upstream call",
                )
    except Exception as exc:  # noqa: BLE001
        await _handle_video_submit_exception(
            redis,
            task_id,
            exc,
            provider_name=slot_provider_name,
            submission_epoch=submission_epoch,
            upstream_invoked=upstream_invoked,
            provider_supports_idempotency=provider_supports_idempotency,
        )
        return

    persisted = await _persist_video_submit_receipt(
        redis,
        task_id,
        result,
        submission_epoch=submission_epoch,
        lease_lost=lease_lost,
    )
    if not persisted:
        await _enqueue_cached_submit_recovery(
            redis,
            task_id,
            defer_s=_POLL_INTERVAL_S,
        )
        return

    try:
        await _enqueue_poll(redis, task_id)
    except Exception:
        logger.warning("video poll enqueue failed task=%s", task_id, exc_info=True)
    finally:
        await _release_lease(redis, task_id, token)


async def _handle_video_submit_exception(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
    upstream_invoked: bool,
    provider_supports_idempotency: bool,
) -> None:
    if isinstance(exc, _VideoLeaseLost):
        logger.warning(
            "video submit lease lost; stale worker will not mutate task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        if not upstream_invoked:
            await _restore_pre_submit_after_lease_loss(
                redis,
                task_id,
                provider_name=provider_name,
                submission_epoch=submission_epoch,
            )
        return
    if (
        upstream_invoked
        and not provider_supports_idempotency
        and _submit_outcome_unknown(exc)
    ):
        try:
            await _mark_submit_unknown(
                task_id,
                exc,
                provider_name=provider_name,
                submission_epoch=submission_epoch,
            )
        except Exception:
            logger.error(
                "video submit outcome unknown persistence failed task=%s epoch=%s",
                task_id,
                submission_epoch,
                exc_info=True,
            )
        return
    await _fail_before_submit(
        redis,
        task_id,
        exc,
        provider_name=provider_name,
        submission_epoch=submission_epoch,
    )


async def _persist_video_submit_receipt(
    redis: Any,
    task_id: str,
    result: SubmitResult,
    *,
    submission_epoch: int | None,
    lease_lost: asyncio.Event,
) -> bool:
    try:
        _raise_if_video_lease_lost(
            lease_lost,
            "video submit lease lost before receipt persistence",
        )
        async with SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if generation is None:
                logger.warning(
                    "video submit receipt fenced out task=%s epoch=%s",
                    task_id,
                    submission_epoch,
                )
                return False
            if generation.status in _TERMINAL_STATUSES:
                return False
            generation.provider_task_id = result.provider_task_id
            generation.upstream_response = result.raw
            generation.status = VideoGenerationStatus.SUBMITTED.value
            generation.progress_stage = VideoGenerationStage.RENDERING.value
            generation.progress_pct = max(generation.progress_pct, 10)
            generation.submitted_at = _now()
            generation.next_poll_at = _now() + timedelta(seconds=_POLL_INTERVAL_S)
            diagnostics = _generation_diagnostics(generation)
            diagnostics["submit_receipt"] = {
                "submission_epoch": submission_epoch,
                "provider_task_id": result.provider_task_id,
                "provider_idempotency_key": getattr(
                    generation, "provider_idempotency_key", None
                ),
                "persisted_at": _now().isoformat(),
            }
            generation.diagnostics = diagnostics
            await session.commit()
            await _publish_after_commit(redis, generation, EV_VIDEO_SUBMITTED)
            return True
    except _VideoLeaseLost:
        logger.warning(
            "video submit receipt skipped after lease loss task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        return False
    except Exception:
        logger.warning("video submit persist failed task=%s", task_id, exc_info=True)
        return False


async def _mark_pre_submit_canceled(session, generation: VideoGeneration) -> None:
    generation.status = VideoGenerationStatus.CANCELED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "canceled"
    generation.error_message = "cancelled before upstream submission"
    generation.finished_at = _now()
    await resolve_video_billing(
        session,
        generation,
        poll_result=PollResult(
            status="cancelled",
            upstream_billable=False,
            raw={"reason": "pre_submit_cancel"},
        ),
        reason="pre_submit_cancel",
    )
    _queue_video_event(session, generation, EV_VIDEO_CANCELED)


async def _mark_pre_submit_expired(
    session, generation: VideoGeneration, *, reason: str
) -> None:
    diagnostics = _generation_diagnostics(generation)
    diagnostics["pre_submit_expired_at"] = _now().isoformat()
    generation.status = VideoGenerationStatus.EXPIRED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "deadline_expired"
    generation.error_message = "video task expired before upstream submission"
    generation.diagnostics = diagnostics
    generation.finished_at = _now()
    await resolve_video_billing(
        session,
        generation,
        poll_result=PollResult(
            status="expired",
            failure_class="deadline_expired",
            upstream_billable=False,
            raw={"reason": "pre_submit_expired", "detail": reason},
        ),
        reason=reason,
    )
    _queue_video_event(session, generation, EV_VIDEO_FAILED)


def _transition_submit_unknown(
    session: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
    reason: str,
    last_error: dict[str, Any] | None = None,
) -> None:
    diagnostics = _generation_diagnostics(generation)
    diagnostics["submit_unknown_at"] = now.isoformat()
    diagnostics["submit_unknown_reason"] = reason
    diagnostics["submission_epoch"] = int(
        getattr(generation, "submission_epoch", 0) or 0
    )
    diagnostics["provider_idempotency_key"] = getattr(
        generation,
        "provider_idempotency_key",
        None,
    )
    if last_error is not None:
        diagnostics["last_submit_error"] = last_error
    generation.status = VideoGenerationStatus.SUBMIT_UNKNOWN.value
    generation.progress_stage = VideoGenerationStage.SUBMITTING.value
    generation.progress_pct = max(generation.progress_pct, 5)
    generation.error_code = "submit_unknown"
    generation.error_message = (
        "video submission outcome is unknown; automatic reconciliation pending"
    )
    generation.next_poll_at = now + timedelta(seconds=_SUBMIT_UNKNOWN_FINALIZE_AFTER_S)
    generation.diagnostics = diagnostics
    _queue_video_event(
        session,
        generation,
        EV_VIDEO_PROGRESS,
        submission_unknown=True,
    )


async def _mark_submit_unknown(
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
) -> bool:
    async with SessionLocal() as session:
        filters = [VideoGeneration.id == task_id]
        if submission_epoch is not None:
            filters.append(VideoGeneration.submission_epoch == submission_epoch)
        generation = (
            await session.execute(
                select(VideoGeneration).where(*filters).with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _TERMINAL_STATUSES:
            return False
        now = _now()
        error_code = _video_exception_code(exc, default="upstream_unknown")
        error_message = _video_exception_message(exc, phase="submit")
        generation.provider_name = generation.provider_name or provider_name
        _transition_submit_unknown(
            session,
            generation,
            now=now,
            reason="ambiguous_non_idempotent_submit_error",
            last_error={
                "at": now.isoformat(),
                "attempt": _generation_attempt(generation),
                "error_code": error_code,
                "message": error_message[:500],
                "retryable": False,
                "outcome_unknown": True,
            },
        )
        await session.commit()
        return True


async def _fail_before_submit(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None = None,
    submission_epoch: int | None = None,
) -> None:
    release_provider_name = provider_name
    release_provider_slot = False
    try:
        async with SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if generation is None or generation.status in _NON_RESUBMIT_STATUSES:
                return
            release_provider_slot = True
            release_provider_name = release_provider_name or generation.provider_name
            if await _schedule_submit_retry(session, redis, generation, exc):
                return
            error_code = _video_exception_code(exc, default="provider_unavailable")
            error_message = _video_exception_message(exc, phase="submit")
            logger.warning(
                "video submit failed task=%s attempt=%s code=%s error=%s",
                task_id,
                _generation_attempt(generation),
                error_code,
                error_message,
                exc_info=_exception_log_info(exc),
            )
            diagnostics = _generation_diagnostics(generation)
            diagnostics["last_submit_error"] = {
                "at": _now().isoformat(),
                "attempt": _generation_attempt(generation),
                "error_code": error_code,
                "message": error_message[:500],
                "retryable": _is_retryable_video_exception(exc),
                "terminal": True,
            }
            generation.status = VideoGenerationStatus.FAILED.value
            generation.progress_stage = VideoGenerationStage.FINISHED.value
            generation.progress_pct = 100
            generation.error_code = error_code
            generation.error_message = error_message
            generation.diagnostics = diagnostics
            generation.finished_at = _now()
            billable_hint = _submit_failure_billable_hint(exc)
            await resolve_video_billing(
                session,
                generation,
                poll_result=PollResult(
                    status="failed",
                    upstream_billable=billable_hint,
                    raw={
                        "phase": "submit",
                        "error": error_message,
                        "error_code": error_code,
                        "upstream_cost_ambiguous": billable_hint is None,
                    },
                ),
                reason="submit_failed_ambiguous_upstream_cost"
                if billable_hint is None
                else "submit_failed_before_upstream_cost",
            )
            _queue_video_event(session, generation, EV_VIDEO_FAILED)
            await session.commit()
            await worker_flush_balance_cache(session)
    finally:
        if release_provider_slot and release_provider_name:
            await _release_provider_slot(redis, release_provider_name, task_id)


async def _schedule_submit_retry(
    session,
    redis: Any,
    generation: VideoGeneration,
    exc: Exception,
) -> bool:
    if not _is_retryable_video_exception(exc):
        return False
    attempt = _generation_attempt(generation)
    if attempt >= _MAX_SUBMIT_ATTEMPTS:
        return False
    now = _now()
    remaining_s = int((generation.deadline_at - now).total_seconds())
    if remaining_s <= 1:
        return False
    delay_s = max(1, min(_submit_retry_delay_s(attempt), remaining_s - 1))
    error_code = _video_exception_code(exc, default="provider_unavailable")
    error_message = _video_exception_message(exc, phase="submit")
    diagnostics = _generation_diagnostics(generation)
    retry_item = {
        "at": now.isoformat(),
        "attempt": attempt,
        "error_code": error_code,
        "message": error_message[:500],
        "next_retry_delay_s": delay_s,
    }
    _append_bounded_history(diagnostics, "submit_retry_history", retry_item)
    diagnostics["last_submit_error"] = {**retry_item, "retryable": True}
    diagnostics["submit_retry_count"] = len(diagnostics["submit_retry_history"])
    generation.status = VideoGenerationStatus.QUEUED.value
    generation.progress_stage = VideoGenerationStage.QUEUED.value
    generation.progress_pct = max(generation.progress_pct, 5)
    generation.next_poll_at = now + timedelta(seconds=delay_s)
    generation.error_code = None
    generation.error_message = None
    generation.diagnostics = diagnostics
    _queue_video_event(
        session,
        generation,
        EV_VIDEO_PROGRESS,
        retry_transition=True,
        retry_after_s=delay_s,
        retry_attempt=attempt,
        retry_error_code=error_code,
    )
    await session.commit()
    logger.info(
        "video submit retry scheduled task=%s attempt=%s delay_s=%s code=%s error=%s",
        generation.id,
        attempt,
        delay_s,
        error_code,
        error_message,
    )
    try:
        await _enqueue_submit(redis, generation.id, defer_s=delay_s)
    except Exception:
        logger.warning(
            "video submit retry enqueue failed task=%s", generation.id, exc_info=True
        )
    return True


async def run_video_poll(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-poll:{new_uuid7()}"
    if not await _acquire_lease(redis, task_id, token):
        return
    try:
        async with SessionLocal() as session:
            generation = (
                await session.execute(
                    select(VideoGeneration)
                    .where(VideoGeneration.id == task_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if generation is None or generation.status in _TERMINAL_STATUSES:
                await _release_lease(redis, task_id, token)
                return
            if not generation.provider_task_id:
                await _enqueue_submit(redis, task_id, defer_s=_POLL_INTERVAL_S)
                await _release_lease(redis, task_id, token)
                return
            provider = await _provider_for_generation(generation)
            adapter = adapter_for_provider(provider)
            deadline_expired = generation.deadline_at <= _now()
            should_commit_poll_state = False
            if generation.cancel_requested_at is not None:
                await _try_provider_cancel(adapter, generation)
                should_commit_poll_state = True
            if deadline_expired:
                diagnostics = _generation_diagnostics(generation)
                diagnostics.setdefault("deadline_expired_at", _now().isoformat())
                diagnostics["deadline_expired_polling_continues"] = True
                generation.diagnostics = diagnostics
                should_commit_poll_state = True
            if should_commit_poll_state:
                await session.commit()

        poll = await adapter.poll(generation.provider_task_id)
        await _apply_poll_result(redis, task_id, poll)
    except VideoUpstreamError as exc:
        if await _finish_cancelled_after_provider_poll_error(redis, task_id, exc):
            return
        if not await _schedule_poll_retry(redis, task_id, exc):
            retryable_poll_error = _is_retryable_video_exception(exc)
            await _apply_poll_result(
                redis,
                task_id,
                PollResult(
                    status="expired" if retryable_poll_error else "failed",
                    failure_class=exc.error_code,
                    upstream_billable=None,
                    raw=exc.raw
                    or {
                        "error": _video_exception_message(exc, phase="poll"),
                        "error_code": _video_exception_code(
                            exc, default="upstream_unknown"
                        ),
                    },
                ),
                fallback_error_message=_video_exception_message(exc, phase="poll"),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("video poll failed task=%s err=%s", task_id, exc, exc_info=True)
        async with SessionLocal() as session:
            generation = (
                await session.execute(
                    select(VideoGeneration)
                    .where(VideoGeneration.id == task_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if generation is None or generation.status in _TERMINAL_STATUSES:
                await _release_lease(redis, task_id, token)
                return
            generation.poll_count += 1
            generation.next_poll_at = _now() + timedelta(seconds=_POLL_INTERVAL_S)
            await session.commit()
        await _enqueue_poll(redis, task_id)
    finally:
        await _release_lease(redis, task_id, token)


async def _schedule_poll_retry(
    redis: Any,
    task_id: str,
    exc: Exception,
) -> bool:
    if not _is_retryable_video_exception(exc):
        return False
    async with SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _TERMINAL_STATUSES:
            return True
        now = _now()
        error_code = _video_exception_code(exc, default="upstream_unknown")
        if _provider_tracking_window_exhausted(generation, now):
            return False
        local_window_exhausted = _poll_window_exhausted(generation, now)
        if local_window_exhausted:
            delay_s = _EXTENDED_POLL_INTERVAL_S
        else:
            remaining_s = int((generation.deadline_at - now).total_seconds())
            delay_s = (
                _POLL_RETRY_DELAY_S
                if remaining_s <= 1
                else max(1, min(_POLL_RETRY_DELAY_S, remaining_s - 1))
            )
        error_message = _video_exception_message(exc, phase="poll")
        diagnostics = dict(generation.diagnostics or {})
        if generation.deadline_at <= now:
            diagnostics.setdefault("deadline_expired_at", now.isoformat())
            diagnostics["deadline_expired_poll_retry_continues"] = True
        if local_window_exhausted:
            diagnostics.setdefault("poll_window_exhausted_at", now.isoformat())
            diagnostics["extended_polling_continues"] = True
            diagnostics["extended_poll_delay_s"] = delay_s
            diagnostics["max_poll_count"] = _MAX_POLL_COUNT
            diagnostics["max_poll_duration_s"] = _MAX_POLL_DURATION_S
            diagnostics["max_provider_poll_duration_s"] = _MAX_PROVIDER_POLL_DURATION_S
            elapsed_s = _poll_elapsed_s(generation, now)
            if elapsed_s is not None:
                diagnostics["poll_elapsed_s"] = elapsed_s
        retry_item = {
            "at": now.isoformat(),
            "poll_count": generation.poll_count,
            "error_code": error_code,
            "message": error_message[:500],
            "next_retry_delay_s": delay_s,
        }
        _append_bounded_history(diagnostics, "poll_retry_history", retry_item)
        diagnostics["last_poll_error"] = {**retry_item, "retryable": True}
        diagnostics["poll_retry_count"] = len(diagnostics["poll_retry_history"])
        generation.status = VideoGenerationStatus.RUNNING.value
        generation.progress_stage = (
            VideoGenerationStage.FETCHING.value
            if error_code == "fetch_failed"
            else VideoGenerationStage.RENDERING.value
        )
        generation.progress_pct = max(generation.progress_pct, 20)
        generation.poll_count += 1
        generation.next_poll_at = now + timedelta(seconds=delay_s)
        generation.error_code = None
        generation.error_message = None
        generation.diagnostics = diagnostics
        await session.commit()
        logger.info(
            "video poll retry scheduled task=%s poll_count=%s delay_s=%s code=%s "
            "error=%s",
            generation.id,
            generation.poll_count,
            delay_s,
            error_code,
            error_message,
        )
        try:
            await _publish(
                redis,
                generation,
                EV_VIDEO_PROGRESS,
                retry_after_s=delay_s,
                retry_error_code=error_code,
            )
        except Exception:
            logger.warning(
                "video poll retry publish failed task=%s",
                generation.id,
                exc_info=True,
            )
        try:
            await _enqueue_poll(redis, generation.id, defer_s=delay_s)
        except Exception:
            logger.warning(
                "video poll retry enqueue failed task=%s", generation.id, exc_info=True
            )
        return True


async def _finish_cancelled_after_provider_poll_error(
    redis: Any,
    task_id: str,
    exc: VideoUpstreamError,
) -> bool:
    if _video_exception_code(exc, default="upstream_unknown") != "upstream_not_ready":
        return False
    async with SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _TERMINAL_STATUSES:
            return True
        diagnostics = _generation_diagnostics(generation)
        if generation.cancel_requested_at is None or not diagnostics.get(
            "cancel_sent_at"
        ):
            return False
        poll = PollResult(
            status="cancelled",
            failure_class="canceled",
            upstream_billable=None,
            raw={
                **(
                    exc.raw
                    or {
                        "phase": "poll",
                        "error": _video_exception_message(exc, phase="poll"),
                        "error_code": "upstream_not_ready",
                    }
                ),
                "upstream_cost_ambiguous": True,
            },
        )
        await _finish_terminal_failure(
            session,
            redis,
            generation,
            poll,
            fallback_error_message="video task cancelled by user",
        )
        return True


async def _try_provider_cancel(adapter: Any, generation: VideoGeneration) -> None:
    diagnostics = _generation_diagnostics(generation)
    if diagnostics.get("cancel_sent_at") or diagnostics.get("cancel_unsupported_at"):
        return
    try:
        result = await adapter.cancel(generation.provider_task_id)
        attempted_at = _now().isoformat()
        diagnostics["cancel_attempted_at"] = attempted_at
        diagnostics["cancel_result"] = result.raw if result else None
        if result is None:
            diagnostics["cancel_unsupported_at"] = attempted_at
        elif result.accepted:
            diagnostics["cancel_sent_at"] = attempted_at
        else:
            diagnostics["cancel_rejected_at"] = attempted_at
    except Exception as exc:  # noqa: BLE001
        diagnostics["cancel_error_at"] = _now().isoformat()
        diagnostics["cancel_error"] = str(exc)[:500]
    generation.diagnostics = diagnostics


async def _apply_poll_result(
    redis: Any,
    task_id: str,
    poll: PollResult,
    *,
    fallback_error_message: str | None = None,
) -> None:
    async with SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _TERMINAL_STATUSES:
            return

        if poll.status in {"queued", "running"}:
            now = _now()
            if not _provider_tracking_window_exhausted(generation, now):
                await _continue_running_poll(session, redis, generation, poll, now=now)
                return
            poll = PollResult(
                status="expired",
                failure_class="poll_timeout",
                usage_total_tokens=poll.usage_total_tokens,
                upstream_billable=None,
                raw={
                    **(poll.raw or {}),
                    "error": "video task exceeded maximum provider tracking window",
                    "poll_count": generation.poll_count,
                    "max_poll_count": _MAX_POLL_COUNT,
                    "max_poll_duration_s": _MAX_POLL_DURATION_S,
                    "max_provider_poll_duration_s": _MAX_PROVIDER_POLL_DURATION_S,
                    "poll_elapsed_s": _poll_elapsed_s(generation, now),
                },
            )

        if poll.status == "succeeded":
            if not poll.video_url:
                poll = PollResult(
                    status="failed",
                    failure_class="fetch_failed",
                    usage_total_tokens=poll.usage_total_tokens,
                    upstream_billable=poll.upstream_billable,
                    raw={**(poll.raw or {}), "error": "missing video_url"},
                )
            else:
                await _finish_success(session, redis, generation, poll)
                return

        await _finish_terminal_failure(
            session,
            redis,
            generation,
            poll,
            fallback_error_message=fallback_error_message,
        )


async def _continue_running_poll(
    session,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    now: datetime,
) -> None:
    local_window_exhausted = _poll_window_exhausted(generation, now)
    delay_s = _EXTENDED_POLL_INTERVAL_S if local_window_exhausted else _POLL_INTERVAL_S
    diagnostics = _generation_diagnostics(generation)
    if local_window_exhausted:
        diagnostics.setdefault("poll_window_exhausted_at", now.isoformat())
        diagnostics["extended_polling_continues"] = True
        diagnostics["extended_poll_delay_s"] = delay_s
        diagnostics["max_poll_count"] = _MAX_POLL_COUNT
        diagnostics["max_poll_duration_s"] = _MAX_POLL_DURATION_S
        diagnostics["max_provider_poll_duration_s"] = _MAX_PROVIDER_POLL_DURATION_S
        elapsed_s = _poll_elapsed_s(generation, now)
        if elapsed_s is not None:
            diagnostics["poll_elapsed_s"] = elapsed_s

    generation.status = VideoGenerationStatus.RUNNING.value
    generation.progress_stage = VideoGenerationStage.RENDERING.value
    generation.progress_pct = max(
        generation.progress_pct,
        min(95, int(poll.progress if poll.progress is not None else 20)),
    )
    generation.poll_count += 1
    generation.upstream_response = poll.raw
    generation.next_poll_at = now + timedelta(seconds=delay_s)
    generation.error_code = None
    generation.error_message = None
    generation.diagnostics = diagnostics
    await session.commit()
    await _publish(
        redis,
        generation,
        EV_VIDEO_PROGRESS,
        extended_polling=local_window_exhausted,
        retry_after_s=delay_s,
    )
    await _enqueue_poll(redis, generation.id, defer_s=delay_s)


async def _finish_success(
    session,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
) -> None:
    release_provider_name = generation.provider_name
    try:
        provider = await _provider_for_generation(generation)
        release_provider_name = release_provider_name or provider.name
        adapter = adapter_for_provider(provider)
        generation.status = VideoGenerationStatus.RUNNING.value
        generation.progress_stage = VideoGenerationStage.FETCHING.value
        generation.progress_pct = max(generation.progress_pct, 96)
        generation.upstream_response = poll.raw
        await session.commit()
        await _publish(redis, generation, EV_VIDEO_FETCHING)

        video_bytes = await adapter.fetch_result(poll.video_url or "")
        stored = await _store_video_asset(generation, video_bytes)
        existing = await _video_for_generation(session, generation.id)
        if existing is None:
            session.add(stored.video)
            await session.flush()
            video = stored.video
        else:
            video = existing
        diagnostics = {**(generation.diagnostics or {}), **stored.diagnostics}
        resolution = await resolve_video_billing(
            session,
            generation,
            poll_result=poll,
            reason="succeeded",
        )
        diagnostics["billing_decision"] = resolution.decision
        generation.status = VideoGenerationStatus.SUCCEEDED.value
        generation.progress_stage = VideoGenerationStage.FINISHED.value
        generation.progress_pct = 100
        generation.upstream_response = poll.raw
        generation.diagnostics = diagnostics
        generation.billed_tokens = resolution.actual_tokens
        generation.billed_cost_micro = resolution.actual_micro
        generation.finished_at = _now()
        _queue_video_event(
            session,
            generation,
            EV_VIDEO_SUCCEEDED,
            video_id=video.id,
        )
        await session.commit()
        await worker_flush_balance_cache(session)
    finally:
        if release_provider_name:
            await _release_provider_slot(redis, release_provider_name, generation.id)


async def worker_flush_balance_cache(session) -> None:
    from .. import billing as worker_billing

    await worker_billing.flush_balance_cache_refreshes(session)


async def _finish_terminal_failure(
    session,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    fallback_error_message: str | None,
) -> None:
    release_provider_name = generation.provider_name
    try:
        resolution = await resolve_video_billing(
            session,
            generation,
            poll_result=poll,
            reason=poll.status,
        )
        internal_status = (
            VideoGenerationStatus.CANCELED.value
            if poll.status == "cancelled"
            else VideoGenerationStatus.EXPIRED.value
            if poll.status == "expired"
            else VideoGenerationStatus.FAILED.value
        )
        generation.status = internal_status
        generation.progress_stage = VideoGenerationStage.FINISHED.value
        generation.progress_pct = 100
        generation.upstream_response = poll.raw
        generation.error_code = poll.failure_class or poll.status
        generation.error_message = fallback_error_message or _error_message(poll)
        generation.billed_tokens = resolution.actual_tokens
        generation.billed_cost_micro = resolution.actual_micro
        generation.diagnostics = {
            **(generation.diagnostics or {}),
            "billing_decision": resolution.decision,
        }
        generation.finished_at = _now()
        _queue_video_event(
            session,
            generation,
            EV_VIDEO_CANCELED
            if internal_status == VideoGenerationStatus.CANCELED.value
            else EV_VIDEO_FAILED,
        )
        await session.commit()
        await worker_flush_balance_cache(session)
    finally:
        if release_provider_name:
            await _release_provider_slot(redis, release_provider_name, generation.id)


def _error_message(poll: PollResult) -> str:
    raw_msg = None
    if isinstance(poll.raw, dict):
        raw_msg = poll.raw.get("message")
        if not raw_msg:
            raw_error = poll.raw.get("error")
            if isinstance(raw_error, dict):
                raw_msg = raw_error.get("message")
            else:
                raw_msg = raw_error
    if isinstance(raw_msg, str) and raw_msg:
        return raw_msg[:1000]
    return f"video task {poll.status}"


async def _video_for_generation(session, generation_id: str) -> Video | None:
    return (
        await session.execute(
            select(Video).where(
                Video.owner_generation_id == generation_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _store_video_asset(generation: VideoGeneration, data: bytes) -> _StoredVideo:
    processed, diagnostics = await asyncio.to_thread(_postprocess_video_bytes, data)
    video_key = f"u/{generation.user_id}/v/{generation.id}/output.mp4"
    poster_key = f"u/{generation.user_id}/v/{generation.id}/poster.jpg"
    await storage.aput_bytes(video_key, processed["video_bytes"])
    poster_storage_key = None
    if processed.get("poster_bytes"):
        try:
            await storage.aput_bytes(poster_key, processed["poster_bytes"])
            poster_storage_key = poster_key
        except StorageDiskFullError:
            raise
        except Exception as exc:  # noqa: BLE001
            diagnostics["poster_store_error"] = str(exc)[:500]
    sha = hashlib.sha256(processed["video_bytes"]).hexdigest()
    video = Video(
        id=new_uuid7(),
        user_id=generation.user_id,
        owner_generation_id=generation.id,
        storage_key=video_key,
        poster_storage_key=poster_storage_key,
        mime="video/mp4",
        width=int(processed.get("width") or 0),
        height=int(processed.get("height") or 0),
        duration_ms=int(processed.get("duration_ms") or 0),
        fps=processed.get("fps"),
        size_bytes=len(processed["video_bytes"]),
        sha256=sha,
        etag=sha,
        has_audio=bool(processed.get("has_audio")),
        faststart=bool(processed.get("faststart")),
        visibility="private",
        metadata_jsonb=diagnostics,
    )
    return _StoredVideo(video=video, diagnostics=diagnostics)


def _postprocess_video_bytes(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    diagnostics: dict[str, Any] = {}
    video_bytes = data
    faststart = _looks_faststart(data)
    diagnostics["faststart_input"] = faststart
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    poster_bytes = None
    metadata: dict[str, Any] = {}
    if ffmpeg and ffprobe:
        with tempfile.TemporaryDirectory(prefix="lumen-video-") as tmp:
            src = Path(tmp) / "input.mp4"
            dst = Path(tmp) / "faststart.mp4"
            poster = Path(tmp) / "poster.jpg"
            src.write_bytes(data)
            if not faststart:
                proc = subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-i",
                        str(src),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(dst),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                )
                if proc.returncode == 0 and dst.is_file():
                    video_bytes = dst.read_bytes()
                    faststart = True
                else:
                    diagnostics["faststart_error"] = proc.stderr.decode(
                        "utf-8", "replace"
                    )[-1000:]
            probe_src = dst if dst.is_file() else src
            metadata = _probe_video(ffprobe, probe_src)
            poster_proc = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-ss",
                    "0",
                    "-i",
                    str(probe_src),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(poster),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
            if poster_proc.returncode == 0 and poster.is_file():
                poster_bytes = poster.read_bytes()
            else:
                diagnostics["poster_error"] = poster_proc.stderr.decode(
                    "utf-8", "replace"
                )[-1000:]
    else:
        diagnostics["ffmpeg_missing"] = True
    diagnostics["faststart"] = faststart
    diagnostics.update(metadata)
    return {
        "video_bytes": video_bytes,
        "poster_bytes": poster_bytes,
        "faststart": faststart,
        **metadata,
    }, diagnostics


def _probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return {"probe_error": proc.stderr.decode("utf-8", "replace")[-1000:]}
    try:
        raw = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return {"probe_error": "invalid ffprobe json"}
    streams = raw.get("streams") if isinstance(raw, dict) else []
    video_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"),
        {},
    )
    audio_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )
    duration = _float_or_none(video_stream.get("duration")) or _float_or_none(
        (raw.get("format") or {}).get("duration")
    )
    fps = _fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    return {
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "duration_ms": int(duration * 1000) if duration is not None else 0,
        "fps": fps,
        "has_audio": audio_stream is not None,
        "probe": raw,
    }


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _fps(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _float_or_none(value)
    left, right = value.split("/", 1)
    try:
        denom = float(right)
        if denom == 0:
            return None
        return float(left) / denom
    except (TypeError, ValueError):
        return None


def _looks_faststart(data: bytes) -> bool:
    offset = 0
    moov_offset: int | None = None
    mdat_offset: int | None = None
    data_len = len(data)
    while offset + 8 <= data_len:
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > data_len:
                break
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = data_len - offset
        if size < header_size:
            break
        if box_type == b"moov" and moov_offset is None:
            moov_offset = offset
        elif box_type == b"mdat" and mdat_offset is None:
            mdat_offset = offset
        if moov_offset is not None and mdat_offset is not None:
            break
        offset += size
    return moov_offset is not None and (
        mdat_offset is None or moov_offset < mdat_offset
    )


async def _finalize_submit_unknown(
    session: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
) -> None:
    diagnostics = _generation_diagnostics(generation)
    diagnostics["submit_unknown_finalized_at"] = now.isoformat()
    resolution = await resolve_video_billing(
        session,
        generation,
        poll_result=PollResult(
            status="expired",
            failure_class="submit_unknown",
            upstream_billable=None,
            raw={
                "reason": "submit_unknown_timeout",
                "upstream_cost_ambiguous": True,
            },
        ),
        reason="submit_unknown_timeout",
    )
    diagnostics["billing_decision"] = resolution.decision
    generation.status = VideoGenerationStatus.EXPIRED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "submit_unknown"
    generation.error_message = (
        "video submission outcome could not be reconciled before timeout"
    )
    generation.billed_tokens = resolution.actual_tokens
    generation.billed_cost_micro = resolution.actual_micro
    generation.finished_at = now
    generation.next_poll_at = None
    generation.diagnostics = diagnostics
    _queue_video_event(session, generation, EV_VIDEO_FAILED)


async def _reconcile_submit_unknown(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
) -> tuple[bool, bool, str | None]:
    try:
        cached_submit = await _load_submit_result(redis, generation.id)
    except Exception:
        logger.warning(
            "video submit-unknown cache lookup failed task=%s",
            generation.id,
            exc_info=True,
        )
        cached_submit = None
    if cached_submit is not None:
        generation.status = VideoGenerationStatus.SUBMITTING.value
        generation.error_code = None
        generation.error_message = None
        generation.next_poll_at = now
        return True, True, None
    if generation.next_poll_at is not None and generation.next_poll_at > now:
        return False, False, None
    provider_name = generation.provider_name
    await _finalize_submit_unknown(session, generation, now=now)
    return True, False, provider_name


async def reconcile_video_tasks(ctx: dict[str, Any]) -> int:
    redis = ctx["redis"]
    now = _now()
    cutoff = now - timedelta(seconds=_RECON_STALE_AFTER_S)
    submit_unknown_cutoff = now - timedelta(seconds=_SUBMIT_UNKNOWN_AFTER_S)
    touched = 0
    release_slots: list[tuple[str, str]] = []
    cached_recoveries: list[str] = []
    async with SessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(VideoGeneration)
                    .where(
                        VideoGeneration.status.in_(
                            [
                                VideoGenerationStatus.QUEUED.value,
                                VideoGenerationStatus.SUBMITTING.value,
                                VideoGenerationStatus.SUBMIT_UNKNOWN.value,
                                VideoGenerationStatus.SUBMITTED.value,
                                VideoGenerationStatus.RUNNING.value,
                            ]
                        ),
                        or_(
                            VideoGeneration.next_poll_at.is_(None),
                            VideoGeneration.next_poll_at <= _now(),
                            VideoGeneration.updated_at <= cutoff,
                        ),
                    )
                    .order_by(VideoGeneration.created_at)
                    .limit(_RECON_LIMIT)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.status == VideoGenerationStatus.SUBMIT_UNKNOWN.value:
                (
                    changed,
                    recover_cached,
                    release_provider,
                ) = await _reconcile_submit_unknown(
                    session,
                    redis,
                    row,
                    now=now,
                )
                if recover_cached:
                    cached_recoveries.append(row.id)
                if release_provider:
                    release_slots.append((release_provider, row.id))
                touched += int(changed)
                continue
            elif row.provider_task_id:
                await _enqueue_poll(redis, row.id, defer_s=0)
            elif row.status == VideoGenerationStatus.SUBMITTING.value:
                if await _lease_active(redis, row.id):
                    continue
                if await _enqueue_cached_submit_recovery(
                    redis,
                    row.id,
                    defer_s=0,
                ):
                    row.next_poll_at = now + timedelta(seconds=_POLL_INTERVAL_S)
                    touched += 1
                    continue
                submit_started_at = getattr(row, "submit_started_at", None) or getattr(
                    row, "updated_at", None
                )
                if (
                    submit_started_at is not None
                    and submit_started_at > submit_unknown_cutoff
                ):
                    continue
                _transition_submit_unknown(
                    session,
                    row,
                    now=now,
                    reason="stale_submitting_without_lease_or_receipt",
                )
            elif row.deadline_at <= now:
                await _mark_pre_submit_expired(
                    session,
                    row,
                    reason="reconcile_deadline_expired_before_submit",
                )
            else:
                row.status = VideoGenerationStatus.QUEUED.value
                row.progress_stage = VideoGenerationStage.QUEUED.value
                await _enqueue_submit(redis, row.id, defer_s=_POLL_INTERVAL_S)
            touched += 1
        await session.commit()
        await worker_flush_balance_cache(session)
    for task_id in cached_recoveries:
        try:
            await _enqueue_submit(redis, task_id, defer_s=0)
        except Exception:
            logger.warning(
                "video cached submit recovery enqueue failed task=%s",
                task_id,
                exc_info=True,
            )
    for provider_name, task_id in release_slots:
        await _release_provider_slot(redis, provider_name, task_id)
    return touched


cron_jobs = [
    cron(reconcile_video_tasks, second={15, 45}, run_at_startup=False),
]


__all__ = [
    "cron_jobs",
    "reconcile_video_tasks",
    "run_video_generation",
    "run_video_poll",
]
