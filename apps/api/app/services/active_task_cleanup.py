"""Shared row transitions for canceling active generation tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any, Awaitable, Callable, Sequence

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    VideoGenerationStatus,
)
from lumen_core.model_entities import Completion, Generation, VideoGeneration

from .generation_queue import GenerationQueueReleaseToken


HoldRelease = Callable[..., Awaitable[bool]]
QueueStateCapture = Callable[..., Awaitable[GenerationQueueReleaseToken | None]]
QueueCleanupEntry = tuple[str, int, GenerationQueueReleaseToken]


@dataclass(slots=True)
class GenerationCancellation:
    queued_rows: list[Generation]
    queued_ids: list[str]
    queued_execution_epochs: dict[str, int]
    queued_queue_tokens: dict[str, GenerationQueueReleaseToken]
    deferred_ids: list[str]
    running_ids: list[str]
    holds_released: int

    def cleanup_fields(self) -> dict[str, Any]:
        return {
            "queued_generation_ids": self.queued_ids,
            "queued_generation_execution_epochs": self.queued_execution_epochs,
            "queued_generation_queue_tokens": self.queued_queue_tokens,
            "running_generation_ids": self.running_ids,
            "deferred_generation_ids": self.deferred_ids,
        }


@dataclass(slots=True)
class CompletionCancellation:
    queued_rows: list[Completion]
    deferred_ids: list[str]
    streaming_ids: list[str]
    holds_released: int

    def cleanup_fields(self) -> dict[str, Any]:
        return {
            "streaming_completion_ids": self.streaming_ids,
            "deferred_completion_ids": self.deferred_ids,
        }


@dataclass(slots=True)
class VideoGenerationCancellation:
    active_ids: list[str]

    def cleanup_fields(self) -> dict[str, Any]:
        return {"active_video_generation_ids": self.active_ids}


def has_releasable_queued_tasks(
    generation_rows: Sequence[Generation],
    completion_rows: Sequence[Completion],
    *,
    generation_requires_durable_settlement: Callable[[object], bool],
    completion_requires_durable_settlement: Callable[[object], bool],
) -> bool:
    releasable_generations = [
        generation
        for generation in generation_rows
        if generation.status == GenerationStatus.QUEUED.value
        and not generation_requires_durable_settlement(generation)
    ]
    releasable_completions = [
        completion
        for completion in completion_rows
        if completion.status == CompletionStatus.QUEUED.value
        and not completion_requires_durable_settlement(completion)
    ]
    return bool(releasable_generations or releasable_completions)


async def cancel_generation_rows(
    generation_rows: Sequence[Generation],
    *,
    canceled_at: datetime,
    cancel_message: str,
    queue_redis: Any,
    capture_queue_ownership: bool,
    logger: logging.Logger,
    snapshot_failure_message: str,
    requires_durable_settlement: Callable[[object], bool],
    execution_epoch_for: Callable[[object], int],
    capture_queue_state: QueueStateCapture,
    release_hold: HoldRelease | None = None,
    billing_ref_id: Callable[[Generation], str] | None = None,
) -> GenerationCancellation:
    queued_rows: list[Generation] = []
    queued_ids: list[str] = []
    queued_execution_epochs: dict[str, int] = {}
    queued_queue_tokens: dict[str, GenerationQueueReleaseToken] = {}
    deferred_ids: list[str] = []
    running_ids: list[str] = []
    holds_released = 0
    for generation in generation_rows:
        generation.cancel_requested_at = (
            getattr(generation, "cancel_requested_at", None) or canceled_at
        )
        if generation.status == GenerationStatus.QUEUED.value:
            if requires_durable_settlement(generation):
                deferred_ids.append(generation.id)
                continue
            queued_rows.append(generation)
            queued_ids.append(generation.id)
            execution_epoch = execution_epoch_for(generation)
            queued_execution_epochs[generation.id] = execution_epoch
            generation.status = GenerationStatus.CANCELED.value
            generation.progress_stage = "finalizing"
            generation.finished_at = canceled_at
            generation.error_code = "cancelled"
            generation.error_message = cancel_message
            if release_hold is not None and billing_ref_id is not None:
                holds_released += int(
                    await release_hold(
                        ref_type="generation",
                        ref_id=billing_ref_id(generation),
                    )
                )
        elif generation.status == GenerationStatus.RUNNING.value:
            running_ids.append(generation.id)
    return GenerationCancellation(
        queued_rows=queued_rows,
        queued_ids=queued_ids,
        queued_execution_epochs=queued_execution_epochs,
        queued_queue_tokens=queued_queue_tokens,
        deferred_ids=deferred_ids,
        running_ids=running_ids,
        holds_released=holds_released,
    )


async def cancel_completion_rows(
    completion_rows: Sequence[Completion],
    *,
    canceled_at: datetime,
    cancel_message: str,
    requires_durable_settlement: Callable[[object], bool],
    release_hold: HoldRelease | None = None,
    billing_ref_id: Callable[[Completion], str] | None = None,
) -> CompletionCancellation:
    queued_rows: list[Completion] = []
    deferred_ids: list[str] = []
    streaming_ids: list[str] = []
    holds_released = 0
    for completion in completion_rows:
        completion.cancel_requested_at = (
            getattr(completion, "cancel_requested_at", None) or canceled_at
        )
        if completion.status == CompletionStatus.QUEUED.value:
            if requires_durable_settlement(completion):
                deferred_ids.append(completion.id)
                continue
            queued_rows.append(completion)
            completion.status = CompletionStatus.CANCELED.value
            completion.progress_stage = "finalizing"
            completion.finished_at = canceled_at
            completion.error_code = "cancelled"
            completion.error_message = cancel_message
            if release_hold is not None and billing_ref_id is not None:
                holds_released += int(
                    await release_hold(
                        ref_type="completion",
                        ref_id=billing_ref_id(completion),
                    )
                )
        elif completion.status == CompletionStatus.STREAMING.value:
            streaming_ids.append(completion.id)
    return CompletionCancellation(
        queued_rows=queued_rows,
        deferred_ids=deferred_ids,
        streaming_ids=streaming_ids,
        holds_released=holds_released,
    )


def cancel_video_generation_rows(
    video_generation_rows: Sequence[VideoGeneration],
    *,
    canceled_at: datetime,
) -> VideoGenerationCancellation:
    """Persist cancellation intent; the video worker owns provider settlement."""
    active_ids: list[str] = []
    for generation in video_generation_rows:
        if generation.status in {
            VideoGenerationStatus.SUCCEEDED.value,
            VideoGenerationStatus.FAILED.value,
            VideoGenerationStatus.CANCELED.value,
            VideoGenerationStatus.EXPIRED.value,
        }:
            continue
        generation.cancel_requested_at = (
            getattr(generation, "cancel_requested_at", None) or canceled_at
        )
        active_ids.append(generation.id)
    return VideoGenerationCancellation(active_ids=active_ids)


async def _invalidate_balance_best_effort(
    *,
    user_id: str,
    invalidate_balance: Callable[[str], Awaitable[None]],
    logger: logging.Logger,
    failure_message: str,
) -> None:
    try:
        await invalidate_balance(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(failure_message, user_id, exc)


async def post_commit_fail_fast_cleanup(
    *,
    user_id: str,
    queued_entries: Sequence[QueueCleanupEntry],
    cancel_ids: Sequence[str],
    invalidate_balance_required: bool,
    get_redis: Callable[[], Any],
    release_queue_state: Callable[..., Awaitable[Any]],
    invalidate_balance: Callable[[str], Awaitable[None]],
    logger: logging.Logger,
    signal_failure_message: str,
    balance_failure_message: str,
) -> None:
    if not queued_entries and not cancel_ids:
        if invalidate_balance_required:
            await _invalidate_balance_best_effort(
                user_id=user_id,
                invalidate_balance=invalidate_balance,
                logger=logger,
                failure_message=balance_failure_message,
            )
        return
    try:
        redis = get_redis()
        for task_id, execution_epoch, ownership_token in queued_entries:
            await release_queue_state(
                redis,
                task_id,
                expected_execution_epoch=execution_epoch,
                ownership_token=ownership_token,
            )
        for task_id in cancel_ids:
            await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning(signal_failure_message, user_id, exc)
    if invalidate_balance_required:
        await _invalidate_balance_best_effort(
            user_id=user_id,
            invalidate_balance=invalidate_balance,
            logger=logger,
            failure_message=balance_failure_message,
        )


async def post_commit_best_effort_cleanup(
    redis: Any,
    *,
    user_id: str,
    queued_entries: Sequence[QueueCleanupEntry],
    cancel_ids: Sequence[str],
    invalidate_balance_required: bool,
    release_queue_state: Callable[..., Awaitable[Any]],
    invalidate_balance: Callable[[str], Awaitable[None]],
    logger: logging.Logger,
    queue_failure_message: str,
    cancel_failure_message: str,
    balance_failure_message: str,
) -> None:
    for task_id, execution_epoch, ownership_token in queued_entries:
        try:
            await release_queue_state(
                redis,
                task_id,
                expected_execution_epoch=execution_epoch,
                ownership_token=ownership_token,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(queue_failure_message, task_id, exc)
    for task_id in cancel_ids:
        try:
            await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
        except Exception as exc:  # noqa: BLE001
            logger.warning(cancel_failure_message, task_id, exc)
    if invalidate_balance_required:
        await _invalidate_balance_best_effort(
            user_id=user_id,
            invalidate_balance=invalidate_balance,
            logger=logger,
            failure_message=balance_failure_message,
        )


__all__ = [
    "CompletionCancellation",
    "GenerationCancellation",
    "VideoGenerationCancellation",
    "cancel_completion_rows",
    "cancel_generation_rows",
    "cancel_video_generation_rows",
    "has_releasable_queued_tasks",
    "post_commit_best_effort_cleanup",
    "post_commit_fail_fast_cleanup",
]
