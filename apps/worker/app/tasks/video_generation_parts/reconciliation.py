"""Periodic reconciliation for interrupted video generation tasks."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from lumen_core.constants import (
    EV_VIDEO_FAILED,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.models import VideoGeneration

from ...video_upstream import PollResult
from ._facade import _g


async def finalize_submit_unknown(
    session: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
) -> None:
    diagnostics = _g._generation_diagnostics(generation)
    diagnostics["submit_unknown_finalized_at"] = now.isoformat()
    resolution = await _g.resolve_video_billing(
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
    _g._queue_video_event(session, generation, EV_VIDEO_FAILED)


async def reconcile_submit_unknown(
    session: Any,
    redis: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
) -> tuple[bool, bool, str | None]:
    try:
        cached_submit = await _g._load_submit_result(redis, generation.id)
    except Exception:
        _g.logger.warning(
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
    await _g._finalize_submit_unknown(session, generation, now=now)
    return True, False, provider_name


async def _reconcile_submitting_row(
    session: Any,
    redis: Any,
    row: VideoGeneration,
    *,
    now: datetime,
    submit_unknown_cutoff: datetime,
) -> bool:
    if await _g._lease_active(redis, row.id):
        return False
    if await _g._enqueue_cached_submit_recovery(redis, row.id, defer_s=0):
        row.next_poll_at = now + timedelta(seconds=_g._POLL_INTERVAL_S)
        return True
    submit_started_at = getattr(row, "submit_started_at", None) or getattr(
        row,
        "updated_at",
        None,
    )
    if submit_started_at is not None and submit_started_at > submit_unknown_cutoff:
        return False
    _g._transition_submit_unknown(
        session,
        row,
        now=now,
        reason="stale_submitting_without_lease_or_receipt",
    )
    return True


async def _reconcile_pre_submit_row(
    session: Any,
    redis: Any,
    row: VideoGeneration,
    *,
    now: datetime,
    submit_unknown_cutoff: datetime,
) -> bool:
    if row.status == VideoGenerationStatus.SUBMITTING.value:
        return await _reconcile_submitting_row(
            session,
            redis,
            row,
            now=now,
            submit_unknown_cutoff=submit_unknown_cutoff,
        )
    if row.deadline_at <= now:
        await _g._mark_pre_submit_expired(
            session,
            row,
            reason="reconcile_deadline_expired_before_submit",
        )
        return True
    row.status = VideoGenerationStatus.QUEUED.value
    row.progress_stage = VideoGenerationStage.QUEUED.value
    await _g._enqueue_submit(redis, row.id, defer_s=_g._POLL_INTERVAL_S)
    return True


async def _video_rows_for_reconciliation(
    session: Any,
    *,
    now: datetime,
) -> list[VideoGeneration]:
    cutoff = now - timedelta(seconds=_g._RECON_STALE_AFTER_S)
    return (
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
                        VideoGeneration.next_poll_at <= _g._now(),
                        VideoGeneration.updated_at <= cutoff,
                    ),
                )
                .order_by(VideoGeneration.created_at)
                .limit(_g._RECON_LIMIT)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )


async def reconcile_video_tasks(ctx: dict[str, Any]) -> int:
    """Repair interrupted video tasks without changing provider identity.

    The pre-submit path intentionally retains these compatibility guarantees:
    ``await _lease_active(redis, row.id)``,
    ``_enqueue_cached_submit_recovery``, ``_transition_submit_unknown``, and
    ``_mark_pre_submit_expired`` with
    ``reconcile_deadline_expired_before_submit``. Rows in
    ``VideoGenerationStatus.SUBMIT_UNKNOWN.value`` are delegated to
    ``_reconcile_submit_unknown``.
    """

    redis = ctx["redis"]
    now = _g._now()
    submit_unknown_cutoff = now - timedelta(seconds=_g._SUBMIT_UNKNOWN_AFTER_S)
    touched = 0
    release_slots: list[tuple[str, str]] = []
    cached_recoveries: list[str] = []
    async with _g.SessionLocal() as session:
        rows = await _video_rows_for_reconciliation(session, now=now)
        for row in rows:
            if row.status == VideoGenerationStatus.SUBMIT_UNKNOWN.value:
                (
                    changed,
                    recover_cached,
                    release_provider,
                ) = await _g._reconcile_submit_unknown(
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
            if row.provider_task_id:
                await _g._enqueue_poll(redis, row.id, defer_s=0)
                touched += 1
                continue
            touched += int(
                await _reconcile_pre_submit_row(
                    session,
                    redis,
                    row,
                    now=now,
                    submit_unknown_cutoff=submit_unknown_cutoff,
                )
            )
        await session.commit()
        await _g.worker_flush_balance_cache(session)
    for task_id in cached_recoveries:
        try:
            await _g._enqueue_submit(redis, task_id, defer_s=0)
        except Exception:
            _g.logger.warning(
                "video cached submit recovery enqueue failed task=%s",
                task_id,
                exc_info=True,
            )
    for provider_name, task_id in release_slots:
        await _g._release_provider_slot(redis, provider_name, task_id)
    return touched


__all__ = [
    "finalize_submit_unknown",
    "reconcile_submit_unknown",
    "reconcile_video_tasks",
]
