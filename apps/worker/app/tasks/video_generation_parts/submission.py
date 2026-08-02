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
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import VideoGeneration

from ...video_submit_cache import CachedSubmitResult
from ...video_upstream_service import PollResult, SubmitResult, VideoSubmitRequest
from . import submit_delivery as submit_delivery_evidence
from .errors import submit_delivery_proven_absent
from .runtime import video_ports
from .submission_fence import relock_pre_submit_dispatch
from .submission_preparation import (
    handle_existing_pre_submit_state,
    prepare_submit_row as _prepare_submit_row,
    relock_cached_submit_row as _relock_cached_submit_row,
    relock_pre_submit_transition as _relock_pre_submit_transition,
    resume_existing_provider_task,
)
from .submit_state import (
    SubmitPreparation as _SubmitPreparation,  # noqa: F401
    record_submit_delivery as _record_submit_delivery,
    submit_delivery_state as _submit_delivery_state,
)

mark_submit_unknown = submit_delivery_evidence.mark_submit_unknown
_persist_video_submit_receipt_impl = (
    submit_delivery_evidence.persist_video_submit_receipt
)
_transition_submit_unknown_impl = submit_delivery_evidence.transition_submit_unknown


@dataclass(slots=True)
class _SubmitAttemptState:
    result: SubmitResult | None = None
    slot_provider_name: str | None = None
    submission_epoch: int | None = None
    upstream_invoked: bool = False
    provider_supports_idempotency: bool = False


