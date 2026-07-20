"""Video submission orchestration and durable receipt handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_VIDEO_CANCELED,
    EV_VIDEO_FAILED,
    EV_VIDEO_PROGRESS,
    EV_VIDEO_SUBMITTED,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import VideoGeneration

from ...video_submit_cache import CachedSubmitResult
from ...video_upstream import PollResult, SubmitResult, VideoSubmitRequest
from ._facade import _g


@dataclass(slots=True)
class _SubmitPreparation:
    generation: VideoGeneration
    cached_submit: CachedSubmitResult | None


async def run_video_generation(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-submit:{_g.new_uuid7()}"
    if not await _g._acquire_lease(redis, task_id, token):
        return
    stop_renewer = asyncio.Event()
    lease_lost = asyncio.Event()
    renewer = asyncio.create_task(
        _g._lease_renewer(
            redis,
            task_id,
            token,
            stop=stop_renewer,
            lost=lease_lost,
        )
    )
    try:
        await _g._run_video_generation_with_lease(
            ctx,
            task_id,
            token=token,
            lease_lost=lease_lost,
        )
    finally:
        stop_renewer.set()
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        await _g._release_lease(redis, task_id, token)


async def handle_existing_pre_submit_state(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    *,
    cached_submit: CachedSubmitResult | None,
    task_id: str,
    token: str,
) -> bool:
    if cached_submit is not None:
        return False
    if generation.status == VideoGenerationStatus.SUBMITTING.value:
        now = _g._now()
        submit_started_at = getattr(
            generation,
            "submit_started_at",
            None,
        ) or getattr(generation, "updated_at", None)
        if submit_started_at is not None and submit_started_at > now - timedelta(
            seconds=_g._SUBMIT_UNKNOWN_AFTER_S
        ):
            generation.next_poll_at = submit_started_at + timedelta(
                seconds=_g._SUBMIT_UNKNOWN_AFTER_S
            )
        else:
            _g._transition_submit_unknown(
                session,
                generation,
                now=now,
                reason="duplicate_worker_observed_stale_submitting",
            )
        await session.commit()
        await _g._release_lease(redis, task_id, token)
        return True
    if generation.deadline_at <= _g._now():
        # Compatibility audit marker: await _mark_pre_submit_expired
        await _g._mark_pre_submit_expired(
            session,
            generation,
            reason="deadline_expired_before_submit",
        )
        await session.commit()
        # Compatibility audit marker: await worker_flush_balance_cache(session)
        await _g.worker_flush_balance_cache(session)
        await _g._release_lease(redis, task_id, token)
        return True
    if (
        generation.cancel_requested_at is not None
        and generation.status == VideoGenerationStatus.QUEUED.value
        and not generation.provider_task_id
    ):
        # Compatibility audit marker: await _mark_pre_submit_canceled
        await _g._mark_pre_submit_canceled(session, generation)
        await session.commit()
        # Compatibility audit marker: await worker_flush_balance_cache(session)
        await _g.worker_flush_balance_cache(session)
        await _g._release_lease(redis, task_id, token)
        return True
    return False


async def resume_existing_provider_task(
    redis: Any,
    generation: VideoGeneration,
    *,
    task_id: str,
    token: str,
) -> bool:
    if not generation.provider_task_id:
        return False
    try:
        await _g._enqueue_poll(redis, generation.id, defer_s=0)
    except Exception:
        _g.logger.warning(
            "video poll enqueue failed task=%s",
            generation.id,
            exc_info=True,
        )
    await _g._release_lease(redis, task_id, token)
    return True


def restore_cached_provider_identity(
    generation: VideoGeneration,
    cached_submit: CachedSubmitResult,
) -> SubmitResult:
    cached_provider_name = _g._cached_submit_provider_name(cached_submit)
    cached_provider_kind = _g._cached_submit_provider_kind(cached_submit)
    snapshot = _g._provider_snapshot(generation)
    snapshot_name = snapshot.get("provider_name")
    snapshot_kind = snapshot.get("provider_kind")
    if (
        generation.provider_name
        and snapshot_name
        and generation.provider_name != snapshot_name
    ):
        raise _g._provider_binding_error(
            generation,
            "video provider snapshot conflicts with persisted provider identity",
            current_provider_name=cached_provider_name,
        )
    if (
        generation.provider_kind
        and snapshot_kind
        and generation.provider_kind != snapshot_kind
    ):
        raise _g._provider_binding_error(
            generation,
            "video provider snapshot conflicts with persisted provider kind",
            current_provider_name=cached_provider_name,
        )
    expected_name = generation.provider_name or snapshot_name
    expected_kind = generation.provider_kind or snapshot_kind
    if cached_provider_name and expected_name and cached_provider_name != expected_name:
        raise _g._provider_binding_error(
            generation,
            "cached video submit receipt belongs to a different provider",
            current_provider_name=cached_provider_name,
        )
    if cached_provider_kind and expected_kind and cached_provider_kind != expected_kind:
        raise _g._provider_binding_error(
            generation,
            "cached video submit receipt has a different provider kind",
            current_provider_name=cached_provider_name,
        )
    resolved_name = expected_name or cached_provider_name
    resolved_kind = expected_kind or cached_provider_kind
    if not isinstance(resolved_name, str) or not resolved_name.strip():
        raise _g._provider_binding_error(
            generation,
            "cached video submit receipt has no provider identity",
        )
    if not isinstance(resolved_kind, str) or not resolved_kind.strip():
        raise _g._provider_binding_error(
            generation,
            "cached video submit receipt has no provider kind",
            current_provider_name=resolved_name,
        )
    generation.provider_name = resolved_name.strip()
    generation.provider_kind = resolved_kind.strip()
    return _g._cached_submit_result(cached_submit)


async def reserve_video_submit_slot(
    redis: Any,
    generation: VideoGeneration,
    provider: Any,
    *,
    task_id: str,
    token: str,
    cached: bool,
) -> bool:
    acquired = await _g._acquire_provider_slot(
        redis,
        provider.name,
        _g._provider_submit_concurrency(provider, generation),
        generation.id,
        exclusive=_g._provider_submit_is_exclusive(provider, generation),
    )
    if acquired:
        return True
    try:
        await _g._enqueue_submit(
            redis,
            generation.id,
            defer_s=_g._POLL_INTERVAL_S,
        )
    except Exception:
        label = "cached submit" if cached else "submit"
        _g.logger.warning(
            "video %s re-enqueue failed task=%s",
            label,
            generation.id,
            exc_info=True,
        )
    await _g._release_lease(redis, task_id, token)
    return False


async def restore_pre_submit_after_lease_loss(
    redis: Any,
    task_id: str,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
) -> None:
    should_requeue = False
    try:
        async with _g.SessionLocal() as session:
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
                generation.next_poll_at = _g._now()
                generation.submit_started_at = None
                diagnostics = _g._generation_diagnostics(generation)
                _g._append_bounded_history(
                    diagnostics,
                    "submit_recovery_history",
                    {
                        "at": _g._now().isoformat(),
                        "reason": "lease_lost_before_upstream",
                        "submission_epoch": submission_epoch,
                    },
                )
                generation.diagnostics = diagnostics
                should_requeue = True
            await session.commit()
    except Exception:
        _g.logger.error(
            "video pre-submit lease recovery failed task=%s epoch=%s",
            task_id,
            submission_epoch,
            exc_info=True,
        )
    if provider_name:
        await _g._release_provider_slot(redis, provider_name, task_id)
    if should_requeue:
        try:
            await _g._enqueue_submit(redis, task_id, defer_s=0)
        except Exception:
            _g.logger.warning(
                "video pre-submit lease recovery enqueue failed task=%s",
                task_id,
                exc_info=True,
            )


async def _prepare_submit_row(
    session: Any,
    redis: Any,
    *,
    task_id: str,
    token: str,
) -> _SubmitPreparation | None:
    generation = (
        await session.execute(
            select(VideoGeneration)
            .where(VideoGeneration.id == task_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if generation is None or generation.status in _g._NON_RESUBMIT_STATUSES:
        await _g._release_lease(redis, task_id, token)
        return None
    if await _g._resume_existing_provider_task(
        redis,
        generation,
        task_id=task_id,
        token=token,
    ):
        return None
    cached_submit = await _g._load_submit_result(redis, generation.id)
    if await _g._handle_existing_pre_submit_state(
        session,
        redis,
        generation,
        cached_submit=cached_submit,
        task_id=task_id,
        token=token,
    ):
        return None
    return _SubmitPreparation(
        generation=generation,
        cached_submit=cached_submit,
    )


async def run_video_generation_with_lease(
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
        async with _g.SessionLocal() as session:
            prepared = await _prepare_submit_row(
                session,
                redis,
                task_id=task_id,
                token=token,
            )
            if prepared is None:
                return
            generation = prepared.generation
            if prepared.cached_submit is not None:
                submission_epoch = int(getattr(generation, "submission_epoch", 0) or 0)
                result = _g._restore_cached_provider_identity(
                    generation,
                    prepared.cached_submit,
                )
                await session.commit()
            else:
                provider = await _g._provider_for_generation(generation)
                if not await _g._reserve_video_submit_slot(
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
                _g._raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost before state transition",
                )
                input_bytes, input_mime = await _g._input_image_bytes(
                    session,
                    generation,
                )
                reference_media = await _g._reference_media_bytes(generation)
                _g._raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost while loading request media",
                )
                _g._persist_provider_snapshot(
                    generation,
                    provider,
                    upstream_model=upstream_model,
                )
                generation.status = VideoGenerationStatus.SUBMITTING.value
                generation.progress_stage = VideoGenerationStage.SUBMITTING.value
                generation.progress_pct = max(generation.progress_pct, 5)
                submit_started_at = _g._now()
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
                    seconds=_g._SUBMIT_UNKNOWN_AFTER_S
                )
                await session.commit()

                _g._raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost before upstream call",
                )
                adapter = _g.adapter_for_provider(provider)
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
                        input_image_url=_g._input_image_url(generation),
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
                    # Ordering contract: await _store_submit_result before the
                    # post-submit lease fence below.
                    await _g._store_submit_result(
                        redis,
                        task_id,
                        result,
                        provider_name=provider.name,
                        provider_kind=provider.kind,
                    )
                except Exception:
                    _g.logger.warning(
                        "video submit cache store failed task=%s",
                        task_id,
                        exc_info=True,
                    )
                _g._raise_if_video_lease_lost(
                    lease_lost,
                    "video submit lease lost after upstream call",
                )
    except Exception as exc:  # noqa: BLE001
        await _g._handle_video_submit_exception(
            redis,
            task_id,
            exc,
            provider_name=slot_provider_name,
            submission_epoch=submission_epoch,
            upstream_invoked=upstream_invoked,
            provider_supports_idempotency=provider_supports_idempotency,
        )
        return

    persisted = await _g._persist_video_submit_receipt(
        redis,
        task_id,
        result,
        submission_epoch=submission_epoch,
        lease_lost=lease_lost,
    )
    if not persisted:
        await _g._enqueue_cached_submit_recovery(
            redis,
            task_id,
            defer_s=_g._POLL_INTERVAL_S,
        )
        return
    try:
        await _g._enqueue_poll(redis, task_id)
    except Exception:
        _g.logger.warning(
            "video poll enqueue failed task=%s",
            task_id,
            exc_info=True,
        )
    finally:
        await _g._release_lease(redis, task_id, token)


async def handle_video_submit_exception(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
    upstream_invoked: bool,
    provider_supports_idempotency: bool,
) -> None:
    if isinstance(exc, _g._VideoLeaseLost):
        _g.logger.warning(
            "video submit lease lost; stale worker will not mutate task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        if not upstream_invoked:
            await _g._restore_pre_submit_after_lease_loss(
                redis,
                task_id,
                provider_name=provider_name,
                submission_epoch=submission_epoch,
            )
        return
    if (
        upstream_invoked
        and not provider_supports_idempotency
        and _g._submit_outcome_unknown(exc)
    ):
        try:
            await _g._mark_submit_unknown(
                task_id,
                exc,
                provider_name=provider_name,
                submission_epoch=submission_epoch,
            )
        except Exception:
            _g.logger.error(
                "video submit outcome unknown persistence failed task=%s epoch=%s",
                task_id,
                submission_epoch,
                exc_info=True,
            )
        return
    await _g._fail_before_submit(
        redis,
        task_id,
        exc,
        provider_name=provider_name,
        submission_epoch=submission_epoch,
    )


async def persist_video_submit_receipt(
    redis: Any,
    task_id: str,
    result: SubmitResult,
    *,
    submission_epoch: int | None,
    lease_lost: asyncio.Event,
) -> bool:
    try:
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video submit lease lost before receipt persistence",
        )
        async with _g.SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if generation is None:
                _g.logger.warning(
                    "video submit receipt fenced out task=%s epoch=%s",
                    task_id,
                    submission_epoch,
                )
                return False
            if generation.status in _g._TERMINAL_STATUSES:
                return False
            generation.provider_task_id = result.provider_task_id
            generation.upstream_response = result.raw
            generation.status = VideoGenerationStatus.SUBMITTED.value
            generation.progress_stage = VideoGenerationStage.RENDERING.value
            generation.progress_pct = max(generation.progress_pct, 10)
            generation.submitted_at = _g._now()
            generation.next_poll_at = _g._now() + timedelta(seconds=_g._POLL_INTERVAL_S)
            diagnostics = _g._generation_diagnostics(generation)
            diagnostics["submit_receipt"] = {
                "submission_epoch": submission_epoch,
                "provider_task_id": result.provider_task_id,
                "provider_idempotency_key": getattr(
                    generation,
                    "provider_idempotency_key",
                    None,
                ),
                "persisted_at": _g._now().isoformat(),
            }
            generation.diagnostics = diagnostics
            await session.commit()
            await _g._publish_after_commit(redis, generation, EV_VIDEO_SUBMITTED)
            return True
    except _g._VideoLeaseLost:
        _g.logger.warning(
            "video submit receipt skipped after lease loss task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        return False
    except Exception:
        _g.logger.warning(
            "video submit persist failed task=%s",
            task_id,
            exc_info=True,
        )
        return False


async def mark_pre_submit_canceled(
    session: Any,
    generation: VideoGeneration,
) -> None:
    generation.status = VideoGenerationStatus.CANCELED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "canceled"
    generation.error_message = "cancelled before upstream submission"
    generation.finished_at = _g._now()
    await _g.resolve_video_billing(
        session,
        generation,
        poll_result=PollResult(
            status="cancelled",
            upstream_billable=False,
            raw={"reason": "pre_submit_cancel"},
        ),
        reason="pre_submit_cancel",
    )
    _g._queue_video_event(session, generation, EV_VIDEO_CANCELED)


async def mark_pre_submit_expired(
    session: Any,
    generation: VideoGeneration,
    *,
    reason: str,
) -> None:
    diagnostics = _g._generation_diagnostics(generation)
    diagnostics["pre_submit_expired_at"] = _g._now().isoformat()
    generation.status = VideoGenerationStatus.EXPIRED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "deadline_expired"
    generation.error_message = "video task expired before upstream submission"
    generation.diagnostics = diagnostics
    generation.finished_at = _g._now()
    await _g.resolve_video_billing(
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
    _g._queue_video_event(session, generation, EV_VIDEO_FAILED)


def transition_submit_unknown(
    session: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
    reason: str,
    last_error: dict[str, Any] | None = None,
) -> None:
    diagnostics = _g._generation_diagnostics(generation)
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
    generation.next_poll_at = now + timedelta(
        seconds=_g._SUBMIT_UNKNOWN_FINALIZE_AFTER_S
    )
    generation.diagnostics = diagnostics
    _g._queue_video_event(
        session,
        generation,
        EV_VIDEO_PROGRESS,
        submission_unknown=True,
    )


async def mark_submit_unknown(
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
) -> bool:
    async with _g.SessionLocal() as session:
        filters = [VideoGeneration.id == task_id]
        if submission_epoch is not None:
            filters.append(VideoGeneration.submission_epoch == submission_epoch)
        generation = (
            await session.execute(
                select(VideoGeneration).where(*filters).with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _g._TERMINAL_STATUSES:
            return False
        now = _g._now()
        error_code = _g._video_exception_code(exc, default="upstream_unknown")
        error_message = _g._video_exception_message(exc, phase="submit")
        generation.provider_name = generation.provider_name or provider_name
        _g._transition_submit_unknown(
            session,
            generation,
            now=now,
            reason="ambiguous_non_idempotent_submit_error",
            last_error={
                "at": now.isoformat(),
                "attempt": _g._generation_attempt(generation),
                "error_code": error_code,
                "message": error_message[:500],
                "retryable": False,
                "outcome_unknown": True,
            },
        )
        await session.commit()
        return True


async def fail_before_submit(
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
        async with _g.SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if generation is None or generation.status in _g._NON_RESUBMIT_STATUSES:
                return
            release_provider_slot = True
            release_provider_name = release_provider_name or generation.provider_name
            if await _g._schedule_submit_retry(session, redis, generation, exc):
                return
            error_code = _g._video_exception_code(
                exc,
                default="provider_unavailable",
            )
            error_message = _g._video_exception_message(exc, phase="submit")
            _g.logger.warning(
                "video submit failed task=%s attempt=%s code=%s error=%s",
                task_id,
                _g._generation_attempt(generation),
                error_code,
                error_message,
                exc_info=_g._exception_log_info(exc),
            )
            diagnostics = _g._generation_diagnostics(generation)
            diagnostics["last_submit_error"] = {
                "at": _g._now().isoformat(),
                "attempt": _g._generation_attempt(generation),
                "error_code": error_code,
                "message": error_message[:500],
                "retryable": _g._is_retryable_video_exception(exc),
                "terminal": True,
            }
            generation.status = VideoGenerationStatus.FAILED.value
            generation.progress_stage = VideoGenerationStage.FINISHED.value
            generation.progress_pct = 100
            generation.error_code = error_code
            generation.error_message = error_message
            generation.diagnostics = diagnostics
            generation.finished_at = _g._now()
            billable_hint = _g._submit_failure_billable_hint(exc)
            await _g.resolve_video_billing(
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
                reason=(
                    "submit_failed_ambiguous_upstream_cost"
                    if billable_hint is None
                    else "submit_failed_before_upstream_cost"
                ),
            )
            _g._queue_video_event(session, generation, EV_VIDEO_FAILED)
            await session.commit()
            # Compatibility audit marker: await worker_flush_balance_cache(session)
            await _g.worker_flush_balance_cache(session)
    finally:
        if release_provider_slot and release_provider_name:
            # Compatibility audit marker:
            # _release_provider_slot(redis, release_provider_name, task_id)
            await _g._release_provider_slot(
                redis,
                release_provider_name,
                task_id,
            )


async def schedule_submit_retry(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    exc: Exception,
) -> bool:
    if not _g._is_retryable_video_exception(exc):
        return False
    attempt = _g._generation_attempt(generation)
    if attempt >= _g._MAX_SUBMIT_ATTEMPTS:
        return False
    now = _g._now()
    remaining_s = int((generation.deadline_at - now).total_seconds())
    if remaining_s <= 1:
        return False
    delay_s = max(
        1,
        min(_g._submit_retry_delay_s(attempt), remaining_s - 1),
    )
    error_code = _g._video_exception_code(exc, default="provider_unavailable")
    error_message = _g._video_exception_message(exc, phase="submit")
    diagnostics = _g._generation_diagnostics(generation)
    retry_item = {
        "at": now.isoformat(),
        "attempt": attempt,
        "error_code": error_code,
        "message": error_message[:500],
        "next_retry_delay_s": delay_s,
    }
    _g._append_bounded_history(
        diagnostics,
        "submit_retry_history",
        retry_item,
    )
    diagnostics["last_submit_error"] = {**retry_item, "retryable": True}
    diagnostics["submit_retry_count"] = len(diagnostics["submit_retry_history"])
    generation.status = VideoGenerationStatus.QUEUED.value
    generation.progress_stage = VideoGenerationStage.QUEUED.value
    generation.progress_pct = max(generation.progress_pct, 5)
    generation.next_poll_at = now + timedelta(seconds=delay_s)
    generation.error_code = None
    generation.error_message = None
    generation.diagnostics = diagnostics
    _g._queue_video_event(
        session,
        generation,
        EV_VIDEO_PROGRESS,
        retry_transition=True,
        retry_after_s=delay_s,
        retry_attempt=attempt,
        retry_error_code=error_code,
    )
    await session.commit()
    _g.logger.info(
        "video submit retry scheduled task=%s attempt=%s delay_s=%s code=%s error=%s",
        generation.id,
        attempt,
        delay_s,
        error_code,
        error_message,
    )
    try:
        await _g._enqueue_submit(redis, generation.id, defer_s=delay_s)
    except Exception:
        _g.logger.warning(
            "video submit retry enqueue failed task=%s",
            generation.id,
            exc_info=True,
        )
    return True


__all__ = [
    "fail_before_submit",
    "handle_existing_pre_submit_state",
    "handle_video_submit_exception",
    "mark_pre_submit_canceled",
    "mark_pre_submit_expired",
    "mark_submit_unknown",
    "persist_video_submit_receipt",
    "reserve_video_submit_slot",
    "restore_cached_provider_identity",
    "restore_pre_submit_after_lease_loss",
    "resume_existing_provider_task",
    "run_video_generation",
    "run_video_generation_with_lease",
    "schedule_submit_retry",
    "transition_submit_unknown",
]
