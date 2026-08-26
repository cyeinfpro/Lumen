"""Durable evidence for video submit delivery and receipt persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from lumen_core.constants import (
    EV_VIDEO_PROGRESS,
    EV_VIDEO_SUBMITTED,
    VideoGenerationStage,
    VideoGenerationStatus,
)
from lumen_core.model_entities import VideoGeneration

from ...video_upstream_service import SubmitResult
from .runtime import video_ports


_SUBMIT_DELIVERY_STATES = frozenset({"proven_absent", "unknown", "confirmed"})
_SUBMIT_DELIVERY_PRECEDENCE = ("proven_absent", "unknown", "confirmed")


def _persisted_submit_delivery_state(
    generation: VideoGeneration,
) -> str | None:
    if getattr(generation, "provider_task_id", None):
        return "confirmed"
    diagnostics = video_ports().operations._generation_diagnostics(generation)
    states: list[str] = []
    aggregate = diagnostics.get("submit_delivery_state")
    if aggregate in _SUBMIT_DELIVERY_STATES:
        states.append(str(aggregate))
    history = diagnostics.get("submit_delivery_history")
    if isinstance(history, list):
        for item in history:
            state = item.get("state") if isinstance(item, dict) else None
            if state in _SUBMIT_DELIVERY_STATES:
                states.append(str(state))
    if isinstance(diagnostics.get("submit_receipt"), dict):
        states.append("confirmed")
    if states:
        return max(states, key=_SUBMIT_DELIVERY_PRECEDENCE.index)
    return None


def _submit_delivery_state(generation: VideoGeneration) -> str:
    persisted = _persisted_submit_delivery_state(generation)
    if persisted is not None:
        return persisted
    if (
        int(getattr(generation, "attempt", 0) or 0) <= 0
        and int(getattr(generation, "submission_epoch", 0) or 0) <= 0
        and getattr(generation, "submit_started_at", None) is None
    ):
        return "proven_absent"
    return "unknown"


def _record_submit_delivery(
    generation: VideoGeneration,
    *,
    state: str,
    reason: str,
    provider_supports_idempotency: bool | None = None,
    error_code: str | None = None,
) -> None:
    if state not in _SUBMIT_DELIVERY_STATES:
        raise ValueError(f"invalid submit delivery state: {state}")
    diagnostics = video_ports().operations._generation_diagnostics(generation)
    current = _persisted_submit_delivery_state(generation) or state
    aggregate = max(
        (current, state),
        key=_SUBMIT_DELIVERY_PRECEDENCE.index,
    )
    item: dict[str, Any] = {
        "at": video_ports().operations._now().isoformat(),
        "attempt": video_ports().operations._generation_attempt(generation),
        "submission_epoch": int(getattr(generation, "submission_epoch", 0) or 0),
        "state": state,
        "reason": reason,
        "provider_idempotency_key": getattr(
            generation,
            "provider_idempotency_key",
            None,
        ),
    }
    if provider_supports_idempotency is not None:
        item["provider_supports_idempotency"] = bool(provider_supports_idempotency)
    if error_code:
        item["error_code"] = error_code
    video_ports().operations._append_bounded_history(
        diagnostics,
        "submit_delivery_history",
        item,
    )
    diagnostics["submit_delivery_state"] = aggregate
    generation.diagnostics = diagnostics


async def persist_video_submit_receipt(
    redis: Any,
    task_id: str,
    result: SubmitResult,
    *,
    submission_epoch: int | None,
    lease_lost: asyncio.Event,
    record_submit_delivery: Callable[..., None],
) -> bool:
    try:
        video_ports().lease_queue._raise_if_video_lease_lost(
            lease_lost,
            "video submit lease lost before receipt persistence",
        )
        async with video_ports().store.SessionLocal() as session:
            filters = [VideoGeneration.id == task_id]
            if submission_epoch is not None:
                filters.append(VideoGeneration.submission_epoch == submission_epoch)
            generation = (
                await session.execute(
                    select(VideoGeneration).where(*filters).with_for_update()
                )
            ).scalar_one_or_none()
            if generation is None:
                video_ports().operations.logger.warning(
                    "video submit receipt fenced out task=%s epoch=%s",
                    task_id,
                    submission_epoch,
                )
                return False
            if generation.status in video_ports().policy._TERMINAL_STATUSES:
                return False
            if generation.cancel_requested_at is not None:
                video_ports().operations.logger.info(
                    "video submit receipt persists through cancellation task=%s epoch=%s",
                    task_id,
                    submission_epoch,
                )
            generation.provider_task_id = result.provider_task_id
            generation.upstream_response = result.raw
            generation.status = VideoGenerationStatus.SUBMITTED.value
            generation.progress_stage = VideoGenerationStage.RENDERING.value
            generation.progress_pct = max(generation.progress_pct, 10)
            generation.submitted_at = video_ports().operations._now()
            generation.next_poll_at = video_ports().operations._now() + timedelta(
                seconds=video_ports().policy._POLL_INTERVAL_S
            )
            diagnostics = video_ports().operations._generation_diagnostics(generation)
            diagnostics["submit_receipt"] = {
                "submission_epoch": submission_epoch,
                "provider_task_id": result.provider_task_id,
                "provider_idempotency_key": getattr(
                    generation,
                    "provider_idempotency_key",
                    None,
                ),
                "persisted_at": video_ports().operations._now().isoformat(),
            }
            generation.diagnostics = diagnostics
            record_submit_delivery(
                generation,
                state="confirmed",
                reason="provider_submit_receipt_persisted",
            )
            await session.commit()
            await video_ports().billing_events._publish_after_commit(
                redis, generation, EV_VIDEO_SUBMITTED
            )
            return True
    except video_ports().policy._VideoLeaseLost:
        video_ports().operations.logger.warning(
            "video submit receipt skipped after lease loss task=%s epoch=%s",
            task_id,
            submission_epoch,
        )
        return False
    except Exception:
        video_ports().operations.logger.warning(
            "video submit persist failed task=%s",
            task_id,
            exc_info=True,
        )
        return False


def transition_submit_unknown(
    session: Any,
    generation: VideoGeneration,
    *,
    now: datetime,
    reason: str,
    last_error: dict[str, Any] | None = None,
    record_submit_delivery: Callable[..., None],
) -> None:
    diagnostics = video_ports().operations._generation_diagnostics(generation)
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
    generation.diagnostics = diagnostics
    record_submit_delivery(
        generation,
        state="unknown",
        reason=reason,
        error_code=(
            str(last_error.get("error_code"))
            if isinstance(last_error, dict) and last_error.get("error_code")
            else None
        ),
    )
    generation.status = VideoGenerationStatus.SUBMIT_UNKNOWN.value
    generation.progress_stage = VideoGenerationStage.SUBMITTING.value
    generation.progress_pct = max(generation.progress_pct, 5)
    generation.error_code = "submit_unknown"
    generation.error_message = (
        "video submission outcome is unknown; automatic reconciliation pending"
    )
    generation.next_poll_at = now + timedelta(
        seconds=video_ports().policy._SUBMIT_UNKNOWN_FINALIZE_AFTER_S
    )
    video_ports().billing_events._queue_video_event(
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
    async with video_ports().store.SessionLocal() as session:
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
            or generation.status in video_ports().policy._TERMINAL_STATUSES
        ):
            return False
        now = video_ports().operations._now()
        error_code = video_ports().operations._video_exception_code(
            exc, default="upstream_unknown"
        )
        error_message = video_ports().operations._video_exception_message(
            exc, phase="submit"
        )
        generation.provider_name = generation.provider_name or provider_name
        video_ports().operations._transition_submit_unknown(
            session,
            generation,
            now=now,
            reason="ambiguous_non_idempotent_submit_error",
            last_error={
                "at": now.isoformat(),
                "attempt": video_ports().operations._generation_attempt(generation),
                "error_code": error_code,
                "message": error_message[:500],
                "retryable": False,
                "outcome_unknown": True,
            },
        )
        await session.commit()
        return True
