"""Provider polling, retry, cancellation, and result routing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_VIDEO_PROGRESS,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import VideoGeneration

from ...video_artifacts import InvalidVideoArtifactError
from ...video_upstream import (
    PollResult,
    VideoProviderAdapter,
    VideoUpstreamError,
)
from ._facade import _g


async def handle_video_upstream_poll_error(
    redis: Any,
    task_id: str,
    exc: VideoUpstreamError,
    *,
    lease_lost: asyncio.Event,
) -> None:
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before upstream error handling",
    )
    if await _g._finish_cancelled_after_provider_poll_error(
        redis,
        task_id,
        lease_lost=lease_lost,
        exc=exc,
    ):
        return
    if await _g._schedule_poll_retry(
        redis,
        task_id,
        exc,
        lease_lost=lease_lost,
    ):
        return
    # Compatibility audit marker:
    # retryable_poll_error = _is_retryable_video_exception(exc)
    retryable_poll_error = _g._is_retryable_video_exception(exc)
    await _g._apply_poll_result(
        redis,
        task_id,
        PollResult(
            status="expired" if retryable_poll_error else "failed",
            failure_class=exc.error_code,
            upstream_billable=None,
            raw=exc.raw
            or {
                "error": _g._video_exception_message(exc, phase="poll"),
                "error_code": _g._video_exception_code(
                    exc,
                    default="upstream_unknown",
                ),
            },
        ),
        fallback_error_message=_g._video_exception_message(
            exc,
            phase="poll",
        ),
        lease_lost=lease_lost,
    )


async def _handle_non_upstream_poll_error(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    lease_lost: asyncio.Event,
) -> None:
    _g.logger.warning(
        "video poll failed task=%s err=%s",
        task_id,
        exc,
        exc_info=True,
    )
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before unexpected error handling",
    )
    if not _g._is_retryable_video_exception(exc):
        await _g._handle_unexpected_poll_exception(
            redis,
            task_id,
            exc,
            lease_lost=lease_lost,
        )
        return
    if await _g._schedule_poll_retry(
        redis,
        task_id,
        exc,
        lease_lost=lease_lost,
    ):
        return
    await _g._apply_poll_result(
        redis,
        task_id,
        PollResult(
            status="expired",
            failure_class=_g._video_exception_code(
                exc,
                default="upstream_unknown",
            ),
            upstream_billable=None,
            raw={
                "error": _g._video_exception_message(exc, phase="poll"),
                "error_code": _g._video_exception_code(
                    exc,
                    default="upstream_unknown",
                ),
            },
        ),
        fallback_error_message=_g._video_exception_message(
            exc,
            phase="poll",
        ),
        lease_lost=lease_lost,
    )


async def _handle_poll_execution_error(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    lease_lost: asyncio.Event,
) -> None:
    if isinstance(exc, _g._VideoLeaseLost):
        _g.logger.warning("video poll lease lost task=%s err=%s", task_id, exc)
        return
    try:
        if isinstance(exc, VideoUpstreamError):
            await _g._handle_video_upstream_poll_error(
                redis,
                task_id,
                lease_lost=lease_lost,
                exc=exc,
            )
            return
        await _handle_non_upstream_poll_error(
            redis,
            task_id,
            exc,
            lease_lost=lease_lost,
        )
    except _g._VideoLeaseLost as lease_exc:
        _g.logger.warning(
            "video poll lease lost during error handling task=%s err=%s",
            task_id,
            lease_exc,
        )


async def run_video_poll(ctx: dict[str, Any], task_id: str) -> None:
    redis = ctx["redis"]
    token = f"video-poll:{_g.new_uuid7()}"
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
    adapter: VideoProviderAdapter | None = None
    try:
        async with _g.SessionLocal() as session:
            generation = (
                await session.execute(
                    select(VideoGeneration)
                    .where(VideoGeneration.id == task_id)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if generation is None or generation.status in _g._TERMINAL_STATUSES:
                return
            if not generation.provider_task_id:
                await _g._enqueue_submit(
                    redis,
                    task_id,
                    defer_s=_g._POLL_INTERVAL_S,
                )
                return
            _g._raise_if_video_lease_lost(
                lease_lost,
                "video poll lease lost before provider resolution",
            )
            provider = await _g._provider_for_generation(generation)
            adapter = _g.adapter_for_provider(provider)
            provider_task_id = generation.provider_task_id
            deadline_expired = generation.deadline_at <= _g._now()
            should_commit_poll_state = False
            if generation.cancel_requested_at is not None:
                await _g._try_provider_cancel(
                    adapter,
                    generation,
                    lease_lost=lease_lost,
                )
                should_commit_poll_state = True
            if deadline_expired:
                diagnostics = _g._generation_diagnostics(generation)
                diagnostics.setdefault(
                    "deadline_expired_at",
                    _g._now().isoformat(),
                )
                diagnostics["deadline_expired_polling_continues"] = True
                generation.diagnostics = diagnostics
                should_commit_poll_state = True
            if should_commit_poll_state:
                _g._raise_if_video_lease_lost(
                    lease_lost,
                    "video poll lease lost before state commit",
                )
                await session.commit()

        poll = await adapter.poll(provider_task_id)
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost during provider poll",
        )
        await _g._apply_poll_result(
            redis,
            task_id,
            poll,
            adapter=adapter,
            lease_lost=lease_lost,
        )
    except Exception as exc:  # noqa: BLE001
        await _handle_poll_execution_error(
            redis,
            task_id,
            exc,
            lease_lost=lease_lost,
        )
    finally:
        stop_renewer.set()
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)
        await _g._release_lease(redis, task_id, token)


def _poll_retry_delay(
    generation: VideoGeneration,
    *,
    now: datetime,
    local_window_exhausted: bool,
) -> int:
    if local_window_exhausted:
        return _g._EXTENDED_POLL_INTERVAL_S
    remaining_s = int((generation.deadline_at - now).total_seconds())
    if remaining_s <= 1:
        return _g._POLL_RETRY_DELAY_S
    return max(1, min(_g._POLL_RETRY_DELAY_S, remaining_s - 1))


async def _publish_and_enqueue_poll_retry(
    redis: Any,
    generation: VideoGeneration,
    *,
    delay_s: int,
    error_code: str,
    lease_lost: asyncio.Event | None,
) -> None:
    try:
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before retry event",
        )
        await _g._publish(
            redis,
            generation,
            EV_VIDEO_PROGRESS,
            retry_after_s=delay_s,
            retry_error_code=error_code,
        )
    except _g._VideoLeaseLost:
        raise
    except Exception:
        _g.logger.warning(
            "video poll retry publish failed task=%s",
            generation.id,
            exc_info=True,
        )
    try:
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before retry enqueue",
        )
        await _g._enqueue_poll(redis, generation.id, defer_s=delay_s)
    except _g._VideoLeaseLost:
        raise
    except Exception:
        _g.logger.warning(
            "video poll retry enqueue failed task=%s",
            generation.id,
            exc_info=True,
        )


async def schedule_poll_retry(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    lease_lost: asyncio.Event | None = None,
) -> bool:
    if not _g._is_retryable_video_exception(exc):
        return False
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before retry scheduling",
    )
    async with _g.SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _g._TERMINAL_STATUSES:
            return True
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before retry mutation",
        )
        now = _g._now()
        error_code = _g._video_exception_code(exc, default="upstream_unknown")
        if _g._provider_tracking_window_exhausted(generation, now):
            return False
        local_window_exhausted = _g._poll_window_exhausted(generation, now)
        delay_s = _poll_retry_delay(
            generation,
            now=now,
            local_window_exhausted=local_window_exhausted,
        )
        error_message = _g._video_exception_message(exc, phase="poll")
        diagnostics = dict(generation.diagnostics or {})
        diagnostics.pop("unexpected_poll_attempts", None)
        diagnostics.pop("unexpected_poll_fingerprint", None)
        if generation.deadline_at <= now:
            diagnostics.setdefault("deadline_expired_at", now.isoformat())
            diagnostics["deadline_expired_poll_retry_continues"] = True
        if local_window_exhausted:
            diagnostics.setdefault("poll_window_exhausted_at", now.isoformat())
            diagnostics["extended_polling_continues"] = True
            diagnostics["extended_poll_delay_s"] = _g._EXTENDED_POLL_INTERVAL_S
            diagnostics["max_poll_count"] = _g._MAX_POLL_COUNT
            diagnostics["max_poll_duration_s"] = _g._MAX_POLL_DURATION_S
            diagnostics["max_provider_poll_duration_s"] = (
                _g._MAX_PROVIDER_POLL_DURATION_S
            )
            elapsed_s = _g._poll_elapsed_s(generation, now)
            if elapsed_s is not None:
                diagnostics["poll_elapsed_s"] = elapsed_s
        retry_item = {
            "at": now.isoformat(),
            "poll_count": generation.poll_count,
            "error_code": error_code,
            "message": error_message[:500],
            "next_retry_delay_s": delay_s,
        }
        _g._append_bounded_history(
            diagnostics,
            "poll_retry_history",
            retry_item,
        )
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
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before retry commit",
        )
        await session.commit()
        _g.logger.info(
            "video poll retry scheduled task=%s poll_count=%s delay_s=%s "
            "code=%s error=%s",
            generation.id,
            generation.poll_count,
            delay_s,
            error_code,
            error_message,
        )
        await _publish_and_enqueue_poll_retry(
            redis,
            generation,
            delay_s=delay_s,
            error_code=error_code,
            lease_lost=lease_lost,
        )
        return True


async def handle_unexpected_poll_exception(
    redis: Any,
    task_id: str,
    exc: Exception,
    *,
    lease_lost: asyncio.Event | None = None,
) -> None:
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before unexpected error persistence",
    )
    async with _g.SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _g._TERMINAL_STATUSES:
            return
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error mutation",
        )
        now = _g._now()
        diagnostics = _g._generation_diagnostics(generation)
        try:
            previous_attempts = int(diagnostics.get("unexpected_poll_attempts") or 0)
        except (TypeError, ValueError):
            previous_attempts = 0
        error_code = _g._video_exception_code(
            exc,
            default="poll_internal_error",
        )
        error_message = _g._video_exception_message(exc, phase="poll")
        fingerprint = f"{type(exc).__module__}.{type(exc).__qualname__}:{error_code}"
        if diagnostics.get("unexpected_poll_fingerprint") != fingerprint:
            previous_attempts = 0
        tracking_exhausted = _g._provider_tracking_window_exhausted(
            generation,
            now,
        )
        attempt = previous_attempts + 1
        terminal = tracking_exhausted or attempt >= _g._MAX_UNEXPECTED_POLL_ATTEMPTS
        item = {
            "at": now.isoformat(),
            "attempt": attempt,
            "error_code": error_code,
            "message": error_message[:500],
            "terminal": terminal,
        }
        _g._append_bounded_history(
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
        diagnostics["max_unexpected_poll_attempts"] = _g._MAX_UNEXPECTED_POLL_ATTEMPTS
        generation.diagnostics = diagnostics
        if terminal:
            await _g._finish_terminal_failure(
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
                        "max_unexpected_poll_attempts": (
                            _g._MAX_UNEXPECTED_POLL_ATTEMPTS
                        ),
                        "provider_tracking_window_exhausted": tracking_exhausted,
                        "upstream_cost_ambiguous": True,
                    },
                ),
                fallback_error_message=error_message,
                lease_lost=lease_lost,
            )
            return

        delay_s = (
            _g._EXTENDED_POLL_INTERVAL_S
            if _g._poll_window_exhausted(generation, now)
            else _g._POLL_RETRY_DELAY_S
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
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error retry commit",
        )
        await session.commit()
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error retry event",
        )
        await _g._publish(
            redis,
            generation,
            EV_VIDEO_PROGRESS,
            retry_after_s=delay_s,
            retry_error_code=error_code,
            unexpected_retry_attempt=attempt,
        )
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before unexpected error retry enqueue",
        )
        await _g._enqueue_poll(redis, generation.id, defer_s=delay_s)


async def finish_cancelled_after_provider_poll_error(
    redis: Any,
    task_id: str,
    exc: VideoUpstreamError,
    *,
    lease_lost: asyncio.Event | None = None,
) -> bool:
    if _g._video_exception_code(exc, default="upstream_unknown") != (
        "upstream_not_ready"
    ):
        return False
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before cancel reconciliation",
    )
    async with _g.SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _g._TERMINAL_STATUSES:
            return True
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before cancel terminal mutation",
        )
        diagnostics = _g._generation_diagnostics(generation)
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
                        "error": _g._video_exception_message(exc, phase="poll"),
                        "error_code": "upstream_not_ready",
                    }
                ),
                "upstream_cost_ambiguous": True,
            },
        )
        kwargs = {
            "fallback_error_message": "video task cancelled by user",
        }
        if lease_lost is not None:
            kwargs["lease_lost"] = lease_lost
        await _g._finish_terminal_failure(
            session,
            redis,
            generation,
            poll,
            **kwargs,
        )
        return True


async def try_provider_cancel(
    adapter: Any,
    generation: VideoGeneration,
    *,
    lease_lost: asyncio.Event | None = None,
) -> None:
    diagnostics = _g._generation_diagnostics(generation)
    if diagnostics.get("cancel_sent_at") or diagnostics.get("cancel_unsupported_at"):
        return
    try:
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before provider cancel",
        )
        result = await adapter.cancel(generation.provider_task_id)
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost during provider cancel",
        )
        attempted_at = _g._now().isoformat()
        diagnostics["cancel_attempted_at"] = attempted_at
        diagnostics["cancel_result"] = result.raw if result else None
        if result is None:
            diagnostics["cancel_unsupported_at"] = attempted_at
        elif result.accepted:
            diagnostics["cancel_sent_at"] = attempted_at
        else:
            diagnostics["cancel_rejected_at"] = attempted_at
    except _g._VideoLeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        diagnostics["cancel_error_at"] = _g._now().isoformat()
        diagnostics["cancel_error"] = str(exc)[:500]
    generation.diagnostics = diagnostics


def _missing_result_poll(
    generation: VideoGeneration,
    poll: PollResult,
    *,
    now: datetime,
) -> tuple[PollResult, int, bool]:
    diagnostics = _g._generation_diagnostics(generation)
    attempts = max(0, int(diagnostics.get("missing_result_url_attempts") or 0)) + 1
    should_retry = (
        attempts <= _g._MAX_MISSING_RESULT_URL_POLLS
        and not _g._provider_tracking_window_exhausted(generation, now)
    )
    if should_retry:
        diagnostics["missing_result_url_attempts"] = attempts
        diagnostics["missing_result_url_retrying"] = True
        generation.diagnostics = diagnostics
        return (
            PollResult(
                status="running",
                progress=max(95, int(poll.progress or 0)),
                usage_total_tokens=poll.usage_total_tokens,
                upstream_billable=poll.upstream_billable,
                raw={
                    **(poll.raw or {}),
                    "warning": "succeeded_without_video_url",
                    "missing_result_url_attempts": attempts,
                },
            ),
            attempts,
            True,
        )
    return (
        PollResult(
            status="failed",
            failure_class="fetch_failed",
            usage_total_tokens=poll.usage_total_tokens,
            upstream_billable=poll.upstream_billable,
            raw={
                **(poll.raw or {}),
                "error": "missing video_url",
                "missing_result_url_attempts": attempts,
            },
        ),
        attempts,
        False,
    )


async def apply_poll_result(
    redis: Any,
    task_id: str,
    poll: PollResult,
    *,
    fallback_error_message: str | None = None,
    adapter: VideoProviderAdapter | None = None,
    lease_lost: asyncio.Event | None = None,
) -> None:
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before result persistence",
    )
    async with _g.SessionLocal() as session:
        generation = (
            await session.execute(
                select(VideoGeneration)
                .where(VideoGeneration.id == task_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if generation is None or generation.status in _g._TERMINAL_STATUSES:
            return
        _g._raise_if_video_lease_lost(
            lease_lost,
            "video poll lease lost before result mutation",
        )

        if poll.status in {"queued", "running"}:
            now = _g._now()
            if not _g._provider_tracking_window_exhausted(generation, now):
                # Compatibility audit marker: await _continue_running_poll(
                await _g._continue_running_poll(
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
                    "error": ("video task exceeded maximum provider tracking window"),
                    "poll_count": generation.poll_count,
                    "max_poll_count": _g._MAX_POLL_COUNT,
                    "max_poll_duration_s": _g._MAX_POLL_DURATION_S,
                    "max_provider_poll_duration_s": (_g._MAX_PROVIDER_POLL_DURATION_S),
                    "poll_elapsed_s": _g._poll_elapsed_s(generation, now),
                },
            )

        if poll.status == "succeeded":
            if not poll.video_url:
                now = _g._now()
                poll, _attempts, should_retry = _missing_result_poll(
                    generation,
                    poll,
                    now=now,
                )
                if should_retry:
                    # Compatibility audit marker: await _continue_running_poll(
                    await _g._continue_running_poll(
                        session,
                        redis,
                        generation,
                        poll,
                        now=now,
                        lease_lost=lease_lost,
                    )
                    return
            else:
                try:
                    await _g._finish_success(
                        session,
                        redis,
                        generation,
                        poll,
                        adapter=adapter,
                        lease_lost=lease_lost,
                    )
                except InvalidVideoArtifactError as exc:
                    await _g._finish_terminal_failure(
                        session,
                        redis,
                        generation,
                        _g._invalid_video_artifact_poll(poll, exc),
                        fallback_error_message=str(exc)[:1000],
                        lease_lost=lease_lost,
                        billing_reason=_g._INVALID_VIDEO_ARTIFACT_REASON,
                    )
                return

        await _g._finish_terminal_failure(
            session,
            redis,
            generation,
            poll,
            fallback_error_message=fallback_error_message,
            lease_lost=lease_lost,
        )


async def continue_running_poll(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    poll: PollResult,
    *,
    now: datetime,
    lease_lost: asyncio.Event | None = None,
) -> None:
    local_window_exhausted = _g._poll_window_exhausted(generation, now)
    delay_s = (
        _g._EXTENDED_POLL_INTERVAL_S if local_window_exhausted else _g._POLL_INTERVAL_S
    )
    diagnostics = _g._generation_diagnostics(generation)
    diagnostics.pop("unexpected_poll_attempts", None)
    diagnostics.pop("unexpected_poll_fingerprint", None)
    if local_window_exhausted:
        diagnostics.setdefault("poll_window_exhausted_at", now.isoformat())
        diagnostics["extended_polling_continues"] = True
        diagnostics["extended_poll_delay_s"] = delay_s
        diagnostics["max_poll_count"] = _g._MAX_POLL_COUNT
        diagnostics["max_poll_duration_s"] = _g._MAX_POLL_DURATION_S
        diagnostics["max_provider_poll_duration_s"] = _g._MAX_PROVIDER_POLL_DURATION_S
        elapsed_s = _g._poll_elapsed_s(generation, now)
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
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before running-state commit",
    )
    await session.commit()
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before progress event",
    )
    await _g._publish(
        redis,
        generation,
        EV_VIDEO_PROGRESS,
        extended_polling=local_window_exhausted,
        retry_after_s=delay_s,
    )
    _g._raise_if_video_lease_lost(
        lease_lost,
        "video poll lease lost before next poll enqueue",
    )
    await _g._enqueue_poll(redis, generation.id, defer_s=delay_s)


def cancelled_poll_during_finalization(poll: PollResult) -> PollResult:
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


def invalid_video_artifact_poll(
    poll: PollResult,
    exc: InvalidVideoArtifactError,
) -> PollResult:
    raw = {
        **(poll.raw or {}),
        "reason": _g._INVALID_VIDEO_ARTIFACT_REASON,
        "phase": "artifact_validation",
        "provider_status": poll.status,
        "error": str(exc)[:1000],
        "error_code": exc.error_code,
        "artifact_diagnostics": exc.diagnostics,
    }
    if poll.usage_total_tokens is None and poll.upstream_billable is None:
        raw["upstream_cost_ambiguous"] = True
    return PollResult(
        status="failed",
        failure_class=exc.error_code,
        usage_total_tokens=poll.usage_total_tokens,
        upstream_billable=poll.upstream_billable,
        raw=raw,
    )


__all__ = [
    "apply_poll_result",
    "cancelled_poll_during_finalization",
    "continue_running_poll",
    "finish_cancelled_after_provider_poll_error",
    "handle_unexpected_poll_exception",
    "handle_video_upstream_poll_error",
    "invalid_video_artifact_poll",
    "run_video_poll",
    "schedule_poll_retry",
    "try_provider_cancel",
]
