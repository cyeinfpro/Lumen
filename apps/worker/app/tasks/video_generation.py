"""Video generation worker tasks."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from ..video_artifacts import (
    DownloadedVideo,
    ProcessedVideoFile as _ProcessedVideoFile,
    copy_video_file_exclusive_result,
    postprocess_video_bytes as _postprocess_video_bytes,
    postprocess_video_file as _postprocess_video_file,
)
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
    VideoProviderAdapter,
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
_MAX_UNEXPECTED_POLL_ATTEMPTS = 4
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
    created_storage_keys: tuple[str, ...] = ()


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
    raw_code = getattr(exc, "error_code", None)
    if isinstance(raw_code, str) and raw_code.strip():
        return raw_code.strip()[:64]
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


def _raise_if_video_lease_lost(
    lease_lost: asyncio.Event | None,
    message: str,
) -> None:
    if lease_lost is not None and lease_lost.is_set():
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


def _provider_binding_error(
    generation: VideoGeneration,
    message: str,
    *,
    current_provider_name: str | None = None,
) -> VideoUpstreamError:
    return VideoUpstreamError(
        message,
        error_code="provider_snapshot_unavailable",
        status_code=422,
        raw={
            "provider_name": generation.provider_name,
            "provider_kind": generation.provider_kind,
            "provider_task_id": generation.provider_task_id,
            "current_provider_name": current_provider_name,
        },
    )


def _provider_snapshot(generation: VideoGeneration) -> dict[str, Any]:
    raw_request = getattr(generation, "upstream_request", None)
    request = raw_request if isinstance(raw_request, dict) else {}
    raw_snapshot = request.get("provider_snapshot")
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, dict) else {}
    for key in ("provider_name", "provider_kind", "upstream_model"):
        value = snapshot.get(key)
        if isinstance(value, str) and value.strip():
            snapshot[key] = value.strip()
            continue
        fallback = request.get(key)
        if isinstance(fallback, str) and fallback.strip():
            snapshot[key] = fallback.strip()
        else:
            snapshot.pop(key, None)
    base_url = snapshot.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        snapshot["base_url"] = base_url.strip().rstrip("/")
    else:
        snapshot.pop("base_url", None)
    return snapshot


def _provider_binding_fingerprint(provider: Any) -> str:
    parts = (
        str(provider.kind),
        str(provider.base_url).rstrip("/"),
        str(provider.api_key),
        str(provider.proxy_name or ""),
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _persist_provider_snapshot(
    generation: VideoGeneration,
    provider: Any,
    *,
    upstream_model: str,
) -> None:
    raw_request = getattr(generation, "upstream_request", None)
    request = dict(raw_request) if isinstance(raw_request, dict) else {}
    request["provider_name"] = provider.name
    request["provider_kind"] = provider.kind
    request["upstream_model"] = upstream_model
    request["provider_snapshot"] = {
        "provider_name": provider.name,
        "provider_kind": provider.kind,
        "base_url": provider.base_url.rstrip("/"),
        "proxy_name": provider.proxy_name,
        "upstream_model": upstream_model,
        "binding_fingerprint": _provider_binding_fingerprint(provider),
        "captured_at": _now().isoformat(),
    }
    generation.upstream_request = request


async def _provider_for_generation(generation: VideoGeneration):
    providers = await _provider_config()
    provider_name = (generation.provider_name or "").strip()
    if generation.provider_task_id and not provider_name:
        raise _provider_binding_error(
            generation,
            "submitted video task has no persisted provider identity",
        )
    if provider_name:
        for provider in providers:
            if provider.name != provider_name:
                continue
            if generation.provider_kind and provider.kind != generation.provider_kind:
                raise _provider_binding_error(
                    generation,
                    "persisted video provider kind no longer matches configuration",
                    current_provider_name=provider.name,
                )
            snapshot = _provider_snapshot(generation)
            snapshot_name = snapshot.get("provider_name")
            snapshot_kind = snapshot.get("provider_kind")
            snapshot_base_url = snapshot.get("base_url")
            snapshot_binding = snapshot.get("binding_fingerprint")
            if snapshot_name and snapshot_name != provider.name:
                raise _provider_binding_error(
                    generation,
                    "video provider snapshot name does not match persisted provider",
                    current_provider_name=provider.name,
                )
            if snapshot_kind and snapshot_kind != provider.kind:
                raise _provider_binding_error(
                    generation,
                    "video provider snapshot kind no longer matches configuration",
                    current_provider_name=provider.name,
                )
            if (
                generation.provider_task_id
                and isinstance(snapshot_base_url, str)
                and snapshot_base_url.rstrip("/") != provider.base_url.rstrip("/")
            ):
                raise _provider_binding_error(
                    generation,
                    "video provider endpoint changed after task submission",
                    current_provider_name=provider.name,
                )
            if (
                generation.provider_task_id
                and isinstance(snapshot_binding, str)
                and snapshot_binding != _provider_binding_fingerprint(provider)
            ):
                raise _provider_binding_error(
                    generation,
                    "video provider credentials or route changed after task submission",
                    current_provider_name=provider.name,
                )
            if not generation.provider_task_id and not provider.supports(
                generation.model,
                generation.action,
            ):
                raise _provider_binding_error(
                    generation,
                    "persisted video provider is no longer enabled for this request",
                    current_provider_name=provider.name,
                )
            return provider
        raise _provider_binding_error(
            generation,
            "persisted video provider is no longer configured; refusing provider switch",
        )
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
    snapshot = _provider_snapshot(generation)
    snapshot_name = snapshot.get("provider_name")
    snapshot_kind = snapshot.get("provider_kind")
    if (
        generation.provider_name
        and snapshot_name
        and generation.provider_name != snapshot_name
    ):
        raise _provider_binding_error(
            generation,
            "video provider snapshot conflicts with persisted provider identity",
            current_provider_name=cached_provider_name,
        )
    if (
        generation.provider_kind
        and snapshot_kind
        and generation.provider_kind != snapshot_kind
    ):
        raise _provider_binding_error(
            generation,
            "video provider snapshot conflicts with persisted provider kind",
            current_provider_name=cached_provider_name,
        )
    expected_name = generation.provider_name or snapshot_name
    expected_kind = generation.provider_kind or snapshot_kind
    if cached_provider_name and expected_name and cached_provider_name != expected_name:
        raise _provider_binding_error(
            generation,
            "cached video submit receipt belongs to a different provider",
            current_provider_name=cached_provider_name,
        )
    if cached_provider_kind and expected_kind and cached_provider_kind != expected_kind:
        raise _provider_binding_error(
            generation,
            "cached video submit receipt has a different provider kind",
            current_provider_name=cached_provider_name,
        )
    resolved_name = expected_name or cached_provider_name
    resolved_kind = expected_kind or cached_provider_kind
    if not isinstance(resolved_name, str) or not resolved_name.strip():
        raise _provider_binding_error(
            generation,
            "cached video submit receipt has no provider identity",
        )
    if not isinstance(resolved_kind, str) or not resolved_kind.strip():
        raise _provider_binding_error(
            generation,
            "cached video submit receipt has no provider kind",
            current_provider_name=resolved_name,
        )
    generation.provider_name = resolved_name.strip()
    generation.provider_kind = resolved_kind.strip()
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
                upstream_model = provider.upstream_model_for(
                    generation.model,
                    generation.action,
                )
                if not upstream_model:
                    raise RuntimeError("provider model mapping missing")
                _raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost before state transition",
                )
                input_bytes, input_mime = await _input_image_bytes(session, generation)
                reference_media = await _reference_media_bytes(generation)
                _raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost while loading request media",
                )
                _persist_provider_snapshot(
                    generation,
                    provider,
                    upstream_model=upstream_model,
                )
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


async def _handle_video_upstream_poll_error(
    redis: Any,
    task_id: str,
    exc: VideoUpstreamError,
    *,
    lease_lost: asyncio.Event,
) -> None:
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before upstream error handling",
    )
    if await _finish_cancelled_after_provider_poll_error(
        redis,
        task_id,
        lease_lost=lease_lost,
        exc=exc,
    ):
        return
    if await _schedule_poll_retry(
        redis,
        task_id,
        exc,
        lease_lost=lease_lost,
    ):
        return
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
                    exc,
                    default="upstream_unknown",
                ),
            },
        ),
        fallback_error_message=_video_exception_message(
            exc,
            phase="poll",
        ),
        lease_lost=lease_lost,
    )


async def run_video_poll(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-poll:{new_uuid7()}"
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
    adapter: VideoProviderAdapter | None = None
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
                return
            if not generation.provider_task_id:
                await _enqueue_submit(redis, task_id, defer_s=_POLL_INTERVAL_S)
                return
            _raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost before provider resolution",
            )
            provider = await _provider_for_generation(generation)
            adapter = adapter_for_provider(provider)
            provider_task_id = generation.provider_task_id
            deadline_expired = generation.deadline_at <= _now()
            should_commit_poll_state = False
            if generation.cancel_requested_at is not None:
                await _try_provider_cancel(
                    adapter,
                    generation,
                    lease_lost=lease_lost,
                )
                should_commit_poll_state = True
            if deadline_expired:
                diagnostics = _generation_diagnostics(generation)
                diagnostics.setdefault("deadline_expired_at", _now().isoformat())
                diagnostics["deadline_expired_polling_continues"] = True
                generation.diagnostics = diagnostics
                should_commit_poll_state = True
            if should_commit_poll_state:
                _raise_if_video_lease_lost(
                    lease_lost,
                    "video poll lease lost before state commit",
                )
                await session.commit()

        poll = await adapter.poll(provider_task_id)
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost during provider poll",
        )
        await _apply_poll_result(
            redis,
            task_id,
            poll,
            adapter=adapter,
            lease_lost=lease_lost,
        )
    except _VideoLeaseLost as exc:
        logger.warning("video poll lease lost task=%s err=%s", task_id, exc)
        return
    except VideoUpstreamError as exc:
        try:
            await _handle_video_upstream_poll_error(
                redis,
                task_id,
                lease_lost=lease_lost,
                exc=exc,
            )
        except _VideoLeaseLost as lease_exc:
            logger.warning(
                "video poll lease lost during upstream error handling task=%s err=%s",
                task_id,
                lease_exc,
            )
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("video poll failed task=%s err=%s", task_id, exc, exc_info=True)
        try:
            _raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost before unexpected error handling",
            )
            if _is_retryable_video_exception(exc):
                if await _schedule_poll_retry(
                    redis,
                    task_id,
                    exc,
                    lease_lost=lease_lost,
                ):
                    return
                await _apply_poll_result(
                    redis,
                    task_id,
                    PollResult(
                        status="expired",
                        failure_class=_video_exception_code(
                            exc,
                            default="upstream_unknown",
                        ),
                        upstream_billable=None,
                        raw={
                            "error": _video_exception_message(exc, phase="poll"),
                            "error_code": _video_exception_code(
                                exc,
                                default="upstream_unknown",
                            ),
                        },
                    ),
                    fallback_error_message=_video_exception_message(
                        exc,
                        phase="poll",
                    ),
                    lease_lost=lease_lost,
                )
                return
            await _handle_unexpected_poll_exception(
                redis,
                task_id,
                exc,
                lease_lost=lease_lost,
            )
        except _VideoLeaseLost as lease_exc:
            logger.warning(
                "video poll lease lost during unexpected error handling task=%s err=%s",
                task_id,
                lease_exc,
            )
            return
    finally:
        stop_renewer.set()
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        await _release_lease(redis, task_id, token)


async def _schedule_poll_retry(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    lease_lost: asyncio.Event | None = None,
) -> bool:
    if not _is_retryable_video_exception(exc):
        return False
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before retry scheduling",
    )
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before retry mutation",
        )
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
        diagnostics.pop("unexpected_poll_attempts", None)
        diagnostics.pop("unexpected_poll_fingerprint", None)
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before retry commit",
        )
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
            _raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost before retry event",
            )
            await _publish(
                redis,
                generation,
                EV_VIDEO_PROGRESS,
                retry_after_s=delay_s,
                retry_error_code=error_code,
            )
        except _VideoLeaseLost:
            raise
        except Exception:
            logger.warning(
                "video poll retry publish failed task=%s",
                generation.id,
                exc_info=True,
            )
        try:
            _raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost before retry enqueue",
            )
            await _enqueue_poll(redis, generation.id, defer_s=delay_s)
        except _VideoLeaseLost:
            raise
        except Exception:
            logger.warning(
                "video poll retry enqueue failed task=%s", generation.id, exc_info=True
            )
        return True


async def _handle_unexpected_poll_exception(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    lease_lost: asyncio.Event | None = None,
) -> None:
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before unexpected error persistence",
    )
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error mutation",
        )
        now = _now()
        diagnostics = _generation_diagnostics(generation)
        try:
            previous_attempts = int(diagnostics.get("unexpected_poll_attempts") or 0)
        except (TypeError, ValueError):
            previous_attempts = 0
        error_code = _video_exception_code(exc, default="poll_internal_error")
        error_message = _video_exception_message(exc, phase="poll")
        fingerprint = f"{type(exc).__module__}.{type(exc).__qualname__}:{error_code}"
        if diagnostics.get("unexpected_poll_fingerprint") != fingerprint:
            previous_attempts = 0
        tracking_exhausted = _provider_tracking_window_exhausted(generation, now)
        attempt = previous_attempts + 1
        terminal = tracking_exhausted or attempt >= _MAX_UNEXPECTED_POLL_ATTEMPTS
        item = {
            "at": now.isoformat(),
            "attempt": attempt,
            "error_code": error_code,
            "message": error_message[:500],
            "terminal": terminal,
        }
        _append_bounded_history(
            diagnostics,
            "unexpected_poll_history",
            item,
        )
        diagnostics["unexpected_poll_attempts"] = attempt
        diagnostics["unexpected_poll_fingerprint"] = fingerprint
        diagnostics["last_poll_error"] = {
            **item,
            "retryable": not terminal,
        }
        diagnostics["max_unexpected_poll_attempts"] = _MAX_UNEXPECTED_POLL_ATTEMPTS
        generation.diagnostics = diagnostics
        if terminal:
            await _finish_terminal_failure(
                session,
                redis,
                generation,
                PollResult(
                    status="expired" if tracking_exhausted else "failed",
                    failure_class=error_code,
                    upstream_billable=None,
                    raw={
                        "phase": "poll",
                        "error": error_message,
                        "error_code": error_code,
                        "unexpected_poll_attempts": attempt,
                        "max_unexpected_poll_attempts": (_MAX_UNEXPECTED_POLL_ATTEMPTS),
                        "provider_tracking_window_exhausted": tracking_exhausted,
                        "upstream_cost_ambiguous": True,
                    },
                ),
                fallback_error_message=error_message,
                lease_lost=lease_lost,
            )
            return

        delay_s = (
            _EXTENDED_POLL_INTERVAL_S
            if _poll_window_exhausted(generation, now)
            else _POLL_RETRY_DELAY_S
        )
        generation.status = VideoGenerationStatus.RUNNING.value
        if generation.progress_stage not in {
            VideoGenerationStage.RENDERING.value,
            VideoGenerationStage.FETCHING.value,
        }:
            generation.progress_stage = VideoGenerationStage.RENDERING.value
        generation.progress_pct = max(generation.progress_pct, 20)
        generation.poll_count += 1
        generation.next_poll_at = now + timedelta(seconds=delay_s)
        generation.error_code = None
        generation.error_message = None
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error retry commit",
        )
        await session.commit()
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error retry event",
        )
        await _publish(
            redis,
            generation,
            EV_VIDEO_PROGRESS,
            retry_after_s=delay_s,
            retry_error_code=error_code,
            unexpected_retry_attempt=attempt,
        )
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error retry enqueue",
        )
        await _enqueue_poll(redis, generation.id, defer_s=delay_s)


async def _finish_cancelled_after_provider_poll_error(
    redis: Any,
    task_id: str,
    exc: VideoUpstreamError,
    *,
    lease_lost: asyncio.Event | None = None,
) -> bool:
    if _video_exception_code(exc, default="upstream_unknown") != "upstream_not_ready":
        return False
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before cancel reconciliation",
    )
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before cancel terminal mutation",
        )
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
        if lease_lost is None:
            await _finish_terminal_failure(
                session,
                redis,
                generation,
                poll,
                fallback_error_message="video task cancelled by user",
            )
        else:
            await _finish_terminal_failure(
                session,
                redis,
                generation,
                poll,
                fallback_error_message="video task cancelled by user",
                lease_lost=lease_lost,
            )
        return True


async def _try_provider_cancel(
    adapter: Any,
    generation: VideoGeneration,
    *,
    lease_lost: asyncio.Event | None = None,
) -> None:
    diagnostics = _generation_diagnostics(generation)
    if diagnostics.get("cancel_sent_at") or diagnostics.get("cancel_unsupported_at"):
        return
    try:
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before provider cancel",
        )
        result = await adapter.cancel(generation.provider_task_id)
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost during provider cancel",
        )
        attempted_at = _now().isoformat()
        diagnostics["cancel_attempted_at"] = attempted_at
        diagnostics["cancel_result"] = result.raw if result else None
        if result is None:
            diagnostics["cancel_unsupported_at"] = attempted_at
        elif result.accepted:
            diagnostics["cancel_sent_at"] = attempted_at
        else:
            diagnostics["cancel_rejected_at"] = attempted_at
    except _VideoLeaseLost:
        raise
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
    adapter: VideoProviderAdapter | None = None,
    lease_lost: asyncio.Event | None = None,
) -> None:
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before result persistence",
    )
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before result mutation",
        )

        if poll.status in {"queued", "running"}:
            now = _now()
            if not _provider_tracking_window_exhausted(generation, now):
                await _continue_running_poll(
                    session,
                    redis,
                    generation,
                    poll,
                    now=now,
                    lease_lost=lease_lost,
                )
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
                await _finish_success(
                    session,
                    redis,
                    generation,
                    poll,
                    adapter=adapter,
                    lease_lost=lease_lost,
                )
                return

        await _finish_terminal_failure(
            session,
            redis,
            generation,
            poll,
            fallback_error_message=fallback_error_message,
            lease_lost=lease_lost,
        )


async def _continue_running_poll(
    session,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    now: datetime,
    lease_lost: asyncio.Event | None = None,
) -> None:
    local_window_exhausted = _poll_window_exhausted(generation, now)
    delay_s = _EXTENDED_POLL_INTERVAL_S if local_window_exhausted else _POLL_INTERVAL_S
    diagnostics = _generation_diagnostics(generation)
    diagnostics.pop("unexpected_poll_attempts", None)
    diagnostics.pop("unexpected_poll_fingerprint", None)
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
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before running-state commit",
    )
    await session.commit()
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before progress event",
    )
    await _publish(
        redis,
        generation,
        EV_VIDEO_PROGRESS,
        extended_polling=local_window_exhausted,
        retry_after_s=delay_s,
    )
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before next poll enqueue",
    )
    await _enqueue_poll(redis, generation.id, defer_s=delay_s)


def _cancelled_poll_during_finalization(poll: PollResult) -> PollResult:
    raw = {
        **(poll.raw or {}),
        "reason": "cancel_requested_during_finalization",
        "provider_status": poll.status,
    }
    if poll.usage_total_tokens is None and poll.upstream_billable is None:
        raw["upstream_cost_ambiguous"] = True
    return PollResult(
        status="cancelled",
        failure_class="canceled",
        usage_total_tokens=poll.usage_total_tokens,
        upstream_billable=poll.upstream_billable,
        raw=raw,
    )


async def _finish_success(
    session,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    adapter: VideoProviderAdapter | None = None,
    lease_lost: asyncio.Event | None = None,
) -> None:
    release_provider_name = generation.provider_name
    release_provider_slot = False
    terminal_committed = False
    stored: _StoredVideo | None = None
    artifacts_adopted = False
    try:
        if generation.cancel_requested_at is not None:
            await _finish_terminal_failure(
                session,
                redis,
                generation,
                _cancelled_poll_during_finalization(poll),
                fallback_error_message="video task cancelled by user",
                lease_lost=lease_lost,
            )
            return
        active_adapter = adapter
        if active_adapter is None:
            provider = await _provider_for_generation(generation)
            release_provider_name = release_provider_name or provider.name
            active_adapter = adapter_for_provider(provider)
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before fetching-state commit",
        )
        release_provider_slot = True
        generation.status = VideoGenerationStatus.RUNNING.value
        generation.progress_stage = VideoGenerationStage.FETCHING.value
        generation.progress_pct = max(generation.progress_pct, 96)
        generation.upstream_response = poll.raw
        await session.commit()
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before fetching event",
        )
        await _publish(redis, generation, EV_VIDEO_FETCHING)

        def ensure_active() -> None:
            _raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost during result download",
            )

        downloaded = await active_adapter.download_result(
            poll.video_url or "",
            ensure_active=ensure_active,
        )
        artifact_attempt_id = new_uuid7()
        stored = await _store_video_asset(
            generation,
            downloaded,
            lease_lost=lease_lost,
            artifact_attempt_id=artifact_attempt_id,
        )
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before success row lock",
        )
        await session.refresh(generation, with_for_update=True)
        if generation.status in _TERMINAL_STATUSES:
            release_provider_slot = False
            return
        if generation.cancel_requested_at is not None:
            release_provider_slot = False
            await _finish_terminal_failure(
                session,
                redis,
                generation,
                _cancelled_poll_during_finalization(poll),
                fallback_error_message="video task cancelled by user",
                lease_lost=lease_lost,
            )
            return
        existing = await _video_for_generation(session, generation.id)
        if existing is None:
            session.add(stored.video)
            await session.flush()
            video = stored.video
            adopt_stored_artifacts = True
        else:
            video = existing
            adopt_stored_artifacts = False
        diagnostics = {**(generation.diagnostics or {}), **stored.diagnostics}
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before billing settlement",
        )
        resolution = await resolve_video_billing(
            session,
            generation,
            poll_result=poll,
            reason="succeeded",
        )
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before success mutation",
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before success commit",
        )
        await session.commit()
        terminal_committed = True
        artifacts_adopted = adopt_stored_artifacts
        await worker_flush_balance_cache(session)
    finally:
        if stored is not None and not artifacts_adopted:
            await _delete_video_storage_keys(stored.created_storage_keys)
        lease_still_owned = lease_lost is None or not lease_lost.is_set()
        if (
            release_provider_slot
            and release_provider_name
            and (terminal_committed or lease_still_owned)
        ):
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
    lease_lost: asyncio.Event | None = None,
) -> None:
    release_provider_name = generation.provider_name
    release_provider_slot = False
    terminal_committed = False
    try:
        if generation.status in _TERMINAL_STATUSES:
            return
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before terminal billing",
        )
        release_provider_slot = True
        resolution = await resolve_video_billing(
            session,
            generation,
            poll_result=poll,
            reason=poll.status,
        )
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before terminal mutation",
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
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before terminal commit",
        )
        await session.commit()
        terminal_committed = True
        await worker_flush_balance_cache(session)
    finally:
        lease_still_owned = lease_lost is None or not lease_lost.is_set()
        if (
            release_provider_slot
            and release_provider_name
            and (terminal_committed or lease_still_owned)
        ):
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


def _video_artifact_keys(
    generation: VideoGeneration,
    extension: str,
    *,
    artifact_attempt_id: str | None,
) -> tuple[str, str]:
    base = f"u/{generation.user_id}/v/{generation.id}"
    if artifact_attempt_id is not None:
        attempt_id = artifact_attempt_id.strip()
        if not attempt_id or "/" in attempt_id or "\x00" in attempt_id:
            raise ValueError("invalid video artifact attempt id")
        base = f"{base}/final/{attempt_id}"
    return f"{base}/output{extension}", f"{base}/poster.jpg"


async def _delete_video_storage_keys(keys: tuple[str, ...] | list[str]) -> None:
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return
    results = await asyncio.gather(
        *(asyncio.to_thread(storage.delete, key) for key in unique_keys),
        return_exceptions=True,
    )
    for key, result in zip(unique_keys, results, strict=False):
        if isinstance(result, BaseException):
            logger.warning(
                "video artifact cleanup failed key=%s err=%s",
                key,
                result,
            )


async def _put_video_storage_bytes(
    key: str,
    data: bytes,
    *,
    track_created: bool,
) -> bool:
    if not track_created:
        await storage.aput_bytes(key, data)
        return False
    result = await asyncio.to_thread(storage.put_bytes_result, key, data)
    return bool(result.created)


async def _store_video_asset(
    generation: VideoGeneration,
    data: bytes | DownloadedVideo,
    *,
    lease_lost: asyncio.Event | None = None,
    artifact_attempt_id: str | None = None,
) -> _StoredVideo:
    if isinstance(data, DownloadedVideo):
        return await _store_downloaded_video_asset(
            generation,
            data,
            lease_lost=lease_lost,
            artifact_attempt_id=artifact_attempt_id,
        )
    processed, diagnostics = await asyncio.to_thread(_postprocess_video_bytes, data)
    _raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost after byte video postprocess",
    )
    mime = str(processed.get("mime") or "video/mp4")
    extension = str(processed.get("extension") or ".mp4")
    video_key, poster_key = _video_artifact_keys(
        generation,
        extension,
        artifact_attempt_id=artifact_attempt_id,
    )
    video_bytes = processed["video_bytes"]
    track_created = artifact_attempt_id is not None
    created_keys: list[str] = []
    try:
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before byte artifact storage",
        )
        if await _put_video_storage_bytes(
            video_key,
            video_bytes,
            track_created=track_created,
        ):
            created_keys.append(video_key)
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after byte artifact storage",
        )
        poster_storage_key = None
        if processed.get("poster_bytes"):
            try:
                if await _put_video_storage_bytes(
                    poster_key,
                    processed["poster_bytes"],
                    track_created=track_created,
                ):
                    created_keys.append(poster_key)
                poster_storage_key = poster_key
            except StorageDiskFullError:
                raise
            except Exception as exc:  # noqa: BLE001
                diagnostics["poster_store_error"] = str(exc)[:500]
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after byte poster storage",
        )
        if artifact_attempt_id is not None:
            diagnostics["artifact_attempt_id"] = artifact_attempt_id
        sha = hashlib.sha256(video_bytes).hexdigest()
        video = Video(
            id=new_uuid7(),
            user_id=generation.user_id,
            owner_generation_id=generation.id,
            storage_key=video_key,
            poster_storage_key=poster_storage_key,
            mime=mime,
            width=int(processed.get("width") or 0),
            height=int(processed.get("height") or 0),
            duration_ms=int(processed.get("duration_ms") or 0),
            fps=processed.get("fps"),
            size_bytes=len(video_bytes),
            sha256=sha,
            etag=sha,
            has_audio=bool(processed.get("has_audio")),
            faststart=bool(processed.get("faststart")),
            visibility="private",
            metadata_jsonb=diagnostics,
        )
        return _StoredVideo(
            video=video,
            diagnostics=diagnostics,
            created_storage_keys=tuple(created_keys),
        )
    except BaseException:
        await _delete_video_storage_keys(created_keys)
        raise


async def _store_downloaded_video_asset(
    generation: VideoGeneration,
    downloaded: DownloadedVideo,
    *,
    lease_lost: asyncio.Event | None,
    artifact_attempt_id: str | None,
) -> _StoredVideo:
    processed: _ProcessedVideoFile | None = None
    created_keys: list[str] = []
    try:
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before video postprocess",
        )
        processed = await asyncio.to_thread(_postprocess_video_file, downloaded)
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after video postprocess",
        )
        video_key, poster_key = _video_artifact_keys(
            generation,
            processed.extension,
            artifact_attempt_id=artifact_attempt_id,
        )
        try:
            write_result = await asyncio.to_thread(
                copy_video_file_exclusive_result,
                processed.path,
                storage.path_for(video_key),
                expected_sha256=processed.sha256,
                expected_size=processed.size_bytes,
            )
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise StorageDiskFullError(video_key) from exc
            raise
        if write_result.created:
            created_keys.append(video_key)
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after video artifact storage",
        )
        diagnostics = dict(processed.metadata)
        poster_storage_key = None
        if processed.poster_bytes:
            try:
                if await _put_video_storage_bytes(
                    poster_key,
                    processed.poster_bytes,
                    track_created=True,
                ):
                    created_keys.append(poster_key)
                poster_storage_key = poster_key
            except StorageDiskFullError:
                raise
            except Exception as exc:  # noqa: BLE001
                diagnostics["poster_store_error"] = str(exc)[:500]
        _raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost after video poster storage",
        )
        if artifact_attempt_id is not None:
            diagnostics["artifact_attempt_id"] = artifact_attempt_id
        video = Video(
            id=new_uuid7(),
            user_id=generation.user_id,
            owner_generation_id=generation.id,
            storage_key=video_key,
            poster_storage_key=poster_storage_key,
            mime=processed.mime,
            width=int(processed.metadata.get("width") or 0),
            height=int(processed.metadata.get("height") or 0),
            duration_ms=int(processed.metadata.get("duration_ms") or 0),
            fps=processed.metadata.get("fps"),
            size_bytes=processed.size_bytes,
            sha256=processed.sha256,
            etag=processed.sha256,
            has_audio=bool(processed.metadata.get("has_audio")),
            faststart=processed.faststart,
            visibility="private",
            metadata_jsonb=diagnostics,
        )
        return _StoredVideo(
            video=video,
            diagnostics=diagnostics,
            created_storage_keys=tuple(created_keys),
        )
    except BaseException:
        await _delete_video_storage_keys(created_keys)
        raise
    finally:
        if processed is not None:
            processed.cleanup()
        downloaded.cleanup()


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