async def run_video_generation(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-submit:{video_ports().new_uuid7()}"
    if not await video_ports()._acquire_lease(redis, task_id, token):
        return
    stop_renewer = asyncio.Event()
    lease_lost = asyncio.Event()
    renewer = asyncio.create_task(
        video_ports()._lease_renewer(
            redis,
            task_id,
            token,
            stop=stop_renewer,
            lost=lease_lost,
        )
    )
    try:
        await video_ports()._run_video_generation_with_lease(
            ctx,
            task_id,
            token=token,
            lease_lost=lease_lost,
        )
    finally:
        stop_renewer.set()
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        await video_ports()._release_lease(redis, task_id, token)


def restore_cached_provider_identity(
    generation: VideoGeneration,
    cached_submit: CachedSubmitResult,
) -> SubmitResult:
    cached_provider_name = video_ports()._cached_submit_provider_name(cached_submit)
    cached_provider_kind = video_ports()._cached_submit_provider_kind(cached_submit)
    snapshot = video_ports()._provider_snapshot(generation)
    snapshot_name = snapshot.get("provider_name")
    snapshot_kind = snapshot.get("provider_kind")
    if (
        generation.provider_name
        and snapshot_name
        and generation.provider_name != snapshot_name
    ):
        raise video_ports()._provider_binding_error(
            generation,
            "video provider snapshot conflicts with persisted provider identity",
            current_provider_name=cached_provider_name,
        )
    if (
        generation.provider_kind
        and snapshot_kind
        and generation.provider_kind != snapshot_kind
    ):
        raise video_ports()._provider_binding_error(
            generation,
            "video provider snapshot conflicts with persisted provider kind",
            current_provider_name=cached_provider_name,
        )
    expected_name = generation.provider_name or snapshot_name
    expected_kind = generation.provider_kind or snapshot_kind
    if cached_provider_name and expected_name and cached_provider_name != expected_name:
        raise video_ports()._provider_binding_error(
            generation,
            "cached video submit receipt belongs to a different provider",
            current_provider_name=cached_provider_name,
        )
    if cached_provider_kind and expected_kind and cached_provider_kind != expected_kind:
        raise video_ports()._provider_binding_error(
            generation,
            "cached video submit receipt has a different provider kind",
            current_provider_name=cached_provider_name,
        )
    resolved_name = expected_name or cached_provider_name
    resolved_kind = expected_kind or cached_provider_kind
    if not isinstance(resolved_name, str) or not resolved_name.strip():
        raise video_ports()._provider_binding_error(
            generation,
            "cached video submit receipt has no provider identity",
        )
    if not isinstance(resolved_kind, str) or not resolved_kind.strip():
        raise video_ports()._provider_binding_error(
            generation,
            "cached video submit receipt has no provider kind",
            current_provider_name=resolved_name,
        )
    generation.provider_name = resolved_name.strip()
    generation.provider_kind = resolved_kind.strip()
    return video_ports()._cached_submit_result(cached_submit)


async def reserve_video_submit_slot(
    redis: Any,
    generation: VideoGeneration,
    provider: Any,
    *,
    task_id: str,
    token: str,
    cached: bool,
) -> bool:
    acquired = await video_ports()._acquire_provider_slot(
        redis,
        provider.name,
        video_ports()._provider_submit_concurrency(provider, generation),
        generation.id,
        exclusive=video_ports()._provider_submit_is_exclusive(provider, generation),
    )
    if acquired:
        return True
    try:
        await video_ports()._enqueue_submit(
            redis,
            generation.id,
            defer_s=video_ports()._POLL_INTERVAL_S,
        )
    except Exception:
        label = "cached submit" if cached else "submit"
        video_ports().logger.warning(
            "video %s re-enqueue failed task=%s",
            label,
            generation.id,
            exc_info=True,
        )
    await video_ports()._release_lease(redis, task_id, token)
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
        async with video_ports().SessionLocal() as session:
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
                generation.next_poll_at = video_ports()._now()
                generation.submit_started_at = None
                diagnostics = video_ports()._generation_diagnostics(generation)
                video_ports()._append_bounded_history(
                    diagnostics,
                    "submit_recovery_history",
                    {
                        "at": video_ports()._now().isoformat(),
                        "reason": "lease_lost_before_upstream",
                        "submission_epoch": submission_epoch,
                    },
                )
                generation.diagnostics = diagnostics
                _record_submit_delivery(
                    generation,
                    state="proven_absent",
                    reason="lease_lost_before_upstream",
                )
                should_requeue = True
            await session.commit()
    except Exception:
        video_ports().logger.error(
            "video pre-submit lease recovery failed task=%s epoch=%s",
            task_id,
            submission_epoch,
            exc_info=True,
        )
    if provider_name:
        await video_ports()._release_provider_slot(redis, provider_name, task_id)
    if should_requeue:
        try:
            await video_ports()._enqueue_submit(redis, task_id, defer_s=0)
        except Exception:
            video_ports().logger.warning(
                "video pre-submit lease recovery enqueue failed task=%s",
                task_id,
                exc_info=True,
            )


async def _relock_pre_submit_dispatch(
    session: Any,
    *,
    task_id: str,
    user_id: str,
    submission_epoch: int,
) -> VideoGeneration | None:
    return await relock_pre_submit_dispatch(
        session,
        task_id=task_id,
        user_id=user_id,
        submission_epoch=submission_epoch,
        record_submit_delivery=_record_submit_delivery,
    )


async def _restore_cached_submit(
    session: Any,
    prepared: _SubmitPreparation,
    *,
    task_id: str,
    attempt: _SubmitAttemptState,
) -> bool:
    generation = await _relock_cached_submit_row(
        session,
        task_id=task_id,
    )
    if generation is None or prepared.cached_submit is None:
        return False
    attempt.submission_epoch = int(
        getattr(generation, "submission_epoch", 0) or 0
    )
    attempt.result = video_ports()._restore_cached_provider_identity(
        generation,
        prepared.cached_submit,
    )
    await session.commit()
    return True


async def _submit_fresh_video(
    session: Any,
    redis: Any,
    prepared: _SubmitPreparation,
    *,
    task_id: str,
    token: str,
    lease_lost: asyncio.Event,
    attempt: _SubmitAttemptState,
) -> bool:
    generation = prepared.generation
    provider = await video_ports()._provider_for_generation(generation)
    if not await video_ports()._reserve_video_submit_slot(
        redis,
        generation,
        provider,
        task_id=task_id,
        token=token,
        cached=False,
    ):
        return False
    attempt.slot_provider_name = provider.name
    attempt.provider_supports_idempotency = bool(
        getattr(provider, "supports_idempotency", False)
    )
    upstream_model = provider.upstream_model_for(
        generation.model,
        generation.action,
    )
    if not upstream_model:
        raise RuntimeError("provider model mapping missing")
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video submit lease lost before state transition",
    )
    input_bytes, input_mime = await video_ports()._input_image_bytes(
        session,
        generation,
    )
    reference_media = await video_ports()._reference_media_bytes(generation)
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video submit lease lost while loading request media",
    )
    generation = await _relock_pre_submit_transition(
        session,
        task_id=task_id,
        user_id=generation.user_id,
    )
    if generation is None:
        await video_ports()._release_provider_slot(
            redis,
            provider.name,
            task_id,
        )
        return False
    generation.provider_name = provider.name
    generation.provider_kind = provider.kind
    video_ports()._persist_provider_snapshot(
        generation,
        provider,
        upstream_model=upstream_model,
    )
    generation.status = VideoGenerationStatus.SUBMITTING.value
    generation.progress_stage = VideoGenerationStage.SUBMITTING.value
    generation.progress_pct = max(generation.progress_pct, 5)
    submit_started_at = video_ports()._now()
    generation.started_at = generation.started_at or submit_started_at
    generation.attempt += 1
    generation.submission_epoch = (
        int(getattr(generation, "submission_epoch", 0) or 0) + 1
    )
    attempt.submission_epoch = generation.submission_epoch
    generation.submit_started_at = submit_started_at
    generation.provider_idempotency_key = (
        getattr(generation, "provider_idempotency_key", None)
        or f"video:{generation.id}"
    )
    generation.next_poll_at = submit_started_at + timedelta(
        seconds=video_ports()._SUBMIT_UNKNOWN_AFTER_S
    )
    await session.commit()

    fenced_generation = await _relock_pre_submit_dispatch(
        session,
        task_id=task_id,
        user_id=generation.user_id,
        submission_epoch=attempt.submission_epoch,
    )
    if fenced_generation is None:
        await video_ports()._release_provider_slot(
            redis,
            provider.name,
            task_id,
        )
        return False
    generation = fenced_generation
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video submit lease lost before upstream call",
    )
    adapter = video_ports().adapter_for_provider(provider)
    # The submission epoch and cancellation fence are durable. Release the
    # row lock before the provider network call; the receipt path below
    # reconciles any cancellation race.
    await session.commit()
    attempt.upstream_invoked = True
    attempt.result = await adapter.submit(
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
            input_image_url=video_ports()._input_image_url(generation),
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
        # Ordering contract: await _store_submit_result before the post-submit
        # lease fence below.
        await video_ports()._store_submit_result(
            redis,
            task_id,
            attempt.result,
            provider_name=provider.name,
            provider_kind=provider.kind,
        )
    except Exception:
        video_ports().logger.warning(
            "video submit cache store failed task=%s",
            task_id,
            exc_info=True,
        )
    video_ports()._raise_if_video_lease_lost(
        lease_lost,
        "video submit lease lost after upstream call",
    )
    return True


async def run_video_generation_with_lease(
    ctx: dict[str, Any],
    task_id: str,
    *,
    token: str,
    lease_lost: asyncio.Event,
) -> None:
    redis = ctx["redis"]
    attempt = _SubmitAttemptState()
    try:
        async with video_ports().SessionLocal() as session:
            prepared = await _prepare_submit_row(
                session,
                redis,
                task_id=task_id,
                token=token,
            )
            if prepared is None:
                return
            if prepared.cached_submit is not None:
                ready = await _restore_cached_submit(
                    session,
                    prepared,
                    task_id=task_id,
                    attempt=attempt,
                )
            else:
                ready = await _submit_fresh_video(
                    session,
                    redis,
                    prepared,
                    task_id=task_id,
                    token=token,
                    lease_lost=lease_lost,
                    attempt=attempt,
                )
            if not ready:
                return
    except Exception as exc:  # noqa: BLE001
        await video_ports()._handle_video_submit_exception(
            redis,
            task_id,
            exc,
            provider_name=attempt.slot_provider_name,
            submission_epoch=attempt.submission_epoch,
            upstream_invoked=attempt.upstream_invoked,
            provider_supports_idempotency=(
                attempt.provider_supports_idempotency
            ),
            lease_lost=lease_lost,
        )
        return

    if attempt.result is None:
        return
    persisted = await video_ports()._persist_video_submit_receipt(
        redis,
        task_id,
        attempt.result,
        submission_epoch=attempt.submission_epoch,
        lease_lost=lease_lost,
    )
    if not persisted:
        await video_ports()._enqueue_cached_submit_recovery(
            redis,
            task_id,
            defer_s=video_ports()._POLL_INTERVAL_S,
        )
        return
    try:
        await video_ports()._enqueue_poll(redis, task_id)
    except Exception:
        video_ports().logger.warning(
            "video poll enqueue failed task=%s",
            task_id,
            exc_info=True,
        )
    finally:
        await video_ports()._release_lease(redis, task_id, token)


async def handle_video_submit_exception(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None,
    submission_epoch: int | None,
    upstream_invoked: bool,
    provider_supports_idempotency: bool,
    lease_lost: asyncio.Event | None = None,
) -> None:
    if isinstance(exc, video_ports()._VideoLeaseLost) or (
        lease_lost is not None and lease_lost.is_set()
    ):
        # Fence against a stale worker: the lease was lost while the failure
        # happened, so this worker must not write SUBMIT_UNKNOWN/FAILED to the
        # row -- a successor may already be (or about to be) driving the task.
        video_ports().logger.warning(
            "video submit lease lost; stale worker will not mutate task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        if not upstream_invoked:
            await video_ports()._restore_pre_submit_after_lease_loss(
                redis,
                task_id,
                provider_name=provider_name,
                submission_epoch=submission_epoch,
            )
        return
    if (
        upstream_invoked
        and not provider_supports_idempotency
        and video_ports()._submit_outcome_unknown(exc)
    ):
        try:
            await video_ports()._mark_submit_unknown(
                task_id,
                exc,
                provider_name=provider_name,
                submission_epoch=submission_epoch,
            )
        except Exception:
            video_ports().logger.error(
                "video submit outcome unknown persistence failed task=%s epoch=%s",
                task_id,
                submission_epoch,
                exc_info=True,
            )
        finally:
            # The upstream call has returned with an ambiguous outcome; the
            # slot's purpose (serializing submissions) is done. Holding it
            # through SUBMIT_UNKNOWN would block exclusive providers until the
            # finalize window (_SUBMIT_UNKNOWN_FINALIZE_AFTER_S) elapses.
            if provider_name:
                await video_ports()._release_provider_slot(
                    redis,
                    provider_name,
                    task_id,
                )
        return
    await video_ports()._fail_before_submit(
        redis,
        task_id,
        exc,
        provider_name=provider_name,
        submission_epoch=submission_epoch,
        upstream_invoked=upstream_invoked,
        provider_supports_idempotency=provider_supports_idempotency,
    )


async def persist_video_submit_receipt(
    redis: Any,
    task_id: str,
    result: SubmitResult,
    *,
    submission_epoch: int | None,
    lease_lost: asyncio.Event,
) -> bool:
    return await _persist_video_submit_receipt_impl(
        redis,
        task_id,
        result,
        submission_epoch=submission_epoch,
        lease_lost=lease_lost,
        record_submit_delivery=_record_submit_delivery,
    )


async def mark_pre_submit_canceled(
    session: Any,
    generation: VideoGeneration,
) -> None:
    delivery_state = _submit_delivery_state(generation)
    _record_submit_delivery(
        generation,
        state=delivery_state,
        reason="pre_submit_cancel_observed",
    )
    generation.status = VideoGenerationStatus.CANCELED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "canceled"
    generation.error_message = (
        "cancelled before upstream submission"
        if delivery_state == "proven_absent"
        else "cancelled while upstream submission outcome was unknown"
    )
    generation.finished_at = video_ports()._now()
    resolution = await video_ports().resolve_video_billing(
        session,
        generation,
        poll_result=PollResult(
            status="cancelled",
            upstream_billable=False,
            raw={"reason": "pre_submit_cancel"},
        ),
        reason="pre_submit_cancel",
    )
    generation.billed_tokens = resolution.actual_tokens
    generation.billed_cost_micro = resolution.actual_micro
    diagnostics = video_ports()._generation_diagnostics(generation)
    diagnostics["pre_submit_billing"] = {
        "at": video_ports()._now().isoformat(),
        "decision": resolution.decision,
        "actual_tokens": resolution.actual_tokens,
        "actual_micro": resolution.actual_micro,
        "submit_delivery_state": delivery_state,
    }
    generation.diagnostics = diagnostics
    video_ports()._queue_video_event(session, generation, EV_VIDEO_CANCELED)


async def mark_pre_submit_expired(
    session: Any,
    generation: VideoGeneration,
    *,
    reason: str,
) -> None:
    delivery_state = _submit_delivery_state(generation)
    _record_submit_delivery(
        generation,
        state=delivery_state,
        reason="pre_submit_expiry_observed",
    )
    diagnostics = video_ports()._generation_diagnostics(generation)
    diagnostics["pre_submit_expired_at"] = video_ports()._now().isoformat()
    generation.status = VideoGenerationStatus.EXPIRED.value
    generation.progress_stage = VideoGenerationStage.FINISHED.value
    generation.progress_pct = 100
    generation.error_code = "deadline_expired"
    generation.error_message = "video task expired before upstream submission"
    generation.diagnostics = diagnostics
    generation.finished_at = video_ports()._now()
    resolution = await video_ports().resolve_video_billing(
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
    generation.billed_tokens = resolution.actual_tokens
    generation.billed_cost_micro = resolution.actual_micro
    diagnostics = video_ports()._generation_diagnostics(generation)
    diagnostics["pre_submit_billing"] = {
        "at": video_ports()._now().isoformat(),
        "decision": resolution.decision,
        "actual_tokens": resolution.actual_tokens,
        "actual_micro": resolution.actual_micro,
        "submit_delivery_state": delivery_state,
    }
    generation.diagnostics = diagnostics
    video_ports()._queue_video_event(session, generation, EV_VIDEO_FAILED)


def transition_submit_unknown(
    session: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
    reason: str,
    last_error: dict[str, Any] | None = None,
) -> None:
    _transition_submit_unknown_impl(
        session,
        generation,
        now=now,
        reason=reason,
        last_error=last_error,
        record_submit_delivery=_record_submit_delivery,
    )


async def fail_before_submit(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    provider_name: str | None = None,
    submission_epoch: int | None = None,
    upstream_invoked: bool = False,
    provider_supports_idempotency: bool = False,
) -> None:
    release_provider_name = provider_name
    release_provider_slot = False
    try:
        async with video_ports().SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if (
                generation is None
                or generation.status in video_ports()._NON_RESUBMIT_STATUSES
            ):
                return
            release_provider_slot = True
            release_provider_name = release_provider_name or generation.provider_name
            error_code = video_ports()._video_exception_code(
                exc,
                default="provider_unavailable",
            )
            if not upstream_invoked or submit_delivery_proven_absent(exc):
                delivery_state = "proven_absent"
            elif video_ports()._submit_outcome_unknown(exc):
                delivery_state = "unknown"
            else:
                delivery_state = "confirmed"
            _record_submit_delivery(
                generation,
                state=delivery_state,
                reason="submit_exception",
                provider_supports_idempotency=provider_supports_idempotency,
                error_code=error_code,
            )
            if await video_ports()._schedule_submit_retry(
                session, redis, generation, exc
            ):
                return
            error_message = video_ports()._video_exception_message(exc, phase="submit")
            video_ports().logger.warning(
                "video submit failed task=%s attempt=%s code=%s error=%s",
                task_id,
                video_ports()._generation_attempt(generation),
                error_code,
                error_message,
                exc_info=video_ports()._exception_log_info(exc),
            )
            diagnostics = video_ports()._generation_diagnostics(generation)
            diagnostics["last_submit_error"] = {
                "at": video_ports()._now().isoformat(),
                "attempt": video_ports()._generation_attempt(generation),
                "error_code": error_code,
                "message": error_message[:500],
                "retryable": video_ports()._is_retryable_video_exception(exc),
                "terminal": True,
            }
            generation.status = VideoGenerationStatus.FAILED.value
            generation.progress_stage = VideoGenerationStage.FINISHED.value
            generation.progress_pct = 100
            generation.error_code = error_code
            generation.error_message = error_message
            generation.diagnostics = diagnostics
            generation.finished_at = video_ports()._now()
            billable_hint = video_ports()._submit_failure_billable_hint(exc)
            await video_ports().resolve_video_billing(
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
            video_ports()._queue_video_event(session, generation, EV_VIDEO_FAILED)
            await session.commit()
            # Compatibility audit marker: await worker_flush_balance_cache(session)
            await video_ports().worker_flush_balance_cache(session)
    finally:
        if release_provider_slot and release_provider_name:
            # Compatibility audit marker:
            # _release_provider_slot(redis, release_provider_name, task_id)
            await video_ports()._release_provider_slot(
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
    if not video_ports()._is_retryable_video_exception(exc):
        return False
    attempt = video_ports()._generation_attempt(generation)
    if attempt >= video_ports()._MAX_SUBMIT_ATTEMPTS:
        return False
    now = video_ports()._now()
    remaining_s = int((generation.deadline_at - now).total_seconds())
    if remaining_s <= 1:
        return False
    delay_s = max(
        1,
        min(video_ports()._submit_retry_delay_s(attempt), remaining_s - 1),
    )
    error_code = video_ports()._video_exception_code(
        exc, default="provider_unavailable"
    )
    error_message = video_ports()._video_exception_message(exc, phase="submit")
    diagnostics = video_ports()._generation_diagnostics(generation)
    retry_item = {
        "at": now.isoformat(),
        "attempt": attempt,
        "error_code": error_code,
        "message": error_message[:500],
        "next_retry_delay_s": delay_s,
    }
    video_ports()._append_bounded_history(
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
    video_ports()._queue_video_event(
        session,
        generation,
        EV_VIDEO_PROGRESS,
        retry_transition=True,
        retry_after_s=delay_s,
        retry_attempt=attempt,
        retry_error_code=error_code,
    )
    await session.commit()
    video_ports().logger.info(
        "video submit retry scheduled task=%s attempt=%s delay_s=%s code=%s error=%s",
        generation.id,
        attempt,
        delay_s,
        error_code,
        error_message,
    )
    try:
        await video_ports()._enqueue_submit(redis, generation.id, defer_s=delay_s)
    except Exception:
        video_ports().logger.warning(
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
    "_relock_pre_submit_dispatch",
    "restore_cached_provider_identity",
    "restore_pre_submit_after_lease_loss",
    "resume_existing_provider_task",
    "run_video_generation",
    "run_video_generation_with_lease",
    "schedule_submit_retry",
    "transition_submit_unknown",
]
