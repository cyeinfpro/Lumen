"""Task cleanup coordination used by the regenerate route."""

from __future__ import annotations

from datetime import datetime
from functools import partial
import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.model_entities import Completion, Generation

from .active_task_cleanup import (
    cancel_completion_rows,
    cancel_generation_rows,
    has_releasable_queued_tasks,
    post_commit_best_effort_cleanup,
)
from .generation_queue import (
    capture_generation_queue_state,
    completion_cancel_requires_durable_settlement,
    current_execution_epoch,
    generation_cancel_requires_durable_settlement,
    queued_generation_cleanup_entries,
)


HoldRelease = Callable[..., Awaitable[bool]]
WalletExists = Callable[[AsyncSession, str], Awaitable[bool]]


def cleanup_string_list(cleanup: dict[str, Any], key: str) -> list[str]:
    values = cleanup.get(key)
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


async def cancel_regenerate_target_active_tasks(
    db: AsyncSession,
    *,
    target_msg_id: str,
    user_id: str,
    canceled_at: datetime,
    account_mode: str,
    queue_redis: Any | None = None,
    release_hold: HoldRelease,
    wallet_exists: WalletExists,
    logger: logging.Logger,
) -> dict[str, Any]:
    generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id == target_msg_id,
                    Generation.status.in_(
                        [
                            GenerationStatus.QUEUED.value,
                            GenerationStatus.RUNNING.value,
                        ]
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
                    Completion.message_id == target_msg_id,
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

    should_release_queued_holds = account_mode == "wallet"
    if not should_release_queued_holds and has_releasable_queued_tasks(
        generations,
        completions,
        generation_requires_durable_settlement=(
            generation_cancel_requires_durable_settlement
        ),
        completion_requires_durable_settlement=(
            completion_cancel_requires_durable_settlement
        ),
    ):
        should_release_queued_holds = await wallet_exists(db, user_id)

    # Active predecessors stay active until their worker observes the cancel
    # signal, so their wallet hold cannot be released ahead of upstream work.
    hold_release = (
        partial(
            release_hold,
            db,
            user_id=user_id,
        )
        if should_release_queued_holds
        else None
    )
    generation_cleanup = await cancel_generation_rows(
        generations,
        canceled_at=canceled_at,
        cancel_message="regenerate cancelled old assistant",
        queue_redis=queue_redis,
        capture_queue_ownership=queue_redis is not None,
        logger=logger,
        snapshot_failure_message=(
            "regenerate image_queue ownership snapshot failed task=%s err=%s"
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
        cancel_message="regenerate cancelled old assistant",
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
        **generation_cleanup.cleanup_fields(),
        **completion_cleanup.cleanup_fields(),
    }


async def post_commit_regenerate_cancel_cleanup(
    redis: Any,
    *,
    user_id: str,
    cleanup: dict[str, Any],
    release_queue_state: Callable[..., Awaitable[Any]],
    invalidate_balance: Callable[[str], Awaitable[None]],
    logger: logging.Logger,
) -> None:
    active_task_ids = [
        *cleanup_string_list(cleanup, "running_generation_ids"),
        *cleanup_string_list(cleanup, "streaming_completion_ids"),
        *cleanup_string_list(cleanup, "deferred_generation_ids"),
        *cleanup_string_list(cleanup, "deferred_completion_ids"),
    ]
    await post_commit_best_effort_cleanup(
        redis,
        user_id=user_id,
        queued_entries=queued_generation_cleanup_entries(cleanup),
        cancel_ids=active_task_ids,
        invalidate_balance_required=int(cleanup.get("holds_released") or 0) > 0,
        release_queue_state=release_queue_state,
        invalidate_balance=invalidate_balance,
        logger=logger,
        queue_failure_message="regenerate image_queue release failed gen=%s err=%s",
        cancel_failure_message="regenerate cancel signal failed task=%s err=%s",
        balance_failure_message=(
            "regenerate balance cache invalidation failed user=%s err=%s"
        ),
    )


__all__ = [
    "cancel_regenerate_target_active_tasks",
    "cleanup_string_list",
    "post_commit_regenerate_cancel_cleanup",
]
