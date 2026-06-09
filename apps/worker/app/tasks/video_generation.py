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
    task_channel,
)
from lumen_core.models import Image, Video, VideoGeneration, new_uuid7
from lumen_core.video_providers import (
    parse_video_provider_config_json,
    select_video_provider,
)

from .. import runtime_settings
from ..db import SessionLocal
from ..storage import StorageDiskFullError, storage
from ..sse_publish import publish_event
from ..video_billing import resolve_video_billing
from ..video_upstream import (
    PollResult,
    SubmitResult,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
    adapter_for_provider,
)


logger = logging.getLogger(__name__)

_LEASE_TTL_S = 120
_POLL_INTERVAL_S = 8
_MAX_POLL_COUNT = 120
_MAX_SUBMIT_ATTEMPTS = 4
_SUBMIT_RETRY_DELAYS_S = (8, 24, 60)
_POLL_RETRY_DELAY_S = 12
_RECON_STALE_AFTER_S = 30
_RECON_LIMIT = 100
_SUBMIT_RESULT_CACHE_TTL_S = 7 * 24 * 60 * 60
_SUBMIT_RESULT_CACHE_PREFIX = "video:submit_result:"
_VIDEO_PROVIDER_SLOT_STALE_AFTER_S = 30 * 60
_VIDEO_PROVIDER_SLOT_TTL_S = 2 * 60 * 60
_VIDEO_PROVIDER_SLOT_PREFIX = "video:provider_slot:"
_VIDEO_PROVIDER_SLOT_LOCK_PREFIX = "video:provider_slot_lock:"
_TERMINAL_STATUSES = {
    VideoGenerationStatus.SUCCEEDED.value,
    VideoGenerationStatus.FAILED.value,
    VideoGenerationStatus.CANCELED.value,
    VideoGenerationStatus.EXPIRED.value,
}
_RETRYABLE_VIDEO_ERROR_CODES = {
    "capacity",
    "fetch_failed",
    "provider_error",
    "upstream_network_error",
    "upstream_timeout",
    "upstream_unknown",
}


