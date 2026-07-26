from __future__ import annotations

from .runtime import (
    generation_domain_ports,
    generation_persistence_ports,
    generation_billing_ports,
    generation_events_ports,
    generation_lease_ports,
)
import asyncio
from datetime import datetime, timezone
from typing import Any, Literal


async def raise_if_generation_interrupted(
    redis: Any,
    task_id: str,
    lease_lost: asyncio.Event,
    reason: str,
) -> None:
    if lease_lost.is_set():
        raise generation_lease_ports()._LeaseLost(f"generation lease lost {reason}")
    if await generation_lease_ports()._is_cancelled(redis, task_id):
        raise generation_lease_ports()._TaskCancelled(reason)


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
) -> Literal["failed", "succeeded"]:
    if await generation_lease_ports()._is_cancelled(redis, task_id):
        cancel_message = "cancelled before existing image settlement"
        result = await session.execute(
            generation_persistence_ports()
            ._generation_attempt_update(
                task_id,
                generation.attempt,
                statuses=(generation_domain_ports().GenerationStatus.QUEUED.value,),
            )
            .values(
                status=generation_domain_ports().GenerationStatus.CANCELED.value,
                progress_stage=generation_domain_ports().GenerationStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=generation_domain_ports().EC.CANCELLED.value,
                error_message=cancel_message,
            )
        )
        generation_persistence_ports()._ensure_generation_updated(
            result, task_id, generation.attempt
        )
        message = await session.get(generation_persistence_ports().Message, message_id)
        if message is not None and message.status not in (
            generation_domain_ports().MessageStatus.SUCCEEDED,
            generation_domain_ports().MessageStatus.FAILED,
            generation_domain_ports().MessageStatus.CANCELED,
        ):
            message.status = generation_domain_ports().MessageStatus.FAILED
        await generation_billing_ports().worker_billing.release_generation(
            session,
            generation,
            reason=generation_domain_ports().EC.CANCELLED.value,
        )
        failure_delivery = generation_events_ports()._stage_generation_event(
            session,
            user_id,
            generation_events_ports().task_channel(task_id),
            generation_events_ports().EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": generation_domain_ports().EC.CANCELLED.value,
                "message": cancel_message,
                "retriable": False,
            },
        )
        await session.commit()
        await generation_billing_ports().worker_billing.flush_balance_cache_refreshes(
            session
        )
        await generation_events_ports()._deliver_generation_event(
            redis, failure_delivery
        )
        return "failed"

    generation_events_ports().logger.info(
        "generation already has image task_id=%s image_id=%s \u2014 short-circuit",
        task_id,
        existing_image.id,
    )
    result = await session.execute(
        generation_persistence_ports()
        ._generation_attempt_update(
            task_id,
            generation.attempt,
            statuses=(generation_domain_ports().GenerationStatus.QUEUED.value,),
        )
        .values(
            status=generation_domain_ports().GenerationStatus.SUCCEEDED.value,
            progress_stage=generation_domain_ports().GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            upstream_pixels=existing_image.width * existing_image.height,
            error_code=None,
            error_message=None,
        )
    )
    generation_persistence_ports()._ensure_generation_updated(
        result, task_id, generation.attempt
    )
    message = await session.get(generation_persistence_ports().Message, message_id)
    if message is not None and message.status not in (
        generation_domain_ports().MessageStatus.SUCCEEDED,
        generation_domain_ports().MessageStatus.CANCELED,
    ):
        message.status = generation_domain_ports().MessageStatus.SUCCEEDED
    await generation_billing_ports().worker_billing.settle_generation(
        session,
        generation,
        width=existing_image.width,
        height=existing_image.height,
    )
    success_delivery = generation_events_ports()._stage_generation_event(
        session,
        user_id,
        generation_events_ports().task_channel(task_id),
        generation_events_ports().EV_GEN_SUCCEEDED,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "images": [
                {
                    "image_id": existing_image.id,
                    "from_generation_id": task_id,
                    "actual_size": f"{existing_image.width}x{existing_image.height}",
                    "url": generation_persistence_ports().storage.public_url(
                        existing_image.storage_key
                    ),
                }
            ],
            "final_size": f"{existing_image.width}x{existing_image.height}",
        },
    )
    await session.commit()
    await generation_billing_ports().worker_billing.flush_balance_cache_refreshes(
        session
    )
    await generation_events_ports()._deliver_generation_event(redis, success_delivery)
    try:
        duration = asyncio.get_event_loop().time() - task_started_at
        generation_events_ports().task_duration_seconds.labels(
            kind="generation",
            outcome=generation_events_ports().safe_outcome("succeeded"),
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
) -> Literal["failed", "stale_attempt"]:
    generation_events_ports().logger.info(
        "generation cancelled by user task=%s reason=%s",
        task_id,
        reason,
    )
    failure_delivery = None
    try:
        async with generation_persistence_ports().SessionLocal() as session:
            result = await session.execute(
                generation_persistence_ports()
                ._generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=generation_domain_ports()._RUNNING_GENERATION_STATUSES,
                )
                .values(
                    status=generation_domain_ports().GenerationStatus.CANCELED.value,
                    progress_stage=generation_domain_ports().GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=generation_domain_ports().EC.CANCELLED.value,
                    error_message="cancelled by user",
                )
            )
            generation_persistence_ports()._ensure_generation_updated(
                result, task_id, attempt
            )
            message = await session.get(
                generation_persistence_ports().Message, message_id
            )
            if message is not None and message.status not in (
                generation_domain_ports().MessageStatus.SUCCEEDED,
                generation_domain_ports().MessageStatus.FAILED,
                generation_domain_ports().MessageStatus.CANCELED,
            ):
                message.status = generation_domain_ports().MessageStatus.FAILED
            generation = await session.get(
                generation_persistence_ports().Generation, task_id
            )
            if generation is not None:
                await generation_billing_ports().worker_billing.release_generation(
                    session,
                    generation,
                    reason="cancelled",
                )
            failure_delivery = generation_events_ports()._stage_generation_event(
                session,
                user_id,
                generation_events_ports().task_channel(task_id),
                generation_events_ports().EV_GEN_FAILED,
                {
                    "generation_id": task_id,
                    "message_id": message_id,
                    "code": "cancelled",
                    "message": "cancelled by user",
                    "retriable": False,
                },
            )
            await session.commit()
            await (
                generation_billing_ports().worker_billing.flush_balance_cache_refreshes(
                    session
                )
            )
    except generation_domain_ports()._StaleGenerationAttempt as stale_exc:
        generation_events_ports().logger.info(
            "generation cancel stale attempt task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return "stale_attempt"
    except Exception as db_exc:  # noqa: BLE001
        generation_events_ports().logger.warning(
            "generation cancel DB update failed task=%s err=%s",
            task_id,
            db_exc,
        )
    if failure_delivery is not None:
        await generation_events_ports()._deliver_generation_event(
            redis, failure_delivery
        )
    else:
        await generation_events_ports().publish_event(
            redis,
            user_id,
            generation_events_ports().task_channel(task_id),
            generation_events_ports().EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": "cancelled",
                "message": "cancelled by user",
                "retriable": False,
            },
        )
    return "failed"
