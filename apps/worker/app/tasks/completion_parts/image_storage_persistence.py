"""ORM staging for completion tool-image artifacts and events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_core.constants import ImageSource

COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY = "_completion_image_event_outbox_id"
COMPLETION_IMAGE_EVENT_PUBLISHED_KEY = "_completion_image_event_published"
COMPLETION_IMAGE_EVENT_METADATA_KEY = "completion_image_event_outbox_id"


@dataclass(frozen=True, slots=True)
class CompletionToolImageMetadata:
    extension: str
    mime: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    blurhash: str | None


@dataclass(frozen=True, slots=True)
class CompletionToolImageKeys:
    original: str
    display: str
    preview: str
    thumbnail: str


@dataclass(frozen=True, slots=True)
class CompletionToolImageVariantSizes:
    display: tuple[int, int]
    preview: tuple[int, int]
    thumbnail: tuple[int, int]


@dataclass(frozen=True, slots=True)
class PreparedCompletionToolImage:
    image_id: str
    metadata: CompletionToolImageMetadata
    keys: CompletionToolImageKeys
    variant_sizes: CompletionToolImageVariantSizes
    delivery_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompletionToolImageWrite:
    task_id: str
    attempt_epoch: int
    execution_epoch: int
    user_id: str
    message_id: str
    revised_prompt: str | None
    billing_budget_micro: int
    event_context: Any | None


async def stage_completion_tool_image(
    service: Any,
    session: Any,
    *,
    prepared: PreparedCompletionToolImage,
    write: CompletionToolImageWrite,
) -> dict[str, Any]:
    repository = service.repository
    metadata = prepared.metadata
    keys = prepared.keys
    image = repository.image_model(
        id=prepared.image_id,
        user_id=write.user_id,
        owner_generation_id=None,
        source=ImageSource.GENERATED,
        parent_image_id=None,
        storage_key=keys.original,
        mime=metadata.mime,
        width=metadata.width,
        height=metadata.height,
        size_bytes=metadata.size_bytes,
        sha256=metadata.sha256,
        blurhash=metadata.blurhash,
        visibility="private",
        metadata_jsonb={
            "source": "completion_tool",
            "completion_id": write.task_id,
            "completion_attempt_epoch": write.attempt_epoch,
            "completion_execution_epoch": write.execution_epoch,
            **(
                {"revised_prompt": write.revised_prompt}
                if write.revised_prompt
                else {}
            ),
        },
    )
    session.add(image)
    for kind, storage_key, size in (
        ("display2048", keys.display, prepared.variant_sizes.display),
        ("preview1024", keys.preview, prepared.variant_sizes.preview),
        ("thumb256", keys.thumbnail, prepared.variant_sizes.thumbnail),
    ):
        session.add(
            repository.image_variant_model(
                image_id=prepared.image_id,
                kind=kind,
                storage_key=storage_key,
                width=size[0],
                height=size[1],
            )
        )

    message = await session.get(repository.message_model, write.message_id)
    if message is not None:
        content = dict(message.content or {})
        images = list(content.get("images") or [])
        images.append(
            {
                **prepared.delivery_payload,
                "width": metadata.width,
                "height": metadata.height,
            }
        )
        content["images"] = images
        message.content = content

    await repository.record_usage(
        session=session,
        task_id=write.task_id,
        attempt_epoch=write.attempt_epoch,
        execution_epoch=write.execution_epoch,
        budget_micro=write.billing_budget_micro,
    )
    image_payload = {
        **prepared.delivery_payload,
        "actual_size": f"{metadata.width}x{metadata.height}",
    }
    if write.event_context is None:
        return image_payload
    if service.events.stage is None:
        raise RuntimeError("completion image durable event staging is not configured")

    write.event_context.delivery = service.events.stage(
        session,
        kind="sse",
        payload={
            "user_id": write.user_id,
            "channel": write.event_context.channel,
            "event_name": service.events.image_event,
            "data": {
                "completion_id": write.task_id,
                "message_id": write.message_id,
                "attempt": write.event_context.attempt,
                "attempt_epoch": write.attempt_epoch,
                "execution_epoch": write.execution_epoch,
                "images": [dict(image_payload)],
            },
        },
    )
    event_id = write.event_context.delivery[0]
    image.metadata_jsonb = {
        **dict(image.metadata_jsonb or {}),
        COMPLETION_IMAGE_EVENT_METADATA_KEY: event_id,
    }
    image_payload[COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY] = event_id
    return image_payload


__all__ = [
    "COMPLETION_IMAGE_EVENT_METADATA_KEY",
    "COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY",
    "COMPLETION_IMAGE_EVENT_PUBLISHED_KEY",
    "CompletionToolImageKeys",
    "CompletionToolImageMetadata",
    "CompletionToolImageVariantSizes",
    "CompletionToolImageWrite",
    "PreparedCompletionToolImage",
    "stage_completion_tool_image",
]
