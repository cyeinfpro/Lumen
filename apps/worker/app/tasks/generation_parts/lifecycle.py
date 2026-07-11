from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind


async def raise_if_generation_interrupted(
    redis: Any,
    task_id: str,
    lease_lost: asyncio.Event,
    reason: str,
) -> None:
    if lease_lost.is_set():
        raise _g._LeaseLost(f"generation lease lost {reason}")
    if await _g._is_cancelled(redis, task_id):
        raise _g._TaskCancelled(reason)


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
    if await _g._is_cancelled(redis, task_id):
        cancel_message = "cancelled before existing image settlement"
        result = await session.execute(
            _g._generation_attempt_update(
                task_id,
                generation.attempt,
                statuses=(_g.GenerationStatus.QUEUED.value,),
            ).values(
                status=_g.GenerationStatus.CANCELED.value,
                progress_stage=_g.GenerationStage.FINALIZING,
                finished_at=datetime.now(timezone.utc),
                error_code=_g.EC.CANCELLED.value,
                error_message=cancel_message,
            )
        )
        _g._ensure_generation_updated(result, task_id, generation.attempt)
        message = await session.get(_g.Message, message_id)
        if message is not None and message.status not in (
            _g.MessageStatus.SUCCEEDED,
            _g.MessageStatus.FAILED,
            _g.MessageStatus.CANCELED,
        ):
            message.status = _g.MessageStatus.FAILED
        await _g.worker_billing.release_generation(
            session,
            generation,
            reason=_g.EC.CANCELLED.value,
        )
        await session.commit()
        await _g.worker_billing.flush_balance_cache_refreshes(session)
        await _g.publish_event(
            redis,
            user_id,
            _g.task_channel(task_id),
            _g.EV_GEN_FAILED,
            {
                "generation_id": task_id,
                "message_id": message_id,
                "code": _g.EC.CANCELLED.value,
                "message": cancel_message,
                "retriable": False,
            },
        )
        return "failed"

    _g.logger.info(
        "generation already has image task_id=%s image_id=%s \u2014 short-circuit",
        task_id,
        existing_image.id,
    )
    result = await session.execute(
        _g._generation_attempt_update(
            task_id,
            generation.attempt,
            statuses=(_g.GenerationStatus.QUEUED.value,),
        ).values(
            status=_g.GenerationStatus.SUCCEEDED.value,
            progress_stage=_g.GenerationStage.FINALIZING,
            finished_at=datetime.now(timezone.utc),
            upstream_pixels=existing_image.width * existing_image.height,
            error_code=None,
            error_message=None,
        )
    )
    _g._ensure_generation_updated(result, task_id, generation.attempt)
    message = await session.get(_g.Message, message_id)
    if message is not None and message.status not in (
        _g.MessageStatus.SUCCEEDED,
        _g.MessageStatus.CANCELED,
    ):
        message.status = _g.MessageStatus.SUCCEEDED
    await _g.worker_billing.settle_generation(
        session,
        generation,
        width=existing_image.width,
        height=existing_image.height,
    )
    await session.commit()
    await _g.worker_billing.flush_balance_cache_refreshes(session)
    await _g.publish_event(
        redis,
        user_id,
        _g.task_channel(task_id),
        _g.EV_GEN_SUCCEEDED,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "images": [
                {
                    "image_id": existing_image.id,
                    "from_generation_id": task_id,
                    "actual_size": (f"{existing_image.width}x{existing_image.height}"),
                    "url": _g.storage.public_url(existing_image.storage_key),
                }
            ],
            "final_size": f"{existing_image.width}x{existing_image.height}",
        },
    )
    try:
        duration = asyncio.get_event_loop().time() - task_started_at
        _g.task_duration_seconds.labels(
            kind="generation",
            outcome=_g.safe_outcome("succeeded"),
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
    _g.logger.info(
        "generation cancelled by user task=%s reason=%s",
        task_id,
        reason,
    )
    try:
        async with _g.SessionLocal() as session:
            result = await session.execute(
                _g._generation_attempt_update(
                    task_id,
                    attempt,
                    statuses=_g._RUNNING_GENERATION_STATUSES,
                ).values(
                    status=_g.GenerationStatus.CANCELED.value,
                    progress_stage=_g.GenerationStage.FINALIZING,
                    finished_at=datetime.now(timezone.utc),
                    error_code=_g.EC.CANCELLED.value,
                    error_message="cancelled by user",
                )
            )
            _g._ensure_generation_updated(result, task_id, attempt)
            message = await session.get(_g.Message, message_id)
            if message is not None and message.status not in (
                _g.MessageStatus.SUCCEEDED,
                _g.MessageStatus.FAILED,
                _g.MessageStatus.CANCELED,
            ):
                message.status = _g.MessageStatus.FAILED
            generation = await session.get(_g.Generation, task_id)
            if generation is not None:
                await _g.worker_billing.release_generation(
                    session,
                    generation,
                    reason="cancelled",
                )
            await session.commit()
            await _g.worker_billing.flush_balance_cache_refreshes(session)
    except _g._StaleGenerationAttempt as stale_exc:
        _g.logger.info(
            "generation cancel stale attempt task=%s attempt=%s err=%s",
            task_id,
            attempt,
            stale_exc,
        )
        return "stale_attempt"
    except Exception as db_exc:  # noqa: BLE001
        _g.logger.warning(
            "generation cancel DB update failed task=%s err=%s",
            task_id,
            db_exc,
        )
    await _g.publish_event(
        redis,
        user_id,
        _g.task_channel(task_id),
        _g.EV_GEN_FAILED,
        {
            "generation_id": task_id,
            "message_id": message_id,
            "code": "cancelled",
            "message": "cancelled by user",
            "retriable": False,
        },
    )
    return "failed"
