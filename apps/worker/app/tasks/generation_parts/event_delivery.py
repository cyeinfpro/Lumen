"""Generation SSE delivery helpers backed by the transactional outbox."""

from __future__ import annotations

import logging
from typing import Any

from lumen_core.constants import EV_GEN_FAILED, EV_GEN_SUCCEEDED

from ...db import SessionLocal
from ...sse_publish import (
    SSEPublishRetryableError,
    publish_event as _publish_sse_event,
)
from .. import outbox


logger = logging.getLogger(__name__)

GenerationEventDelivery = tuple[str, str, dict[str, Any]]


def stage_generation_event(
    session: Any,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> GenerationEventDelivery:
    return outbox._stage_outbox_event(
        session,
        kind="sse",
        payload={
            "user_id": user_id,
            "channel": channel,
            "event_name": event_name,
            "data": data,
        },
    )


def stage_generation_success_event(
    session: Any,
    user_id: str,
    channel: str,
    *,
    generation_id: str,
    message_id: str,
    image_id: str,
    actual_size: str,
    mime: str,
    image_url: str,
    filename: object,
    image_payload_meta: dict[str, Any],
    diagnostics: dict[str, Any],
) -> GenerationEventDelivery:
    return stage_generation_event(
        session,
        user_id,
        channel,
        EV_GEN_SUCCEEDED,
        {
            "generation_id": generation_id,
            "message_id": message_id,
            "images": [
                {
                    "image_id": image_id,
                    "from_generation_id": generation_id,
                    "actual_size": actual_size,
                    "mime": mime,
                    "url": image_url,
                    "display_url": f"/api/images/{image_id}/variants/display2048",
                    "preview_url": f"/api/images/{image_id}/variants/preview1024",
                    "thumb_url": f"/api/images/{image_id}/variants/thumb256",
                    "filename": filename,
                    **image_payload_meta,
                }
            ],
            "final_size": actual_size,
            "diagnostics": diagnostics,
        },
    )


def stage_generation_failure_event(
    session: Any,
    user_id: str,
    channel: str,
    *,
    generation_id: str,
    message_id: str,
    code: str,
    message: str,
    diagnostics: dict[str, Any],
    safe_error_summary: dict[str, Any],
    error_details: dict[str, Any] | None,
) -> GenerationEventDelivery:
    data = {
        "generation_id": generation_id,
        "message_id": message_id,
        "code": code,
        "message": message,
        "retriable": False,
        "diagnostics": diagnostics,
        "safe_error_summary": safe_error_summary,
    }
    if error_details:
        data["error_details"] = error_details
    return stage_generation_event(
        session,
        user_id,
        channel,
        EV_GEN_FAILED,
        data,
    )


async def deliver_generation_events(
    redis: Any,
    deliveries: list[GenerationEventDelivery],
) -> None:
    await outbox._deliver_staged_outbox_events(redis, deliveries)


async def deliver_generation_event(
    redis: Any,
    delivery: GenerationEventDelivery,
) -> None:
    await deliver_generation_events(redis, [delivery])


async def _persist_generation_event_for_retry(
    *,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    async with SessionLocal() as session:
        stage_generation_event(
            session,
            user_id,
            channel,
            event_name,
            data,
        )
        await session.commit()


async def publish_event(
    redis: Any,
    user_id: str,
    channel: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    """Publish progress without turning delivery faults into task faults."""

    try:
        await _publish_sse_event(redis, user_id, channel, event_name, data)
    except SSEPublishRetryableError as exc:
        try:
            await _persist_generation_event_for_retry(
                user_id=user_id,
                channel=channel,
                event_name=event_name,
                data=data,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "generation event outbox staging failed task=%s event=%s",
                data.get("generation_id"),
                event_name,
            )
        else:
            logger.warning(
                "generation event deferred to outbox task=%s event=%s stream=%s",
                data.get("generation_id"),
                event_name,
                exc.stream_key,
            )
