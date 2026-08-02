"""Cancellation fence applied after durable submit-state persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from lumen_core.constants import VideoGenerationStatus
from lumen_core.model_entities import User, VideoGeneration

from .runtime import video_ports


async def _lock_submit_user(
    session: Any,
    *,
    user_id: str,
) -> User | None:
    """Lock the account before the task row, matching account-deletion order."""
    return (
        await session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
    ).scalar_one_or_none()


async def _cancel_inactive_user_pre_submit(
    session: Any,
    generation: VideoGeneration,
    *,
    reason: str,
    record_submit_delivery: Callable[..., None],
) -> None:
    now = video_ports()._now()
    generation.cancel_requested_at = (
        getattr(generation, "cancel_requested_at", None) or now
    )
    diagnostics = video_ports()._generation_diagnostics(generation)
    diagnostics["pre_submit_cancellation_reason"] = reason
    generation.diagnostics = diagnostics
    record_submit_delivery(
        generation,
        state="proven_absent",
        reason=reason,
    )
    await video_ports()._mark_pre_submit_canceled(session, generation)
    await session.commit()
    await video_ports().worker_flush_balance_cache(session)


async def relock_pre_submit_dispatch(
    session: Any,
    *,
    task_id: str,
    user_id: str,
    submission_epoch: int,
    record_submit_delivery: Callable[..., None],
) -> VideoGeneration | None:
    """Fence upstream dispatch against cancellation and account deletion."""
    user = await _lock_submit_user(session, user_id=user_id)
    generation = (
        await session.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.id == task_id,
                VideoGeneration.submission_epoch == submission_epoch,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if generation is None:
        video_ports().logger.warning(
            "video submit dispatch fenced out task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        return None
    if generation.user_id != user_id:
        video_ports().logger.error(
            "video submit dispatch user mismatch task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        return None
    if user is None or getattr(user, "deleted_at", None) is not None:
        if (
            generation.status == VideoGenerationStatus.SUBMITTING.value
            and not generation.provider_task_id
        ):
            await _cancel_inactive_user_pre_submit(
                session,
                generation,
                reason="inactive_user_fence_before_upstream",
                record_submit_delivery=record_submit_delivery,
            )
        else:
            video_ports().logger.info(
                "video submit dispatch blocked by inactive user task=%s epoch=%s "
                "status=%s",
                task_id,
                submission_epoch,
                generation.status,
            )
        return None
    if generation.cancel_requested_at is not None:
        if (
            generation.status == VideoGenerationStatus.SUBMITTING.value
            and not generation.provider_task_id
        ):
            record_submit_delivery(
                generation,
                state="proven_absent",
                reason="cancellation_fence_before_upstream",
            )
            await video_ports()._mark_pre_submit_canceled(session, generation)
            await session.commit()
            await video_ports().worker_flush_balance_cache(session)
        else:
            video_ports().logger.info(
                "video submit dispatch canceled task=%s epoch=%s status=%s",
                task_id,
                submission_epoch,
                generation.status,
            )
        return None
    if (
        generation.status != VideoGenerationStatus.SUBMITTING.value
        or generation.provider_task_id
    ):
        video_ports().logger.warning(
            "video submit dispatch state fenced out task=%s epoch=%s status=%s",
            task_id,
            submission_epoch,
            generation.status,
        )
        return None
    return generation
