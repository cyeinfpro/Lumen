"""Serialized persistence for deterministic completion checkpoint images."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, AsyncIterator

from lumen_core.constants import CompletionStatus

from .completion_checkpoint_events import (
    CompletionCheckpointImageEventContext,
    ensure_checkpoint_image_event,
)
from .completion_checkpoint_payloads import (
    COMPLETION_CHECKPOINT_IMAGES_KEY,
    COMPLETION_CHECKPOINT_IMAGE_PENDING,
    CompletionCheckpointCorrupt,
    validated_checkpoint_images,
)
from .tasks.completion_parts.image_storage_runtime import (
    COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY,
    COMPLETION_IMAGE_EVENT_PUBLISHED_KEY,
    CompletionToolImageEventContext,
)


def _completion_request(completion: Any) -> dict[str, Any]:
    request = getattr(completion, "upstream_request", None)
    return dict(request) if isinstance(request, dict) else {}


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str) or not value:
        return None
    if value == "0":
        return 0
    if value[0] == "0" or any(character not in "0123456789" for character in value):
        return None
    return int(value)


def _pinned_image_service(service: Any, image_id: str) -> Any:
    return replace(
        service,
        repository=replace(
            service.repository,
            new_id=lambda: image_id,
        ),
    )


def _ensure_current_completion(
    repository: Any,
    completion: Any,
    context: CompletionCheckpointImageEventContext,
) -> None:
    current_attempt = _nonnegative_int(getattr(completion, "attempt", None))
    current_epoch = _nonnegative_int(
        getattr(completion, "execution_epoch", None)
    )
    if (
        completion is None
        or current_attempt != context.attempt_epoch
        or current_epoch != context.execution_epoch
        or completion.status != CompletionStatus.STREAMING.value
        or getattr(completion, "cancel_requested_at", None) is not None
    ):
        raise repository.superseded_error_type(
            f"completion checkpoint image superseded task={context.task_id} "
            f"execution_epoch={context.execution_epoch} "
            f"attempt_epoch={context.attempt_epoch}"
        )


def _image_payload_from_row(
    repository: Any,
    image: Any,
    *,
    context: CompletionCheckpointImageEventContext,
    revised_prompt: str | None,
) -> dict[str, Any]:
    image_id = str(image.id)
    return {
        "image_id": image_id,
        "from_completion_id": context.task_id,
        "completion_execution_epoch": context.execution_epoch,
        "completion_attempt_epoch": context.attempt_epoch,
        "mime": image.mime,
        "url": repository.public_url(image.storage_key),
        "display_url": f"/api/images/{image_id}/variants/display2048",
        "preview_url": f"/api/images/{image_id}/variants/preview1024",
        "thumb_url": f"/api/images/{image_id}/variants/thumb256",
        "actual_size": f"{image.width}x{image.height}",
        **({"revised_prompt": revised_prompt} if revised_prompt else {}),
    }


async def _adopt_checkpoint_image(
    service: Any,
    session: Any,
    completion: Any,
    *,
    context: CompletionCheckpointImageEventContext,
    image_record: dict[str, Any],
    expected_sha: str,
    reserved_micro: int,
) -> tuple[dict[str, Any], int] | None:
    repository = service.repository
    image_id = str(image_record["image_id"])
    image = await session.get(repository.image_model, image_id)
    if image is None:
        return None
    metadata = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
    key_prefix = (
        f"u/{context.user_id}/completion-tools/{context.task_id}/executions/"
        f"{context.execution_epoch}/attempts/{context.attempt_epoch}/{image_id}/orig."
    )
    if not (
        image.user_id == context.user_id
        and image.sha256 == expected_sha
        and isinstance(image.storage_key, str)
        and image.storage_key.startswith(key_prefix)
        and metadata.get("completion_id") == context.task_id
        and metadata.get("completion_attempt_epoch") == context.attempt_epoch
        and metadata.get("completion_execution_epoch") == context.execution_epoch
    ):
        raise repository.superseded_error_type(
            f"completion checkpoint image identity conflict "
            f"task={context.task_id} image={image_id}"
        )
    persisted_reserved = _nonnegative_int(
        _completion_request(completion).get("tool_image_reserved_micro")
    )
    payload = _image_payload_from_row(
        repository,
        image,
        context=context,
        revised_prompt=image_record.get("revised_prompt"),
    )
    event_id, event_published = await ensure_checkpoint_image_event(
        service,
        session,
        image=image,
        image_payload=payload,
        context=context,
    )
    payload[COMPLETION_IMAGE_EVENT_OUTBOX_ID_KEY] = event_id
    payload[COMPLETION_IMAGE_EVENT_PUBLISHED_KEY] = event_published
    return (
        payload,
        max(0, int(persisted_reserved or 0) - max(0, int(reserved_micro))),
    )


@asynccontextmanager
async def _locked_checkpoint_recovery(
    service: Any,
    context: CompletionCheckpointImageEventContext,
) -> AsyncIterator[tuple[Any, Any]]:
    repository = service.repository
    async with repository.session_factory() as session:
        await repository.acquire_task_lock(session, context.task_id)
        completion = await session.get(
            repository.completion_model,
            context.task_id,
            with_for_update=True,
        )
        _ensure_current_completion(repository, completion, context)
        yield session, completion


async def persist_completion_checkpoint_image(
    service: Any,
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    execution_epoch: int,
    image_record: dict[str, Any],
    reserved_micro: int,
) -> tuple[dict[str, Any], int]:
    image_record = validated_checkpoint_images(
        {COMPLETION_CHECKPOINT_IMAGES_KEY: [image_record]}
    )[0]
    if image_record["state"] != COMPLETION_CHECKPOINT_IMAGE_PENDING:
        raise CompletionCheckpointCorrupt(
            str(
                image_record.get("quarantine_reason")
                or "completion checkpoint image is not pending"
            )
        )
    pinned_service = _pinned_image_service(
        service,
        str(image_record["image_id"]),
    )
    raw_image = pinned_service.codec.decode(str(image_record["image_b64"]))
    expected_sha = pinned_service.codec.sha256(raw_image)
    checkpoint_context = CompletionCheckpointImageEventContext(
        user_id=user_id,
        channel=channel,
        task_id=task_id,
        message_id=message_id,
        attempt=attempt,
        attempt_epoch=attempt_epoch,
        execution_epoch=execution_epoch,
    )
    async with _locked_checkpoint_recovery(
        pinned_service,
        checkpoint_context,
    ) as (session, completion):
        committed = await _adopt_checkpoint_image(
            pinned_service,
            session,
            completion,
            context=checkpoint_context,
            image_record=image_record,
            expected_sha=expected_sha,
            reserved_micro=reserved_micro,
        )
        if committed is not None:
            return committed

    budget_micro = await pinned_service.budget.reserve(
        user_id=user_id,
        task_id=task_id,
        reserved_micro=reserved_micro,
    )
    delivery_context = CompletionToolImageEventContext(
        channel=channel,
        attempt=attempt,
    )
    async with _locked_checkpoint_recovery(
        pinned_service,
        checkpoint_context,
    ) as (session, completion):
        committed = await _adopt_checkpoint_image(
            pinned_service,
            session,
            completion,
            context=checkpoint_context,
            image_record=image_record,
            expected_sha=expected_sha,
            reserved_micro=reserved_micro,
        )
        if committed is not None:
            return committed
        payload = await pinned_service.store_tool_image(
            session=session,
            task_id=task_id,
            attempt_epoch=attempt_epoch,
            execution_epoch=execution_epoch,
            user_id=user_id,
            message_id=message_id,
            raw_image=raw_image,
            revised_prompt=image_record.get("revised_prompt"),
            billing_budget_micro=budget_micro,
            event_context=delivery_context,
            cleanup_created_files_on_failure=False,
        )

    await pinned_service.deliver_tool_image_event(
        redis=redis,
        event_context=delivery_context,
        image_payload=payload,
        task_id=task_id,
    )
    return payload, max(0, int(budget_micro or 0))


__all__ = ["persist_completion_checkpoint_image"]
