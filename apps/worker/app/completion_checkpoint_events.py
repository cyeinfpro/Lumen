"""Durable redelivery for committed completion image events."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .completion_checkpoint_payloads import (
    COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY,
    CompletionCheckpointCorrupt,
)
from .tasks.completion_parts.image_storage_runtime import (
    COMPLETION_IMAGE_EVENT_METADATA_KEY,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CompletionCheckpointImageEventContext:
    user_id: str
    channel: str
    task_id: str
    message_id: str
    attempt: int
    attempt_epoch: int
    execution_epoch: int


async def ensure_checkpoint_image_event(
    service: Any,
    session: Any,
    *,
    image: Any,
    image_payload: dict[str, Any],
    context: CompletionCheckpointImageEventContext,
) -> tuple[str, bool]:
    metadata = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
    event_id = metadata.get(COMPLETION_IMAGE_EVENT_METADATA_KEY)
    if isinstance(event_id, str) and event_id:
        event = await session.get(service.events.outbox_model, event_id)
        if event is None:
            raise CompletionCheckpointCorrupt(
                f"completion image event outbox missing image={image.id}"
            )
        return event_id, getattr(event, "published_at", None) is not None

    if service.events.stage is None:
        raise CompletionCheckpointCorrupt(
            f"completion image event staging unavailable image={image.id}"
        )
    delivery = service.events.stage(
        session,
        kind="sse",
        payload={
            "user_id": context.user_id,
            "channel": context.channel,
            "event_name": service.events.image_event,
            "data": {
                "completion_id": context.task_id,
                "message_id": context.message_id,
                "attempt": context.attempt,
                "attempt_epoch": context.attempt_epoch,
                "execution_epoch": context.execution_epoch,
                "images": [dict(image_payload)],
            },
        },
    )
    event_id = delivery[0]
    image.metadata_jsonb = {
        **metadata,
        COMPLETION_IMAGE_EVENT_METADATA_KEY: event_id,
    }
    await session.commit()
    return event_id, False


async def redeliver_checkpoint_image_events(
    completion: Any,
    *,
    redis: Any,
    tool_image_service: Any,
    images: list[dict[str, Any]],
    mark_published: Callable[..., Awaitable[dict[str, Any]]],
) -> None:
    repository = tool_image_service.repository
    for image in images:
        event_id = str(image[COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY])
        async with repository.session_factory() as session:
            event = await session.get(tool_image_service.events.outbox_model, event_id)
        if event is None:
            raise CompletionCheckpointCorrupt(
                f"completion image event outbox missing image={image['image_id']}"
            )
        if getattr(event, "published_at", None) is None:
            if tool_image_service.events.deliver is None:
                raise CompletionCheckpointCorrupt(
                    f"completion image event delivery unavailable image={image['image_id']}"
                )
            try:
                await tool_image_service.events.deliver(
                    redis,
                    [(event_id, str(event.kind), dict(event.payload or {}))],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "completion image event redelivery deferred image=%s event=%s err=%s",
                    image["image_id"],
                    event_id,
                    exc,
                )
                continue
            async with repository.session_factory() as session:
                event = await session.get(
                    tool_image_service.events.outbox_model,
                    event_id,
                )
            if event is None or getattr(event, "published_at", None) is None:
                continue
        completion.upstream_request = await mark_published(
            repository,
            task_id=str(completion.id),
            attempt_epoch=int(completion.attempt or 0),
            execution_epoch=int(completion.execution_epoch or 0),
            image_id=str(image["image_id"]),
        )


__all__ = [
    "CompletionCheckpointImageEventContext",
    "ensure_checkpoint_image_event",
    "redeliver_checkpoint_image_events",
]
