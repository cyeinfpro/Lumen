from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from lumen_core.constants import (
    EV_GEN_FAILED,
    EV_GEN_SUCCEEDED,
    GenerationErrorCode as EC,
    GenerationStage,
    GenerationStatus,
    MessageStatus,
    task_channel,
)
from lumen_core.models import Generation, Message

from ...observability import safe_outcome, task_duration_seconds
from ...task_cancellation import force_next_cancellation_check
from .errors import LeaseLost, StaleGenerationAttempt, TaskCancelled
from .event_delivery import stage_generation_event
from .execution_boundary import (
    release_or_settle_generation,
    release_would_absorb_upstream_cost,
)
from .lease import is_cancelled
from .retry_state import (
    RUNNING_GENERATION_STATUSES,
    ensure_generation_updated,
    generation_attempt_update,
)
from .services import RunGenerationDeps


logger = logging.getLogger(__name__)


async def raise_if_generation_interrupted(
    redis: Any,
    task_id: str,
    lease_lost: asyncio.Event,
    reason: str,
) -> None:
    if lease_lost.is_set():
        raise LeaseLost(f"generation lease lost {reason}")
    force_next_cancellation_check(task_id)
    if await is_cancelled(redis, task_id):
        raise TaskCancelled(reason)


async def settle_existing_generated_image(
    session: Any,
    *,
    redis: Any,
    task_id: str,
    user_id: str,
    message_id: str,
    generation: Any,
    existing_image: Any,
    task_started_at: float,
    services: RunGenerationDeps,
) -> Literal["failed", "succeeded"]:
    force_next_cancellation_check(task_id)
    if await is_cancelled(redis, task_id):
        cancel_message = "cancelled before existing image settlement"
        result = await session.execute(
            generation_attempt_update(
                task_id,
                generation.attempt,
                statuses=(GenerationStatus.QUEUED.value,),
                allow_cancel_requested=True,
            )
            .where(Generation.cancel_requested_at.is_not(None))
            .values(
                status=GenerationStatus.CANCELED.value,
                progress_stage=GenerationStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=EC.CANCELLED.value,
                error_message=cancel_message,
            )
        )
        ensure_generation_updated(result, task_id, generation.attempt)
        message = await session.get(Message, message_id)
        if message is not None and message.status not in (
            MessageStatus.SUCCEEDED,
            MessageStatus.FAILED,
            MessageStatus.CANCELED,
        ):
            message.status = MessageStatus.FAILED
        # 既有图片意味着此前尝试已在上游产出并计费:一旦收据证实当前 dispatch
        # 已收到响应,取消也走结算而不是退款(纯转嫁)。
        if release_would_absorb_upstream_cost(None, generation):
            await services.billing.settle_unknown_upstream(
                session,
                generation,
                reason=EC.CANCELLED.value,
                knowledge="incurred",
            )
        else:
            await release_or_settle_generation(
                services.billing,
                session,
                generation,
                reason=EC.CANCELLED.value,
            )
        failure_delivery = stage_generation_event(
            session,
            user_id,
            task_channel(task_id),
            EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": EC.CANCELLED.value,
                "message": cancel_message,
                "retriable": False,
            },
        )
        await session.commit()
        await services.billing.flush_after_commit(session)
        await services.events.deliver(redis, failure_delivery)
        return "failed"

    logger.info(
        "generation already has image task_id=%s image_id=%s - short-circuit",
        task_id,
        existing_image.id,
    )
    result = await session.execute(
        generation_attempt_update(
            task_id,
            generation.attempt,
            statuses=(GenerationStatus.QUEUED.value,),
        ).values(
            status=GenerationStatus.SUCCEEDED.value,
            progress_stage=GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            upstream_pixels=existing_image.width * existing_image.height,
            error_code=None,
            error_message=None,
        )
    )
    ensure_generation_updated(result, task_id, generation.attempt)
    message = await session.get(Message, message_id)
    if message is not None and message.status not in (
        MessageStatus.SUCCEEDED,
        MessageStatus.CANCELED,
    ):
        message.status = MessageStatus.SUCCEEDED
    await services.billing.settle(
        session,
        generation,
        width=existing_image.width,
        height=existing_image.height,
    )
    success_delivery = stage_generation_event(
        session,
        user_id,
        task_channel(task_id),
        EV_GEN_SUCCEEDED,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "images": [
                {
                    "image_id": existing_image.id,
                    "from_generation_id": task_id,
                    "actual_size": (f"{existing_image.width}x{existing_image.height}"),
                    "url": services.artifacts.public_url(existing_image.storage_key),
                }
            ],
            "final_size": (f"{existing_image.width}x{existing_image.height}"),
        },
    )
    await session.commit()
    await services.billing.flush_after_commit(session)
    await services.events.deliver(redis, success_delivery)
    try:
        duration = asyncio.get_event_loop().time() - task_started_at
        task_duration_seconds.labels(
            kind="generation",
            outcome=safe_outcome("succeeded"),
        ).observe(duration)
    except Exception:  # noqa: BLE001
        pass
    return "succeeded"


async def finalize_running_generation_cancel(
    redis: Any,
    *,
    task_id: str,
    message_id: str,
    user_id: str,
    attempt: int,
    reason: BaseException,
    services: RunGenerationDeps,
) -> Literal["failed", "stale_attempt"]:
    logger.info(
        "generation cancelled by user task=%s reason=%s",
        task_id,
        reason,
    )
    failure_delivery = None
    try:
        async with services.store.session() as session:
            result = await session.execute(
                generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=RUNNING_GENERATION_STATUSES,
                    allow_cancel_requested=True,
                )
                .where(Generation.cancel_requested_at.is_not(None))
                .values(
                    status=GenerationStatus.CANCELED.value,
                    progress_stage=GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=EC.CANCELLED.value,
                    error_message="cancelled by user",
                )
            )
            ensure_generation_updated(result, task_id, attempt)
            message = await session.get(Message, message_id)
            if message is not None and message.status not in (
                MessageStatus.SUCCEEDED,
                MessageStatus.FAILED,
                MessageStatus.CANCELED,
            ):
                message.status = MessageStatus.FAILED
            generation = await session.get(Generation, task_id)
            if generation is not None:
                # 取消前若当前 dispatch 已收到响应,上游成本已经发生,必须结算
                # 而不是 release(reason 是触发取消的异常,本地失败不满足决策表
                # 的「适配层已证明未计费」前提)。
                if release_would_absorb_upstream_cost(reason, generation):
                    await services.billing.settle_unknown_upstream(
                        session,
                        generation,
                        reason="cancelled",
                        knowledge="incurred",
                    )
                else:
                    await release_or_settle_generation(
                        services.billing,
                        session,
                        generation,
                        reason="cancelled",
                    )
            failure_delivery = stage_generation_event(
                session,
                user_id,
                task_channel(task_id),
                EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": "cancelled",
                    "message": "cancelled by user",
                    "retriable": False,
                },
            )
            await session.commit()
            await services.billing.flush_after_commit(session)
    except StaleGenerationAttempt as stale_exc:
        logger.info(
            "generation cancel stale attempt task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return "stale_attempt"
    except Exception as db_exc:  # noqa: BLE001
        logger.warning(
            "generation cancel DB update failed task=%s err=%s",
            task_id,
            db_exc,
        )
    if failure_delivery is not None:
        await services.events.deliver(redis, failure_delivery)
    else:
        await services.events.publish(
            redis,
            user_id,
            task_channel(task_id),
            EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": False,
            },
        )
    return "failed"
