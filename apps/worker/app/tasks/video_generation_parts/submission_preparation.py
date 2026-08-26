"""Short-transaction preparation for video provider submission."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from lumen_core.constants import VideoGenerationStatus
from lumen_core.model_entities import VideoGeneration

from ...video_submit_cache import CachedSubmitResult
from .runtime import video_ports
from .submit_state import SubmitPreparation


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
    should_handle = (
        generation.status == VideoGenerationStatus.SUBMITTING.value
        or generation.deadline_at <= video_ports().operations._now()
        or (
            generation.cancel_requested_at is not None
            and generation.status == VideoGenerationStatus.QUEUED.value
            and not generation.provider_task_id
        )
    )
    if not should_handle:
        return False
    generation = (
        await session.execute(
            select(VideoGeneration)
            .where(VideoGeneration.id == generation.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if generation is None:
        await session.commit()
        await video_ports().lease_queue._release_lease(redis, task_id, token)
        return True
    if generation.status in video_ports().policy._NON_RESUBMIT_STATUSES:
        await session.commit()
        await video_ports().lease_queue._release_lease(redis, task_id, token)
        return True
    if generation.provider_task_id:
        await session.commit()
        return await video_ports().operations._resume_existing_provider_task(
            redis,
            generation,
            task_id=task_id,
            token=token,
        )
    release_provider_name: str | None = None
    flush_balance_cache = False
    if generation.status == VideoGenerationStatus.SUBMITTING.value:
        now = video_ports().operations._now()
        submit_started_at = getattr(
            generation,
            "submit_started_at",
            None,
        ) or getattr(generation, "updated_at", None)
        if submit_started_at is not None and submit_started_at > now - timedelta(
            seconds=video_ports().policy._SUBMIT_UNKNOWN_AFTER_S
        ):
            generation.next_poll_at = submit_started_at + timedelta(
                seconds=video_ports().policy._SUBMIT_UNKNOWN_AFTER_S
            )
        else:
            video_ports().operations._transition_submit_unknown(
                session,
                generation,
                now=now,
                reason="duplicate_worker_observed_stale_submitting",
            )
            if generation.provider_name:
                release_provider_name = generation.provider_name
    elif generation.deadline_at <= video_ports().operations._now():
        # Compatibility audit marker: await _mark_pre_submit_expired
        await video_ports().operations._mark_pre_submit_expired(
            session,
            generation,
            reason="deadline_expired_before_submit",
        )
        flush_balance_cache = True
    elif (
        generation.cancel_requested_at is not None
        and generation.status == VideoGenerationStatus.QUEUED.value
        and not generation.provider_task_id
    ):
        # Compatibility audit marker: await _mark_pre_submit_canceled
        await video_ports().operations._mark_pre_submit_canceled(session, generation)
        flush_balance_cache = True
    else:
        await session.commit()
        return False
    await session.commit()
    if flush_balance_cache:
        # Compatibility audit marker: await worker_flush_balance_cache(session)
        await video_ports().billing_events.worker_flush_balance_cache(session)
    if release_provider_name:
        await video_ports().lease_queue._release_provider_slot(
            redis,
            release_provider_name,
            generation.id,
        )
    await video_ports().lease_queue._release_lease(redis, task_id, token)
    return True


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
        await video_ports().lease_queue._enqueue_poll(redis, generation.id, defer_s=0)
    except Exception:
        video_ports().operations.logger.warning(
            "video poll enqueue failed task=%s",
            generation.id,
            exc_info=True,
        )
    await video_ports().lease_queue._release_lease(redis, task_id, token)
    return True


async def prepare_submit_row(
    session: Any,
    redis: Any,
    *,
    task_id: str,
    token: str,
) -> SubmitPreparation | None:
    generation = (
        await session.execute(
            select(VideoGeneration)
            .where(VideoGeneration.id == task_id)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    await session.commit()
    if (
        generation is None
        or generation.status in video_ports().policy._NON_RESUBMIT_STATUSES
    ):
        await video_ports().lease_queue._release_lease(redis, task_id, token)
        return None
    if await video_ports().operations._resume_existing_provider_task(
        redis,
        generation,
        task_id=task_id,
        token=token,
    ):
        return None
    cached_submit = await video_ports().provider._load_submit_result(
        redis, generation.id
    )
    if await video_ports().operations._handle_existing_pre_submit_state(
        session,
        redis,
        generation,
        cached_submit=cached_submit,
        task_id=task_id,
        token=token,
    ):
        return None
    return SubmitPreparation(
        generation=generation,
        cached_submit=cached_submit,
    )


async def relock_cached_submit_row(
    session: Any,
    *,
    task_id: str,
) -> VideoGeneration | None:
    generation = (
        await session.execute(
            select(VideoGeneration)
            .where(VideoGeneration.id == task_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        generation is None
        or generation.status in video_ports().policy._TERMINAL_STATUSES
    ):
        await session.commit()
        return None
    return generation


async def relock_pre_submit_transition(
    session: Any,
    *,
    task_id: str,
    user_id: str,
) -> VideoGeneration | None:
    generation = (
        await session.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.id == task_id,
                VideoGeneration.user_id == user_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (
        generation is None
        or generation.status != VideoGenerationStatus.QUEUED.value
        or generation.provider_task_id
    ):
        await session.commit()
        return None
    if generation.deadline_at <= video_ports().operations._now():
        await video_ports().operations._mark_pre_submit_expired(
            session,
            generation,
            reason="deadline_expired_before_submit_transition",
        )
        await session.commit()
        await video_ports().billing_events.worker_flush_balance_cache(session)
        return None
    if generation.cancel_requested_at is not None:
        await video_ports().operations._mark_pre_submit_canceled(session, generation)
        await session.commit()
        await video_ports().billing_events.worker_flush_balance_cache(session)
        return None
    return generation


__all__ = [
    "handle_existing_pre_submit_state",
    "prepare_submit_row",
    "relock_cached_submit_row",
    "relock_pre_submit_transition",
    "resume_existing_provider_task",
]
