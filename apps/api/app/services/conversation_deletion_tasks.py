"""Task and image cleanup orchestration for conversation deletion."""

from __future__ import annotations

from datetime import datetime
from functools import partial
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.model_entities import Completion, Generation, Image, Message

from ..db import affected_rows
from .active_task_cleanup import (
    cancel_completion_rows,
    cancel_generation_rows,
    has_releasable_queued_tasks,
    post_commit_fail_fast_cleanup,
)
from .generation_queue import (
    capture_queued_generation_cleanup_entries,
    capture_generation_queue_state,
    completion_cancel_requires_durable_settlement,
    current_execution_epoch,
    generation_cancel_requires_durable_settlement,
)


HoldRelease = Callable[..., Awaitable[bool]]
WalletExists = Callable[[AsyncSession, str], Awaitable[bool]]


async def soft_delete_conversation_generated_images(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    deleted_at: datetime,
) -> int:
    generation_ids = (
        select(Generation.id)
        .join(Message, Message.id == Generation.message_id)
        .where(
            Message.conversation_id == conv_id,
            Generation.user_id == user_id,
        )
    )
    result = await db.execute(
        update(Image)
        .where(
            Image.user_id == user_id,
            Image.deleted_at.is_(None),
            Image.owner_generation_id.in_(generation_ids),
        )
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    return affected_rows(result)


async def cancel_conversation_active_tasks(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    canceled_at: datetime,
    account_mode: str = "wallet",
    queue_redis: Any | None = None,
    release_hold: HoldRelease,
    wallet_exists: WalletExists,
    logger: logging.Logger,
) -> dict[str, Any]:
    message_ids = select(Message.id).where(Message.conversation_id == conv_id)
    generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id.in_(message_ids),
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
                    Completion.message_id.in_(message_ids),
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
    active_generation_ids = [generation.id for generation in generations]
    active_completion_ids = [completion.id for completion in completions]
    release_queued_holds = account_mode == "wallet"
    if not release_queued_holds and has_releasable_queued_tasks(
        generations,
        completions,
        generation_requires_durable_settlement=(
            generation_cancel_requires_durable_settlement
        ),
        completion_requires_durable_settlement=(
            completion_cancel_requires_durable_settlement
        ),
    ):
        release_queued_holds = await wallet_exists(db, user_id)

    hold_release = (
        partial(
            release_hold,
            db,
            user_id=user_id,
            reason="conversation deleted",
        )
        if release_queued_holds
        else None
    )
    generation_cleanup = await cancel_generation_rows(
        generations,
        canceled_at=canceled_at,
        cancel_message="conversation deleted",
        queue_redis=queue_redis,
        capture_queue_ownership=queue_redis is not None,
        logger=logger,
        snapshot_failure_message=(
            "conversation deletion image_queue ownership snapshot failed task=%s err=%s"
        ),
        requires_durable_settlement=(generation_cancel_requires_durable_settlement),
        execution_epoch_for=current_execution_epoch,
        capture_queue_state=capture_generation_queue_state,
        release_hold=hold_release,
        billing_ref_id=billing_core.generation_billing_ref_id,
    )
    completion_cleanup = await cancel_completion_rows(
        completions,
        canceled_at=canceled_at,
        cancel_message="conversation deleted",
        requires_durable_settlement=(completion_cancel_requires_durable_settlement),
        release_hold=hold_release,
        billing_ref_id=billing_core.completion_billing_ref_id,
    )
    return {
        "generations_canceled": len(generations),
        "completions_canceled": len(completions),
        "holds_released": (
            generation_cleanup.holds_released + completion_cleanup.holds_released
        ),
        "active_generation_ids": active_generation_ids,
        "active_completion_ids": active_completion_ids,
        **generation_cleanup.cleanup_fields(),
        **completion_cleanup.cleanup_fields(),
    }


async def post_commit_conversation_task_cleanup(
    *,
    user_id: str,
    cleanup: dict[str, Any],
    get_redis: Callable[[], Any],
    release_queue_state: Callable[..., Awaitable[Any]],
    invalidate_balance: Callable[[str], Awaitable[None]],
    logger: logging.Logger,
) -> None:
    cancel_ids = [
        task_id
        for task_id in [
            *cleanup.get("running_generation_ids", []),
            *cleanup.get("streaming_completion_ids", []),
            *cleanup.get("deferred_generation_ids", []),
            *cleanup.get("deferred_completion_ids", []),
        ]
        if isinstance(task_id, str)
    ]
    queued_ids = cleanup.get("queued_generation_ids")
    redis = get_redis() if queued_ids or cancel_ids else None
    queued_entries = (
        await capture_queued_generation_cleanup_entries(redis, cleanup)
        if redis is not None
        else []
    )
    await post_commit_fail_fast_cleanup(
        user_id=user_id,
        queued_entries=queued_entries,
        cancel_ids=cancel_ids,
        invalidate_balance_required=cleanup.get("holds_released", 0) > 0,
        get_redis=(lambda: redis),
        release_queue_state=release_queue_state,
        invalidate_balance=invalidate_balance,
        logger=logger,
        signal_failure_message=(
            "conversation deletion cancel signal write failed user=%s err=%s"
        ),
        balance_failure_message=(
            "conversation deletion balance cache invalidation failed user=%s err=%s"
        ),
    )


__all__ = [
    "cancel_conversation_active_tasks",
    "post_commit_conversation_task_cleanup",
    "soft_delete_conversation_generated_images",
]
