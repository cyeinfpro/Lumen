"""Durable completion checkpoints shared by execution and reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable

from lumen_core.constants import (
    CompletionStage,
    CompletionStatus,
    EV_COMP_SUCCEEDED,
    MessageStatus,
    task_channel,
)
from lumen_core.model_entities.conversations import Message
from lumen_core.upstream_billing import (
    mark_upstream_response_received,
)

from .completion_checkpoint_payloads import (
    COMPLETION_CHECKPOINT_IMAGES_KEY,
    COMPLETION_CHECKPOINT_IMAGE_COMMITTED,
    COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY,
    COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY,
    COMPLETION_CHECKPOINT_IMAGE_PENDING,
    COMPLETION_CHECKPOINT_IMAGE_QUARANTINED,
    CompletionCheckpointCorrupt,
    build_completed_checkpoint_images,
    checkpoint_image_quarantine_count,
    checkpoint_images_validation_error,
    mark_checkpoint_image_committed,
    mark_checkpoint_image_event_published,
    mark_checkpoint_image_quarantined,
    validated_checkpoint_images,
)
from .completion_checkpoint_schema import (
    COMPLETION_CHECKPOINT_AT_KEY,
    COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY,
    COMPLETION_CHECKPOINT_EXECUTION_EPOCH_KEY,
    COMPLETION_CHECKPOINT_RESPONSE_ID_KEY,
    COMPLETION_CHECKPOINT_STATE_KEY,
    COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY,
    COMPLETION_CHECKPOINT_USAGE_EXACT_KEY,
    COMPLETION_CHECKPOINT_USAGE_KEY,
    COMPLETION_CHECKPOINT_VERSION,
    COMPLETION_CHECKPOINT_VERSION_KEY,
    COMPLETION_EXECUTION_EPOCH_KEY,
    COMPLETION_USAGE_ATTEMPT_EPOCH_KEY,
    COMPLETION_USAGE_EXECUTION_EPOCH_KEY,
    completed_usage_has_exact_totals,
    parse_completion_checkpoint,
)
from .completion_checkpoint_events import redeliver_checkpoint_image_events
from .completion_checkpoint_image_recovery import (
    persist_completion_checkpoint_image,
)
from .completion_text import completion_text_or_empty

COMPLETION_CHECKPOINT_ARTIFACTS_PENDING = "artifacts_pending"
COMPLETION_CHECKPOINT_ARTIFACTS_COMMITTED = "artifacts_committed"
COMPLETION_CHECKPOINT_BILLING_READY = "billing_ready"
COMPLETION_CHECKPOINT_PARTIAL_CORRUPTION = "partial_corruption"
COMPLETION_CHECKPOINT_QUARANTINED = "quarantined"
COMPLETION_CHECKPOINT_CORRUPT_IMAGE_COUNT_KEY = (
    "completion_checkpoint_corrupt_image_count"
)
_COMPLETION_USAGE_FIELDS = (
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cache_creation_5m_tokens",
    "cache_creation_1h_tokens",
    "reasoning_tokens",
    "image_output_tokens",
)


def _durable_checkpoint_usage(
    raw_usage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(raw_usage, dict):
        return None
    try:
        json.dumps(raw_usage, allow_nan=False)
    except (OverflowError, TypeError, ValueError):
        return None
    return dict(raw_usage)


def _completion_request(completion: Any) -> dict[str, Any]:
    request = getattr(completion, "upstream_request", None)
    return dict(request) if isinstance(request, dict) else {}


def _nonnegative_int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (OverflowError, TypeError, ValueError):
        return None


def completion_execution_epoch(state: Any) -> int:
    try:
        return max(
            0,
            int(
                state.preparation.queue_metadata_payload.get(
                    COMPLETION_EXECUTION_EPOCH_KEY,
                    0,
                )
                or 0
            ),
        )
    except (OverflowError, TypeError, ValueError):
        return 0


def completion_has_trustworthy_persisted_usage(completion: Any) -> bool:
    if completion_has_completed_checkpoint(completion):
        return True
    request = _checkpoint_request(completion) or _completion_request(completion)
    usage_epoch = _nonnegative_int(request.get(COMPLETION_USAGE_EXECUTION_EPOCH_KEY))
    execution_epoch = _nonnegative_int(getattr(completion, "execution_epoch", 0) or 0)
    if usage_epoch is None or execution_epoch is None:
        return False
    if usage_epoch != execution_epoch:
        return False
    return _completion_has_positive_persisted_usage(completion)


def _completion_has_positive_persisted_usage(completion: Any) -> bool:
    for field in _COMPLETION_USAGE_FIELDS:
        try:
            if int(getattr(completion, field, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _checkpoint_request(completion: Any) -> dict[str, Any] | None:
    return parse_completion_checkpoint(completion).request


def _validated_checkpoint_images(
    request: dict[str, Any],
) -> list[dict[str, Any]] | None:
    try:
        return validated_checkpoint_images(request)
    except CompletionCheckpointCorrupt:
        return None


def completion_checkpoint_validation_error(completion: Any) -> str | None:
    parsed = parse_completion_checkpoint(completion)
    if parsed.error is not None:
        return parsed.error
    if parsed.request is None:
        return None
    return checkpoint_images_validation_error(parsed.request)


def completion_checkpoint_pending_images(completion: Any) -> list[dict[str, Any]]:
    request = _checkpoint_request(completion)
    if request is None:
        return []
    images = _validated_checkpoint_images(request)
    if images is None:
        return []
    return [
        image
        for image in images
        if image["state"] == COMPLETION_CHECKPOINT_IMAGE_PENDING
    ]


def completion_checkpoint_committed_payloads(
    completion: Any,
) -> list[dict[str, Any]]:
    request = _checkpoint_request(completion)
    if request is None:
        return []
    images = _validated_checkpoint_images(request)
    if images is None:
        return []
    return [
        dict(image["payload"])
        for image in images
        if image["state"] == COMPLETION_CHECKPOINT_IMAGE_COMMITTED
    ]


def _checkpoint_usage_is_recoverable(
    completion: Any,
    request: dict[str, Any],
) -> bool:
    usage_exact = request.get(COMPLETION_CHECKPOINT_USAGE_EXACT_KEY)
    usage_complete = request.get(COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY)
    checkpoint_state = request.get(COMPLETION_CHECKPOINT_STATE_KEY)
    if usage_exact is True and usage_complete is True:
        return checkpoint_state in {
            COMPLETION_CHECKPOINT_BILLING_READY,
            COMPLETION_CHECKPOINT_PARTIAL_CORRUPTION,
        }
    return bool(
        usage_exact is False
        and usage_complete is False
        and checkpoint_state
        in {
            COMPLETION_CHECKPOINT_ARTIFACTS_COMMITTED,
            COMPLETION_CHECKPOINT_PARTIAL_CORRUPTION,
        }
        and _completion_has_positive_persisted_usage(completion)
    )


def completion_has_completed_checkpoint(completion: Any) -> bool:
    request = _checkpoint_request(completion)
    if request is None:
        return False
    images = _validated_checkpoint_images(request)
    if images is None or _checkpoint_normalization_required(completion):
        return False
    return bool(
        all(
            image["state"]
            in {
                COMPLETION_CHECKPOINT_IMAGE_COMMITTED,
                COMPLETION_CHECKPOINT_IMAGE_QUARANTINED,
            }
            for image in images
        )
        and _checkpoint_usage_is_recoverable(completion, request)
        and bool(completion_text_or_empty(getattr(completion, "text", None)))
    )


def completion_checkpoint_has_no_usable_output(completion: Any) -> bool:
    request = _checkpoint_request(completion)
    if request is None:
        return False
    images = _validated_checkpoint_images(request)
    if images is None or _checkpoint_normalization_required(completion):
        return False
    return bool(
        not any(
            image["state"]
            in {
                COMPLETION_CHECKPOINT_IMAGE_PENDING,
                COMPLETION_CHECKPOINT_IMAGE_COMMITTED,
            }
            for image in images
        )
        and request.get(COMPLETION_CHECKPOINT_STATE_KEY)
        in {
            COMPLETION_CHECKPOINT_ARTIFACTS_COMMITTED,
            COMPLETION_CHECKPOINT_BILLING_READY,
            COMPLETION_CHECKPOINT_PARTIAL_CORRUPTION,
        }
        and not completion_text_or_empty(getattr(completion, "text", None))
    )


async def apply_completed_checkpoint(
    context: Any,
    completion: Any,
) -> tuple[str, str, dict[str, Any]]:
    await context.billing.charge_completion(context.session, completion)
    completion.status = CompletionStatus.SUCCEEDED.value
    completion.progress_stage = CompletionStage.FINALIZING
    completion.finished_at = context.now
    completion.updated_at = context.now
    completion.error_code = None
    completion.error_message = None
    message = await context.session.get(Message, completion.message_id)
    if message is not None and message.status != MessageStatus.CANCELED.value:
        content = dict(message.content or {})
        content["text"] = completion.text
        message.content = content
        message.status = MessageStatus.SUCCEEDED.value
    request = _checkpoint_request(completion) or _completion_request(completion)
    images = completion_checkpoint_committed_payloads(completion)
    return context.stage_event(
        context.session,
        kind="sse",
        payload={
            "user_id": completion.user_id,
            "channel": task_channel(str(completion.id)),
            "event_name": EV_COMP_SUCCEEDED,
            "data": {
                "completion_id": completion.id,
                "message_id": completion.message_id,
                "attempt": int(completion.attempt or 0),
                "attempt_epoch": int(completion.attempt or 0),
                "execution_epoch": int(completion.execution_epoch or 0),
                "text": completion.text,
                "tokens_in": int(completion.tokens_in or 0),
                "tokens_out": int(completion.tokens_out or 0),
                "response_id": request.get(COMPLETION_CHECKPOINT_RESPONSE_ID_KEY),
                "recovered_from": "completed_checkpoint",
                **({"images": images} if images else {}),
            },
        },
    )


def _checkpoint_state(
    images: list[dict[str, Any]],
    *,
    usage_exact: bool,
) -> tuple[str, bool]:
    has_pending = any(
        image.get("state") == COMPLETION_CHECKPOINT_IMAGE_PENDING for image in images
    )
    if any(
        image.get("state") == COMPLETION_CHECKPOINT_IMAGE_QUARANTINED
        for image in images
    ):
        return COMPLETION_CHECKPOINT_PARTIAL_CORRUPTION, bool(
            usage_exact and not has_pending
        )
    if has_pending:
        return COMPLETION_CHECKPOINT_ARTIFACTS_PENDING, False
    if usage_exact:
        return COMPLETION_CHECKPOINT_BILLING_READY, True
    return COMPLETION_CHECKPOINT_ARTIFACTS_COMMITTED, False


def _apply_checkpoint_image_state(
    request: dict[str, Any],
    images: list[dict[str, Any]],
) -> None:
    state, usage_complete = _checkpoint_state(
        images,
        usage_exact=(request.get(COMPLETION_CHECKPOINT_USAGE_EXACT_KEY) is True),
    )
    request[COMPLETION_CHECKPOINT_IMAGES_KEY] = images
    request[COMPLETION_CHECKPOINT_STATE_KEY] = state
    request[COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY] = usage_complete
    request[COMPLETION_CHECKPOINT_CORRUPT_IMAGE_COUNT_KEY] = (
        checkpoint_image_quarantine_count(request)
    )
    if state == COMPLETION_CHECKPOINT_PARTIAL_CORRUPTION:
        request[COMPLETION_CHECKPOINT_VERSION_KEY] = COMPLETION_CHECKPOINT_VERSION


async def record_completion_completed_checkpoint(
    state: Any,
    *,
    final_text: str,
    response_id: str | None,
    raw_usage: dict[str, Any] | None,
    usage_complete: bool,
    usage_values: dict[str, int],
    checkpoint_images: list[dict[str, Any]] | None = None,
) -> None:
    if state.settlement.lease_lost.is_set():
        raise state.ports.retry._LeaseLost("lease lost before completion checkpoint")
    execution_epoch = completion_execution_epoch(state)
    attempt_epoch = max(0, int(state.preparation.attempt_epoch or 0))
    checkpoint_at = datetime.now(timezone.utc).isoformat()
    images = validated_checkpoint_images(
        {
            COMPLETION_CHECKPOINT_IMAGES_KEY: [
                dict(image) for image in checkpoint_images or []
            ]
        }
    )
    durable_text = completion_text_or_empty(final_text)
    if not durable_text and any(
        image["state"] == COMPLETION_CHECKPOINT_IMAGE_COMMITTED for image in images
    ):
        durable_text = "已生成图片。"
    usage_exact = bool(usage_complete and response_id)
    async with state.ports.persistence.SessionLocal() as session:
        completion = (
            await session.execute(
                state.ports.persistence.select(state.ports.persistence.Completion)
                .where(
                    state.ports.persistence.Completion.id == state.request.task_id,
                    state.ports.persistence.Completion.attempt == attempt_epoch,
                    state.ports.persistence.Completion.execution_epoch
                    == execution_epoch,
                    state.ports.persistence.Completion.status
                    == CompletionStatus.STREAMING.value,
                    state.ports.persistence.Completion.cancel_requested_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if completion is None:
            raise state.ports.retry._CompletionEpochSuperseded(
                f"completion checkpoint superseded task={state.request.task_id} "
                f"execution_epoch={execution_epoch} attempt_epoch={attempt_epoch}"
            )
        request = mark_upstream_response_received(
            completion,
            at=checkpoint_at,
            attempt=attempt_epoch,
            execution_epoch=execution_epoch,
        )
        request[COMPLETION_USAGE_EXECUTION_EPOCH_KEY] = execution_epoch
        request[COMPLETION_USAGE_ATTEMPT_EPOCH_KEY] = attempt_epoch
        request[COMPLETION_CHECKPOINT_VERSION_KEY] = COMPLETION_CHECKPOINT_VERSION
        request[COMPLETION_CHECKPOINT_EXECUTION_EPOCH_KEY] = execution_epoch
        request[COMPLETION_CHECKPOINT_ATTEMPT_EPOCH_KEY] = attempt_epoch
        request[COMPLETION_CHECKPOINT_RESPONSE_ID_KEY] = (
            response_id.strip() if isinstance(response_id, str) else None
        )
        request[COMPLETION_CHECKPOINT_USAGE_EXACT_KEY] = usage_exact
        request[COMPLETION_CHECKPOINT_USAGE_KEY] = _durable_checkpoint_usage(raw_usage)
        request[COMPLETION_CHECKPOINT_AT_KEY] = checkpoint_at
        _apply_checkpoint_image_state(request, images)
        completion.upstream_request = request
        completion.text = durable_text
        for field in _COMPLETION_USAGE_FIELDS:
            try:
                value = max(0, int(usage_values.get(field, 0) or 0))
            except (OverflowError, TypeError, ValueError):
                value = 0
            if field in {"tokens_out", "image_output_tokens"}:
                try:
                    value = max(
                        value,
                        max(0, int(getattr(completion, field, 0) or 0)),
                    )
                except (OverflowError, TypeError, ValueError):
                    pass
            setattr(completion, field, value)
        await session.commit()


async def record_completed_event_checkpoint(
    state: Any,
    response: dict[str, Any],
    raw_usage: Any,
    *,
    final_text: str,
    usage_complete_allowed: bool = True,
    checkpoint_images: list[dict[str, Any]] | None = None,
) -> None:
    images = checkpoint_images or []
    await record_completion_completed_checkpoint(
        state,
        final_text=final_text,
        response_id=(
            str(response["id"])
            if isinstance(response.get("id"), str) and response["id"].strip()
            else None
        ),
        raw_usage=raw_usage if isinstance(raw_usage, dict) else None,
        usage_complete=(
            usage_complete_allowed
            and getattr(state.usage, "completed_usage_exact", True)
            and completed_usage_has_exact_totals(raw_usage)
        ),
        usage_values=state.usage.usage_totals.model_values(),
        checkpoint_images=images,
    )


def _checkpoint_normalization_required(completion: Any) -> bool:
    request = _checkpoint_request(completion)
    if request is None:
        return False
    images = _validated_checkpoint_images(request)
    if images is None:
        return False
    normalized = dict(request)
    _apply_checkpoint_image_state(normalized, images)
    raw_request = _completion_request(completion)
    corrupt_count = normalized[COMPLETION_CHECKPOINT_CORRUPT_IMAGE_COUNT_KEY]
    return bool(
        raw_request.get(COMPLETION_CHECKPOINT_IMAGES_KEY) != images
        or raw_request.get(COMPLETION_CHECKPOINT_STATE_KEY)
        != normalized.get(COMPLETION_CHECKPOINT_STATE_KEY)
        or raw_request.get(COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY)
        != normalized.get(COMPLETION_CHECKPOINT_USAGE_COMPLETE_KEY)
        or (
            corrupt_count > 0
            and raw_request.get(COMPLETION_CHECKPOINT_CORRUPT_IMAGE_COUNT_KEY)
            != corrupt_count
        )
        or (
            corrupt_count > 0
            and raw_request.get(COMPLETION_CHECKPOINT_VERSION_KEY)
            != normalized.get(COMPLETION_CHECKPOINT_VERSION_KEY)
        )
        or "completion_checkpoint" in raw_request
    )


def completion_checkpoint_requires_recovery(completion: Any) -> bool:
    return bool(
        completion_checkpoint_pending_images(completion)
        or _completion_checkpoint_unpublished_image_events(completion)
        or _checkpoint_normalization_required(completion)
    )


def _completion_checkpoint_unpublished_image_events(
    completion: Any,
) -> list[dict[str, Any]]:
    request = _checkpoint_request(completion)
    if request is None:
        return []
    images = _validated_checkpoint_images(request) or []
    return [
        image
        for image in images
        if image.get("state") == COMPLETION_CHECKPOINT_IMAGE_COMMITTED
        and isinstance(
            image.get(COMPLETION_CHECKPOINT_IMAGE_EVENT_OUTBOX_ID_KEY),
            str,
        )
        and not image.get(COMPLETION_CHECKPOINT_IMAGE_EVENT_PUBLISHED_KEY)
    ]


async def persist_completed_event_checkpoint(
    state: Any,
    response: dict[str, Any],
    raw_usage: Any,
    *,
    final_text: str,
    image_events: list[dict[str, Any]],
) -> None:
    images = build_completed_checkpoint_images(state, image_events)
    await record_completed_event_checkpoint(
        state,
        response,
        raw_usage,
        final_text=final_text,
        checkpoint_images=images,
    )
    pending = [
        image
        for image in images
        if image["state"] == COMPLETION_CHECKPOINT_IMAGE_PENDING
    ]
    for image in pending:
        try:
            payload, budget_micro = await persist_completion_checkpoint_image(
                state.ports.tools.tool_image_service,
                redis=state.request.redis,
                user_id=state.preparation.user_id,
                channel=state.request.channel,
                task_id=state.request.task_id,
                message_id=state.preparation.message_id,
                attempt=state.preparation.attempt,
                attempt_epoch=state.preparation.attempt_epoch,
                execution_epoch=completion_execution_epoch(state),
                image_record=image,
                reserved_micro=state.streaming.reserved_tool_image_budget_micro,
            )
        except CompletionCheckpointCorrupt as exc:
            images = mark_checkpoint_image_quarantined(
                images,
                image_id=str(image["image_id"]),
                reason=str(exc),
            )
            await record_completed_event_checkpoint(
                state,
                response,
                raw_usage,
                final_text=final_text,
                checkpoint_images=images,
            )
            continue
        images = mark_checkpoint_image_committed(
            images,
            image_id=str(image["image_id"]),
            payload=payload,
            budget_micro=budget_micro,
        )
        state.streaming.tool_images.append(payload)
        state.streaming.stored_image_call_ids.add(str(image["dedupe_key"]))
        state.streaming.reserved_tool_image_budget_micro += budget_micro
        await record_completed_event_checkpoint(
            state,
            response,
            raw_usage,
            final_text=final_text,
            checkpoint_images=images,
        )


async def record_checkpoint_image_committed(
    repository: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    execution_epoch: int,
    image_id: str,
    payload: dict[str, Any],
    budget_micro: int,
) -> dict[str, Any]:
    return await _update_checkpoint_images(
        repository,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        execution_epoch=execution_epoch,
        update_images=lambda images: mark_checkpoint_image_committed(
            images,
            image_id=image_id,
            payload=payload,
            budget_micro=budget_micro,
        ),
    )


async def record_checkpoint_image_quarantined(
    repository: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    execution_epoch: int,
    image_id: str,
    reason: str,
) -> dict[str, Any]:
    return await _update_checkpoint_images(
        repository,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        execution_epoch=execution_epoch,
        update_images=lambda images: mark_checkpoint_image_quarantined(
            images,
            image_id=image_id,
            reason=reason,
        ),
    )


async def record_checkpoint_image_event_published(
    repository: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    execution_epoch: int,
    image_id: str,
) -> dict[str, Any]:
    return await _update_checkpoint_images(
        repository,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        execution_epoch=execution_epoch,
        update_images=lambda images: mark_checkpoint_image_event_published(
            images,
            image_id=image_id,
        ),
    )


async def _update_checkpoint_images(
    repository: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    execution_epoch: int,
    update_images: Callable[
        [list[dict[str, Any]]],
        list[dict[str, Any]],
    ],
) -> dict[str, Any]:
    async with repository.session_factory() as session:
        await repository.acquire_task_lock(session, task_id)
        completion = await session.get(
            repository.completion_model,
            task_id,
            with_for_update=True,
        )
        if (
            completion is None
            or completion.attempt != attempt_epoch
            or int(getattr(completion, "execution_epoch", 0) or 0) != execution_epoch
            or completion.status != CompletionStatus.STREAMING.value
            or getattr(completion, "cancel_requested_at", None) is not None
        ):
            raise repository.superseded_error_type(
                f"completion checkpoint image commit superseded task={task_id} "
                f"execution_epoch={execution_epoch} attempt_epoch={attempt_epoch}"
            )
        validation_error = completion_checkpoint_validation_error(completion)
        if validation_error is not None:
            raise CompletionCheckpointCorrupt(validation_error)
        request = _checkpoint_request(completion)
        if request is None:
            raise repository.superseded_error_type(
                f"completion checkpoint missing task={task_id} "
                f"execution_epoch={execution_epoch} attempt_epoch={attempt_epoch}"
            )
        images = _validated_checkpoint_images(request)
        if images is None:
            raise CompletionCheckpointCorrupt(
                f"completion checkpoint images invalid task={task_id}"
            )
        images = update_images(images)
        _apply_checkpoint_image_state(request, images)
        if not completion_text_or_empty(getattr(completion, "text", None)):
            completion.text = (
                "已生成图片。"
                if any(
                    image["state"] == COMPLETION_CHECKPOINT_IMAGE_COMMITTED
                    for image in images
                )
                else ""
            )
        request[COMPLETION_CHECKPOINT_AT_KEY] = datetime.now(timezone.utc).isoformat()
        completion.upstream_request = request
        await session.commit()
        return request


async def _record_checkpoint_images_normalized(
    repository: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    execution_epoch: int,
) -> dict[str, Any]:
    return await _update_checkpoint_images(
        repository,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        execution_epoch=execution_epoch,
        update_images=lambda images: images,
    )


async def recover_completion_checkpoint_images(
    completion: Any,
    *,
    redis: Any,
    channel: str,
    tool_image_service: Any,
) -> bool:
    validation_error = completion_checkpoint_validation_error(completion)
    if validation_error is not None:
        raise CompletionCheckpointCorrupt(validation_error)
    if not completion_checkpoint_requires_recovery(completion):
        return False
    repository = tool_image_service.repository
    if _checkpoint_normalization_required(completion):
        completion.upstream_request = await _record_checkpoint_images_normalized(
            repository,
            task_id=str(completion.id),
            attempt_epoch=int(completion.attempt or 0),
            execution_epoch=int(completion.execution_epoch or 0),
        )
    pending = completion_checkpoint_pending_images(completion)
    request = _checkpoint_request(completion) or _completion_request(completion)
    reserved_micro = int(
        _nonnegative_int(request.get("tool_image_reserved_micro")) or 0
    )
    for image in pending:
        try:
            payload, budget_micro = await persist_completion_checkpoint_image(
                tool_image_service,
                redis=redis,
                user_id=completion.user_id,
                channel=channel,
                task_id=str(completion.id),
                message_id=completion.message_id,
                attempt=int(completion.attempt or 0),
                attempt_epoch=int(completion.attempt or 0),
                execution_epoch=int(completion.execution_epoch or 0),
                image_record=image,
                reserved_micro=reserved_micro,
            )
        except CompletionCheckpointCorrupt as exc:
            request = await record_checkpoint_image_quarantined(
                repository,
                task_id=str(completion.id),
                attempt_epoch=int(completion.attempt or 0),
                execution_epoch=int(completion.execution_epoch or 0),
                image_id=str(image["image_id"]),
                reason=str(exc),
            )
            completion.upstream_request = request
            continue
        request = await record_checkpoint_image_committed(
            repository,
            task_id=str(completion.id),
            attempt_epoch=int(completion.attempt or 0),
            execution_epoch=int(completion.execution_epoch or 0),
            image_id=str(image["image_id"]),
            payload=payload,
            budget_micro=budget_micro,
        )
        completion.upstream_request = request
        reserved_micro = int(
            _nonnegative_int(request.get("tool_image_reserved_micro")) or 0
        )
    await redeliver_checkpoint_image_events(
        completion,
        redis=redis,
        tool_image_service=tool_image_service,
        images=_completion_checkpoint_unpublished_image_events(completion),
        mark_published=record_checkpoint_image_event_published,
    )
    return True
