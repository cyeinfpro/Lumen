"""Typed generation-task construction helpers for message submission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import GenerationStage, GenerationStatus, Intent
from lumen_core.models import Generation, Message, new_uuid7
from lumen_core.schemas import ImageParamsIn
from lumen_core.sizing import ResolvedSize


AsyncCallable = Callable[..., Awaitable[Any]]


class GenerationCredentialPin(Protocol):
    credential_id: str
    supplier_id: str
    default_chat_model: str
    default_image_model: str | None


@dataclass(frozen=True)
class GenerationTaskCommand:
    db: AsyncSession
    user_id: str
    account_mode: str
    assistant_msg: Message
    intent: Intent
    stored_key: str
    attachment_ids: list[str]
    image_params: ImageParamsIn
    text: str
    resolved_size: ResolvedSize
    prompt_suffix: str
    default_image_output_format: str
    mask_image_id: str | None
    credential_pin: GenerationCredentialPin | None
    request_metadata: dict[str, Any] | None


@dataclass(frozen=True)
class GenerationTaskServices:
    billing_enabled: AsyncCallable
    billing_allow_negative: AsyncCallable
    billing_image_thresholds: AsyncCallable
    user_rate_multiplier: AsyncCallable
    apply_rate_multiplier: Callable[[int, int], int]
    requested_image_billing_tier: Callable[[ImageParamsIn], str | None]
    write_audit: AsyncCallable
    child_idempotency_key: Callable[[str, int], str]
    defer_seconds: Callable[[int], int]
    payload_context: Callable[[dict[str, Any] | None], dict[str, Any]]
    billing_http_error: Callable[[billing_core.BillingError], HTTPException]


@dataclass(frozen=True)
class GenerationBilling:
    enabled: bool
    estimated_micro: int
    estimated_tier: str
    size_px: int
    allow_negative: bool


@dataclass(frozen=True)
class GenerationBatch:
    requested_count: int
    action: str
    primary_image_id: str | None
    prompt: str
    base_upstream_request: dict[str, Any]
    request_trace_id: object


async def prepare_generation_billing(
    command: GenerationTaskCommand,
    services: GenerationTaskServices,
    *,
    base_upstream_request: dict[str, Any],
    size_px: int,
    billing_tier: str | None,
) -> GenerationBilling:
    db = command.db
    billing_is_enabled = (
        command.account_mode == "wallet" and await services.billing_enabled(db)
    )
    billing_thresholds = (
        await services.billing_image_thresholds(db) if billing_is_enabled else {}
    )
    if not billing_is_enabled:
        base_estimated_micro, estimated_tier = (0, "free")
    elif billing_tier is not None:
        (
            base_estimated_micro,
            estimated_tier,
        ) = await billing_core.estimate_image_cost_for_tier(
            db,
            tier=billing_tier,
            n=1,
        )
    else:
        base_estimated_micro, estimated_tier = await billing_core.estimate_image_cost(
            db,
            size_px=size_px,
            n=1,
            thresholds=billing_thresholds or None,
        )
    estimated_micro = 0
    if billing_is_enabled:
        rate_multiplier_x10000 = int(
            await services.user_rate_multiplier(db, command.user_id)
        )
        estimated_micro = services.apply_rate_multiplier(
            base_estimated_micro,
            rate_multiplier_x10000,
        )
        base_upstream_request["billing_pricing_snapshot"] = {
            "kind": "image",
            "tier": estimated_tier,
            "unit_price_micro": int(base_estimated_micro),
            "captured_size_px": int(size_px),
        }
        base_upstream_request["billing_rate_multiplier_x10000"] = rate_multiplier_x10000
    allow_negative = (
        await services.billing_allow_negative(db)
        if billing_is_enabled and estimated_micro > 0
        else False
    )
    return GenerationBilling(
        enabled=billing_is_enabled,
        estimated_micro=estimated_micro,
        estimated_tier=estimated_tier,
        size_px=size_px,
        allow_negative=allow_negative,
    )


def generation_upstream_request(
    batch: GenerationBatch,
    image_index: int,
) -> dict[str, Any]:
    request = dict(batch.base_upstream_request)
    request["n"] = 1
    if batch.requested_count > 1:
        request["batch_task_index"] = image_index
        request["batch_task_count"] = batch.requested_count
        request["requested_image_count"] = batch.requested_count
        if isinstance(batch.request_trace_id, str) and batch.request_trace_id:
            request["request_trace_id"] = batch.request_trace_id
        request["trace_id"] = f"gen_{new_uuid7()}"
    else:
        request.setdefault("trace_id", f"gen_{new_uuid7()}")
    return request


def new_generation(
    command: GenerationTaskCommand,
    services: GenerationTaskServices,
    batch: GenerationBatch,
    *,
    image_index: int,
    upstream_request: dict[str, Any],
) -> Generation:
    credential_pin = command.credential_pin
    return Generation(
        message_id=command.assistant_msg.id,
        user_id=command.user_id,
        action=batch.action,
        prompt=batch.prompt,
        size_requested=command.resolved_size.size,
        aspect_ratio=command.image_params.aspect_ratio,
        input_image_ids=command.attachment_ids,
        primary_input_image_id=batch.primary_image_id,
        mask_image_id=(
            command.mask_image_id if command.intent == Intent.IMAGE_TO_IMAGE else None
        ),
        status=GenerationStatus.QUEUED.value,
        progress_stage=GenerationStage.QUEUED.value,
        attempt=0,
        idempotency_key=services.child_idempotency_key(
            command.stored_key,
            image_index,
        ),
        upstream_request=upstream_request,
        user_api_credential_id=(
            credential_pin.credential_id if credential_pin else None
        ),
        upstream_supplier_id=credential_pin.supplier_id if credential_pin else None,
    )


async def hold_generation_billing(
    command: GenerationTaskCommand,
    services: GenerationTaskServices,
    batch: GenerationBatch,
    billing: GenerationBilling,
    generation: Generation,
    *,
    image_index: int,
) -> None:
    if not billing.enabled or billing.estimated_micro <= 0:
        return
    try:
        tx = await billing_core.hold(
            command.db,
            command.user_id,
            billing.estimated_micro,
            ref_type="generation",
            ref_id=generation.id,
            idempotency_key=f"hold:{generation.id}",
            allow_negative=billing.allow_negative,
            meta={
                "tier": billing.estimated_tier,
                "size_requested": command.resolved_size.size,
                "pixels_estimated": billing.size_px,
                "image_count": 1,
                "batch_task_index": image_index,
                "batch_task_count": batch.requested_count,
                "pricing_snapshot": generation.upstream_request.get(
                    "billing_pricing_snapshot"
                ),
            },
        )
    except billing_core.BillingError as exc:
        raise services.billing_http_error(exc) from exc
    if tx is None:
        return
    await services.write_audit(
        command.db,
        event_type="wallet.hold.image",
        user_id=command.user_id,
        details={
            "generation_id": generation.id,
            "amount_micro": billing.estimated_micro,
            "tier": billing.estimated_tier,
            "image_count": 1,
            "batch_task_index": image_index,
            "batch_task_count": batch.requested_count,
            "balance_after": tx.balance_after,
            "hold_after": tx.hold_after,
        },
        autocommit=False,
    )


def generation_outbox_payload(
    command: GenerationTaskCommand,
    services: GenerationTaskServices,
    generation: Generation,
    *,
    image_index: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": generation.id,
        "user_id": command.user_id,
        "kind": "generation",
    }
    defer_s = services.defer_seconds(image_index)
    if defer_s > 0:
        payload["defer_s"] = defer_s
    payload.update(services.payload_context(generation.upstream_request))
    return payload
