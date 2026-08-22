"""Public existing-message Generation batch orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent
from lumen_core.models import Message
from lumen_core.schema_models.messaging import ImageParamsIn
from lumen_core.sizing import resolve_size

from ..audit import write_audit
from ..task_billing import (
    apply_rate_multiplier_micro,
    requested_image_billing_tier,
    user_rate_multiplier_x10000,
)
from .message_generation_tasks import GenerationTaskCommand, GenerationTaskServices
from .message_submission_billing import (
    billing_allow_negative,
    billing_enabled,
    billing_http_error,
    billing_image_thresholds,
    generation_child_idempotency_key,
    http_error,
    image_multi_generation_defer_s,
)
from .message_submission_prompting import TaskCredentialPin, resolve_task_credential_pin


DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"


@dataclass(frozen=True, slots=True)
class ExistingMessageGenerationCommand:
    db: AsyncSession
    user_id: str
    account_mode: str
    assistant_msg: Message
    intent: Intent
    idempotency_key: str
    image_params: ImageParamsIn
    attachment_ids: list[str]
    text: str
    request_metadata: dict[str, Any] | None = None
    default_image_output_format: str = DEFAULT_IMAGE_OUTPUT_FORMAT
    mask_image_id: str | None = None
    credential_pin: TaskCredentialPin | None = None
    credential_pin_resolved: bool = False


@dataclass(frozen=True, slots=True)
class ExistingMessageGenerationServices:
    create_generation_tasks: Callable[
        ..., Awaitable[tuple[list[str], list[dict[str, Any]]]]
    ]
    create_outbox_rows: Callable[..., Awaitable[list[Any]]]
    payload_context: Callable[[dict[str, Any] | None], dict[str, Any]]
    result_factory: Callable[..., Any]


async def execute_generation_batch_for_message(
    command: ExistingMessageGenerationCommand,
    services: ExistingMessageGenerationServices,
) -> Any:
    """Create an image batch without manufacturing another assistant message."""
    if command.intent not in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE):
        raise http_error(
            "invalid_intent", "generation batch requires an image intent", 422
        )
    if command.intent == Intent.IMAGE_TO_IMAGE and not command.attachment_ids:
        raise http_error(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )
    try:
        resolved_size = resolve_size(
            aspect=command.image_params.aspect_ratio,
            mode=command.image_params.size_mode,
            fixed=command.image_params.fixed_size,
        )
    except Exception as exc:  # noqa: BLE001
        raise http_error("invalid_size", f"size resolve failed: {exc}", 422) from exc
    credential_pin = command.credential_pin
    if not command.credential_pin_resolved:
        credential_pin = await resolve_task_credential_pin(
            command.db, command.user_id, "image", command.account_mode
        )

    generation_ids, outbox_payloads = await services.create_generation_tasks(
        GenerationTaskCommand(
            db=command.db,
            user_id=command.user_id,
            account_mode=command.account_mode,
            assistant_msg=command.assistant_msg,
            intent=command.intent,
            stored_key=command.idempotency_key,
            attachment_ids=command.attachment_ids,
            image_params=command.image_params,
            text=command.text,
            resolved_size=resolved_size,
            prompt_suffix=resolved_size.prompt_suffix,
            default_image_output_format=command.default_image_output_format,
            mask_image_id=command.mask_image_id,
            credential_pin=credential_pin,
            request_metadata=command.request_metadata,
        ),
        GenerationTaskServices(
            billing_enabled=billing_enabled,
            billing_allow_negative=billing_allow_negative,
            billing_image_thresholds=billing_image_thresholds,
            user_rate_multiplier=user_rate_multiplier_x10000,
            apply_rate_multiplier=apply_rate_multiplier_micro,
            requested_image_billing_tier=requested_image_billing_tier,
            write_audit=write_audit,
            child_idempotency_key=generation_child_idempotency_key,
            defer_seconds=image_multi_generation_defer_s,
            payload_context=services.payload_context,
            billing_http_error=billing_http_error,
        ),
    )
    outbox_rows = await services.create_outbox_rows(command.db, outbox_payloads)
    return services.result_factory(
        assistant_msg=command.assistant_msg,
        completion_id=None,
        generation_ids=generation_ids,
        outbox_payloads=outbox_payloads,
        outbox_rows=outbox_rows,
    )


__all__ = [
    "ExistingMessageGenerationCommand",
    "ExistingMessageGenerationServices",
    "execute_generation_batch_for_message",
]
