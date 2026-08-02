"""Task and billing cleanup used by self-service and admin account deletion."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import CompletionStatus, GenerationStatus, VideoGenerationStatus
from lumen_core.memory_extraction_models import MemoryExtractionRun
from lumen_core.model_entities import (
    Completion,
    Generation,
    Video,
    VideoGeneration,
)

from ..billing_cache_state import invalidate_balance_cache
from ..redis_client import get_redis
from .active_task_cleanup import cancel_video_generation_rows
from .generation_queue import (
    GenerationQueueReleaseToken,
    capture_queued_generation_cleanup_entries,
    completion_cancel_requires_durable_settlement,
    current_execution_epoch,
    generation_cancel_requires_durable_settlement,
    release_generation_queue_state,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class _RowcountResult(Protocol):
    rowcount: int | None


def dml_rowcount(result: object) -> int | None:
    if not isinstance(result, _RowcountResult):
        raise TypeError("expected a DML result with rowcount")
    return result.rowcount


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return HTTPException(
        status_code=http, detail={"error": {"code": code, "message": msg}}
    )


async def _release_account_delete_task_hold(
    db: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> bool:
    try:
        tx = await billing_core.release(
            db,
            user_id,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=f"account_delete:{ref_type}:{ref_id}",
            meta={"reason": "account deleted"},
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _account_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


async def cancel_account_active_tasks(
    db: AsyncSession,
    *,
    user_id: str,
    canceled_at: datetime,
    account_mode: str = "wallet",
    queue_redis: Any | None = None,
) -> dict[str, object]:
    generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.status.in_(
                        [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    completions = list(
        (
            await db.execute(
                select(Completion)
                .where(
                    Completion.user_id == user_id,
                    Completion.status.in_(
                        [
                            CompletionStatus.QUEUED.value,
                            CompletionStatus.STREAMING.value,
                        ]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    video_generations = list(
        (
            await db.execute(
                select(VideoGeneration)
                .where(
                    VideoGeneration.user_id == user_id,
                    VideoGeneration.status.in_(
                        [
                            VideoGenerationStatus.QUEUED.value,
                            VideoGenerationStatus.SUBMITTING.value,
                            VideoGenerationStatus.SUBMIT_UNKNOWN.value,
                            VideoGenerationStatus.SUBMITTED.value,
                            VideoGenerationStatus.RUNNING.value,
                        ]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    task_ids: list[str] = []
    queued_generation_ids: list[str] = []
    queued_generation_execution_epochs: dict[str, int] = {}
    queued_generation_queue_tokens: dict[str, Any] = {}
    running_generation_ids: list[str] = []
    streaming_completion_ids: list[str] = []
    deferred_generation_ids: list[str] = []
    deferred_completion_ids: list[str] = []
    holds_released = 0
    releasable_queued_generations = [
        generation
        for generation in generations
        if generation.status == GenerationStatus.QUEUED.value
        and not generation_cancel_requires_durable_settlement(generation)
    ]
    releasable_queued_completions = [
        completion
        for completion in completions
        if completion.status == CompletionStatus.QUEUED.value
        and not completion_cancel_requires_durable_settlement(completion)
    ]
    should_release_queued_holds = account_mode == "wallet"
    if not should_release_queued_holds and (
        releasable_queued_generations or releasable_queued_completions
    ):
        should_release_queued_holds = await _account_wallet_exists(db, user_id)

    # The worker owns upstream cancellation and final billing cleanup for
    # active rows; this transaction only finalizes work that never started.
    for generation in generations:
        task_ids.append(generation.id)
        generation.cancel_requested_at = (
            getattr(generation, "cancel_requested_at", None) or canceled_at
        )
        if generation.status == GenerationStatus.QUEUED.value:
            if generation_cancel_requires_durable_settlement(generation):
                deferred_generation_ids.append(generation.id)
                continue
            queued_generation_ids.append(generation.id)
            queued_generation_execution_epochs[generation.id] = current_execution_epoch(
                generation
            )
            generation.status = GenerationStatus.CANCELED.value
            generation.finished_at = canceled_at
            if should_release_queued_holds:
                holds_released += int(
                    await _release_account_delete_task_hold(
                        db,
                        user_id=user_id,
                        ref_type="generation",
                        ref_id=billing_core.generation_billing_ref_id(generation),
                    )
                )
        elif generation.status == GenerationStatus.RUNNING.value:
            running_generation_ids.append(generation.id)
    for completion in completions:
        task_ids.append(completion.id)
        completion.cancel_requested_at = (
            getattr(completion, "cancel_requested_at", None) or canceled_at
        )
        if completion.status == CompletionStatus.QUEUED.value:
            if completion_cancel_requires_durable_settlement(completion):
                deferred_completion_ids.append(completion.id)
                continue
            completion.status = CompletionStatus.CANCELED.value
            completion.finished_at = canceled_at
            if should_release_queued_holds:
                holds_released += int(
                    await _release_account_delete_task_hold(
                        db,
                        user_id=user_id,
                        ref_type="completion",
                        ref_id=billing_core.completion_billing_ref_id(completion),
                    )
                )
        elif completion.status == CompletionStatus.STREAMING.value:
            streaming_completion_ids.append(completion.id)
    video_cleanup = cancel_video_generation_rows(
        video_generations,
        canceled_at=canceled_at,
    )
    videos_result = await db.execute(
        update(Video)
        .where(
            Video.user_id == user_id,
            Video.deleted_at.is_(None),
        )
        .values(deleted_at=canceled_at)
    )
    memory_extractions_canceled = await cancel_account_memory_extractions(
        db,
        user_id=user_id,
        canceled_at=canceled_at,
    )
    return {
        "generations_canceled": len(generations),
        "completions_canceled": len(completions),
        "video_generations_canceled": len(video_cleanup.active_ids),
        "videos_deleted": dml_rowcount(videos_result),
        "memory_extractions_canceled": memory_extractions_canceled,
        "holds_released": holds_released,
        "task_ids": task_ids,
        "queued_generation_ids": queued_generation_ids,
        "queued_generation_execution_epochs": queued_generation_execution_epochs,
        "queued_generation_queue_tokens": queued_generation_queue_tokens,
        "running_generation_ids": running_generation_ids,
        "streaming_completion_ids": streaming_completion_ids,
        "deferred_generation_ids": deferred_generation_ids,
        "deferred_completion_ids": deferred_completion_ids,
        **video_cleanup.cleanup_fields(),
    }


async def cancel_account_memory_extractions(
    db: AsyncSession,
    *,
    user_id: str,
    canceled_at: datetime,
) -> int | None:
    result = await db.execute(
        update(MemoryExtractionRun)
        .where(
            MemoryExtractionRun.user_id == user_id,
            MemoryExtractionRun.status.in_(("pending", "running", "retryable")),
        )
        .values(
            status="canceled",
            owner=None,
            lease_expires_at=None,
            canceled_at=canceled_at,
            cancel_reason="account_deleted",
            fence=MemoryExtractionRun.fence + 1,
            updated_at=canceled_at,
        )
    )
    return dml_rowcount(result)


async def _release_account_generation_queue_state(
    redis: Any,
    task_id: str,
    *,
    expected_execution_epoch: int,
    ownership_token: GenerationQueueReleaseToken,
) -> bool:
    return await release_generation_queue_state(
        redis,
        task_id,
        expected_execution_epoch=expected_execution_epoch,
        ownership_token=ownership_token,
    )


async def post_commit_account_task_cleanup(
    *,
    user_id: str,
    cleanup: dict[str, Any],
) -> None:
    queued_generation_ids = cleanup.get("queued_generation_ids")
    cancel_task_ids = [
        *[
            task_id
            for task_id in cleanup.get("running_generation_ids", [])
            if isinstance(task_id, str)
        ],
        *[
            task_id
            for task_id in cleanup.get("streaming_completion_ids", [])
            if isinstance(task_id, str)
        ],
        *[
            task_id
            for task_id in cleanup.get("deferred_generation_ids", [])
            if isinstance(task_id, str)
        ],
        *[
            task_id
            for task_id in cleanup.get("deferred_completion_ids", [])
            if isinstance(task_id, str)
        ],
    ]
    has_queued_generations = bool(
        isinstance(queued_generation_ids, list) and queued_generation_ids
    )
    if not has_queued_generations and not cancel_task_ids:
        if int(cleanup.get("holds_released") or 0) > 0:
            try:
                await invalidate_balance_cache(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "account deletion balance cache invalidation failed user=%s err=%s",
                    user_id,
                    exc,
                )
        return
    try:
        redis = get_redis()
        queued_generation_entries = (
            await capture_queued_generation_cleanup_entries(redis, cleanup)
        )
        for task_id, execution_epoch, ownership_token in queued_generation_entries:
            await _release_account_generation_queue_state(
                redis,
                task_id,
                expected_execution_epoch=execution_epoch,
                ownership_token=ownership_token,
            )
        for task_id in cancel_task_ids:
            await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "account deletion cancel signal write failed user=%s err=%s", user_id, exc
        )
    if int(cleanup.get("holds_released") or 0) > 0:
        try:
            await invalidate_balance_cache(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "account deletion balance cache invalidation failed user=%s err=%s",
                user_id,
                exc,
            )


__all__ = [
    "cancel_account_active_tasks",
    "cancel_account_memory_extractions",
    "dml_rowcount",
    "post_commit_account_task_cleanup",
]