@dataclass(frozen=True)
class _StoredVideo:
    video: Video
    diagnostics: dict[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def _submit_retry_delay_s(attempt: int) -> int:
    index = max(0, min(attempt - 1, len(_SUBMIT_RETRY_DELAYS_S) - 1))
    return _SUBMIT_RETRY_DELAYS_S[index]


def _generation_attempt(generation: VideoGeneration) -> int:
    return int(getattr(generation, "attempt", 0) or 0)


def _generation_diagnostics(generation: VideoGeneration) -> dict[str, Any]:
    raw = getattr(generation, "diagnostics", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


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


def _submit_result_cache_key(task_id: str) -> str:
    return f"{_SUBMIT_RESULT_CACHE_PREFIX}{task_id}"


async def _store_submit_result(redis: Any, task_id: str, result: SubmitResult) -> None:
    await redis.set(
        _submit_result_cache_key(task_id),
        json.dumps(
            {
                "provider_task_id": result.provider_task_id,
                "raw": result.raw,
            },
            separators=(",", ":"),
        ),
        ex=_SUBMIT_RESULT_CACHE_TTL_S,
    )


async def _load_submit_result(redis: Any, task_id: str) -> SubmitResult | None:
    raw = await redis.get(_submit_result_cache_key(task_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("video submit cache decode failed task=%s", task_id, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    provider_task_id = payload.get("provider_task_id")
    raw_result = payload.get("raw")
    if not isinstance(provider_task_id, str) or not provider_task_id:
        return None
    if not isinstance(raw_result, dict):
        return None
    return SubmitResult(provider_task_id=provider_task_id, raw=raw_result)


async def _enqueue_poll(
    redis: Any, task_id: str, *, defer_s: int = _POLL_INTERVAL_S
) -> None:
    await redis.enqueue_job(
        "run_video_poll",
        task_id,
        _defer_by=defer_s,
        _job_id=f"lumen:video_poll:{task_id}:{int(time.time())}",
    )


async def _enqueue_submit(
    redis: Any, task_id: str, *, defer_s: int = _POLL_INTERVAL_S
) -> None:
    await redis.enqueue_job(
        "run_video_generation",
        task_id,
        _defer_by=defer_s,
        _job_id=f"lumen:video_generation:reconcile:{task_id}:{int(time.time())}",
    )


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


async def _publish(
    redis: Any, generation: VideoGeneration, event_name: str, **extra: Any
) -> None:
    data = {
        "video_generation_id": generation.id,
        "kind": "video_generation",
        "status": generation.status,
        "stage": generation.progress_stage,
        "progress_pct": generation.progress_pct,
        "video_id": extra.pop("video_id", None),
        "error_code": generation.error_code,
        "error_message": generation.error_message,
        **extra,
    }
    await publish_event(
        redis,
        generation.user_id,
        task_channel(generation.id),
        event_name,
        data,
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
        if kind not in {"image", "video"}:
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            result.append(
                VideoReferenceMedia(kind=kind, url=url.strip())  # type: ignore[arg-type]
            )
            continue
        if kind == "video":
            raise RuntimeError("reference video snapshot missing public URL")
        storage_key = item.get("storage_key")
        if not isinstance(storage_key, str) or not storage_key.strip():
            raise RuntimeError("reference media storage key missing")
        mime = item.get("mime") if isinstance(item.get("mime"), str) else None
        result.append(
            VideoReferenceMedia(
                kind=kind,  # type: ignore[arg-type]
                data=await storage.aget_bytes(storage_key),
                mime=mime,
            )
        )
    if not result:
        raise RuntimeError("reference media snapshot has no usable entries")
    return result


async def run_video_generation(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-submit:{new_uuid7()}"
    slot_provider_name: str | None = None
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
            cached_submit = None
            if generation.provider_task_id:
                try:
                    await _enqueue_poll(redis, generation.id, defer_s=0)
                except Exception:
                    logger.warning(
                        "video poll enqueue failed task=%s",
                        generation.id,
                        exc_info=True,
                    )
                await _release_lease(redis, task_id, token)
                return
            cached_submit = await _load_submit_result(redis, generation.id)
            if (
                cached_submit is None
                and generation.cancel_requested_at is not None
                and generation.status == VideoGenerationStatus.QUEUED.value
                and not generation.provider_task_id
            ):
                await _mark_pre_submit_canceled(session, redis, generation)
                await session.commit()
                await _release_lease(redis, task_id, token)
                return
            if cached_submit is not None:
                result = cached_submit
            else:
                provider = await _provider_for_generation(generation)
                slot_acquired = await _acquire_provider_slot(
                    redis,
                    provider.name,
                    provider.concurrency,
                    generation.id,
                )
                if not slot_acquired:
                    try:
                        await _enqueue_submit(
                            redis,
                            generation.id,
                            defer_s=_POLL_INTERVAL_S,
                        )
                    except Exception:
                        logger.warning(
                            "video submit re-enqueue failed task=%s",
                            generation.id,
                            exc_info=True,
                        )
                    await _release_lease(redis, task_id, token)
                    return
                slot_provider_name = provider.name
                input_bytes, input_mime = await _input_image_bytes(session, generation)
                reference_media = await _reference_media_bytes(generation)
                generation.provider_name = provider.name
                generation.provider_kind = provider.kind
                generation.status = VideoGenerationStatus.SUBMITTING.value
                generation.progress_stage = VideoGenerationStage.SUBMITTING.value
                generation.progress_pct = max(generation.progress_pct, 5)
                generation.started_at = generation.started_at or _now()
                generation.attempt += 1
                await session.commit()

            if cached_submit is None:
                upstream_model = provider.upstream_model_for(
                    generation.model, generation.action
                )
                if not upstream_model:
                    raise RuntimeError("provider model mapping missing")
                adapter = adapter_for_provider(provider)
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
                    )
                )
                try:
                    await _store_submit_result(redis, task_id, result)
                except Exception:
                    logger.warning(
                        "video submit cache store failed task=%s",
                        task_id,
                        exc_info=True,
                    )
    except Exception as exc:  # noqa: BLE001
        try:
            await _fail_before_submit(
                redis,
                task_id,
                exc,
                provider_name=slot_provider_name,
            )
        finally:
            await _release_lease(redis, task_id, token)
        return

    try:
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
            generation.provider_task_id = result.provider_task_id
            generation.upstream_response = result.raw
            generation.status = VideoGenerationStatus.SUBMITTED.value
            generation.progress_stage = VideoGenerationStage.RENDERING.value
            generation.progress_pct = max(generation.progress_pct, 10)
            generation.submitted_at = _now()
            generation.next_poll_at = _now() + timedelta(seconds=_POLL_INTERVAL_S)
            await session.commit()
            await _publish(redis, generation, EV_VIDEO_SUBMITTED)
    except Exception:
        logger.warning("video submit persist failed task=%s", task_id, exc_info=True)
        await _release_lease(redis, task_id, token)
        return

    try:
        await _enqueue_poll(redis, task_id)
    except Exception:
        logger.warning("video poll enqueue failed task=%s", task_id, exc_info=True)
    finally:
        await _release_lease(redis, task_id, token)


async def _mark_pre_submit_canceled(
    session, redis: Any, generation: VideoGeneration
) -> None:
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
    await _publish(redis, generation, EV_VIDEO_CANCELED)


async def _fail_before_submit(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None = None,
) -> None:
    release_provider_name = provider_name
    try:
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
            await resolve_video_billing(
                session,
                generation,
                poll_result=PollResult(
                    status="failed",
                    upstream_billable=False,
                    raw={
                        "phase": "submit",
                        "error": error_message,
                        "error_code": error_code,
                    },
                ),
                reason="submit_failed_before_upstream_cost",
            )
            await session.commit()
            await _publish(redis, generation, EV_VIDEO_FAILED)
    finally:
        if release_provider_name:
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
    generation.status = VideoGenerationStatus.SUBMITTING.value
    generation.progress_stage = VideoGenerationStage.SUBMITTING.value
    generation.progress_pct = max(generation.progress_pct, 5)
    generation.next_poll_at = now + timedelta(seconds=delay_s)
    generation.error_code = None
    generation.error_message = None
    generation.diagnostics = diagnostics
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
        await _publish(
            redis,
            generation,
            EV_VIDEO_PROGRESS,
            retry_after_s=delay_s,
            retry_attempt=attempt,
            retry_error_code=error_code,
        )
    except Exception:
        logger.warning(
            "video submit retry publish failed task=%s",
            generation.id,
            exc_info=True,
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
            if generation.cancel_requested_at is not None:
                await _try_provider_cancel(adapter, generation)
                await session.commit()
            deadline_expired = generation.deadline_at <= _now()

        poll = (
            PollResult(
                status="expired",
                failure_class="timeout",
                upstream_billable=None,
                raw={"deadline_expired": True},
            )
            if deadline_expired
            else await adapter.poll(generation.provider_task_id)
        )
        await _apply_poll_result(redis, task_id, poll)
    except VideoUpstreamError as exc:
        if not await _schedule_poll_retry(redis, task_id, exc):
            await _apply_poll_result(
                redis,
                task_id,
                PollResult(
                    status="failed",
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
        if generation.deadline_at <= now or generation.poll_count >= _MAX_POLL_COUNT:
            return False
        remaining_s = int((generation.deadline_at - now).total_seconds())
        if remaining_s <= 1:
            return False
        delay_s = max(1, min(_POLL_RETRY_DELAY_S, remaining_s - 1))
        error_code = _video_exception_code(exc, default="upstream_unknown")
        error_message = _video_exception_message(exc, phase="poll")
        diagnostics = dict(generation.diagnostics or {})
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

        if (
            poll.status in {"queued", "running"}
            and generation.poll_count < _MAX_POLL_COUNT
        ):
            generation.status = VideoGenerationStatus.RUNNING.value
            generation.progress_stage = VideoGenerationStage.RENDERING.value
            generation.progress_pct = max(
                generation.progress_pct,
                min(95, int(poll.progress if poll.progress is not None else 20)),
            )
            generation.poll_count += 1
            generation.upstream_response = poll.raw
            generation.next_poll_at = _now() + timedelta(seconds=_POLL_INTERVAL_S)
            await session.commit()
            await _publish(redis, generation, EV_VIDEO_PROGRESS)
            await _enqueue_poll(redis, task_id)
            return

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
        await session.commit()
        await worker_flush_balance_cache(session)
        await _publish(redis, generation, EV_VIDEO_SUCCEEDED, video_id=video.id)
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
        await session.commit()
        await worker_flush_balance_cache(session)
        await _publish(
            redis,
            generation,
            EV_VIDEO_CANCELED
            if internal_status == VideoGenerationStatus.CANCELED.value
            else EV_VIDEO_FAILED,
        )
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
    moov = data.find(b"moov")
    mdat = data.find(b"mdat")
    return moov != -1 and (mdat == -1 or moov < mdat)


async def reconcile_video_tasks(ctx: dict[str, Any]) -> int:
    redis = ctx["redis"]
    cutoff = _now() - timedelta(seconds=_RECON_STALE_AFTER_S)
    touched = 0
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
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.deadline_at <= _now() and row.provider_task_id:
                await _enqueue_poll(redis, row.id, defer_s=0)
            elif row.provider_task_id:
                await _enqueue_poll(redis, row.id, defer_s=0)
            else:
                row.status = VideoGenerationStatus.QUEUED.value
                row.progress_stage = VideoGenerationStage.QUEUED.value
                await _enqueue_submit(redis, row.id, defer_s=_POLL_INTERVAL_S)
            touched += 1
        await session.commit()
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
