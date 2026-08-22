"""Assistant task construction and post-commit publication services."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_IMAGE_RESPONSES_MODEL,
    EV_COMP_QUEUED,
    EV_CONV_MSG_APPENDED,
    EV_GEN_QUEUED,
    CompletionStage,
    CompletionStatus,
    GenerationAction,
    Intent,
    MessageStatus,
    Role,
    conv_channel,
    task_channel,
)
from lumen_core.models import Completion, Conversation, Message, OutboxEvent
from lumen_core.queue_metadata import generation_queue_metadata
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    MessageAttachmentIn,
    PostMessageIn,
)
from lumen_core.sizing import ResolvedSize, resolve_size

from ..arq_pool import get_arq_pool
from ..audit import write_audit
from ..runtime_settings import get_setting
from ..sse_publish import publish_sse_event, publish_sse_events
from ..task_billing import (
    ChatWalletPreflight,
    apply_rate_multiplier_micro,
    requested_image_billing_tier,
    resolve_image_render_quality,
    user_rate_multiplier_x10000,
)
from .message_generation_tasks import (
    GenerationBatch,
    GenerationTaskCommand,
    GenerationTaskServices,
    generation_outbox_payload,
    generation_upstream_request,
    hold_generation_billing,
    new_generation,
    prepare_generation_billing,
)
from .message_generation_batch import (
    ExistingMessageGenerationCommand,
    ExistingMessageGenerationServices,
    execute_generation_batch_for_message,
)

from .message_submission_billing import (
    billing_http_error as _billing_http_error,
    billing_allow_negative,
    billing_enabled,
    billing_image_thresholds,
    billing_setting_raw,
    chat_max_tool_invocations,
    chat_tool_budget_setting_micro,
    ensure_chat_wallet_preflight,
    generation_child_idempotency_key,
    idempotency_lock_key,
    idempotency_lookup_keys,
    image_multi_generation_defer_s,
    http_error as _http,
    stored_idempotency_key,
)
from .message_submission_prompting import (
    TaskCredentialPin,
    build_structured_system_prompt,
    resolve_system_prompt_for_message,
    resolve_task_credential_pin,
    sanitize_system_prompt_source as _sanitize_system_prompt_source,
)


logger = logging.getLogger(__name__)

IMAGE_OUTPUT_FORMAT_VALUES = frozenset({"png", "jpeg", "webp"})
DEFAULT_IMAGE_OUTPUT_FORMAT = "jpeg"
GENERATION_FAST_DEFAULT_KEY = "generation.fast_default"

_VECTOR_STORE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_IMAGE_BACKGROUND_VALUES = frozenset({"auto", "opaque", "transparent"})
_IMAGE_MODERATION_VALUES = frozenset({"auto", "low"})
_TRANSPARENT_BACKGROUND_RE = re.compile(
    r"透明(?:底|背景|底色)|去背|抠图|免抠|无背景|"
    r"transparent\s+(?:background|bg)|background\s+transparent|"
    r"cutout|isolated\s+subject|"
    r"(?<!\w)(?:no|without)\s+(?:a\s+)?background\b",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_NEGATIVE_RE = re.compile(
    r"不(?:要|需要|用)?透明(?:底|背景|底色)?|非透明|"
    r"不要(?:去背|抠图|免抠|无背景|移除背景|去掉背景)|"
    r"保留背景|不要(?:删除|移除|去掉).{0,6}背景|"
    r"opaque\s+background|no\s+transparent\s+(?:background|bg)",
    re.IGNORECASE,
)
_TRANSPARENT_BACKGROUND_NEGATIVE_CONTEXT_RE = re.compile(
    r"(?<!\w)(?:no|without)\s+(?:a\s+)?background\s+"
    r"(?:blur|bokeh|noise|characters?|people|persons?|subjects?|"
    r"objects?|details?|change|changes|music|story|context|scene|"
    r"scenery|lighting|shadows?|text|pattern|elements?)\b",
    re.IGNORECASE,
)

AsyncCallable = Callable[..., Awaitable[Any]]


@dataclass
class AssistantTaskResult:
    assistant_msg: Message
    completion_id: str | None
    generation_ids: list[str]
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[OutboxEvent]


@dataclass(frozen=True)
class CompletionTaskCommand:
    user_id: str
    user_email: str | None
    account_mode: str
    assistant_msg: Message
    intent: Intent
    stored_key: str
    attachment_ids: list[str]
    chat_params: ChatParamsIn
    system_prompt: str | None
    request_metadata: dict[str, Any] | None
    credential_pin: TaskCredentialPin | None
    chat_wallet_preflight_done: bool
    chat_wallet_preflight: ChatWalletPreflight | None


@dataclass(frozen=True)
class CompletionTaskServices:
    ensure_chat_wallet_preflight: AsyncCallable
    billing_allow_negative: AsyncCallable
    write_audit: AsyncCallable


async def resolve_fast_default(
    db: AsyncSession,
    *,
    get_spec_fn: Callable[[str], Any] = get_spec,
    get_setting_fn: AsyncCallable = get_setting,
) -> bool:
    spec = get_spec_fn(GENERATION_FAST_DEFAULT_KEY)
    if spec is None:
        return True
    raw = await get_setting_fn(db, spec)
    if raw in {"0", "1"}:
        return raw == "1"
    return True


def chat_params_with_fast_default(
    chat_params: ChatParamsIn,
    fast_default: bool,
) -> ChatParamsIn:
    if chat_params.fast is not None:
        return chat_params
    return chat_params.model_copy(update={"fast": fast_default})


def wants_transparent_background(prompt: str | None) -> bool:
    if not prompt:
        return False
    if _TRANSPARENT_BACKGROUND_NEGATIVE_RE.search(prompt):
        return False
    if _TRANSPARENT_BACKGROUND_NEGATIVE_CONTEXT_RE.search(prompt):
        return False
    return bool(_TRANSPARENT_BACKGROUND_RE.search(prompt))


def _resolve_image_background(
    image_params: ImageParamsIn,
    prompt: str | None,
) -> str:
    background = (
        image_params.background
        if image_params.background in _IMAGE_BACKGROUND_VALUES
        else "auto"
    )
    if background == "auto" and wants_transparent_background(prompt):
        return "transparent"
    return background


def image_upstream_request(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
    *,
    prompt: str | None = None,
    default_output_format: str = DEFAULT_IMAGE_OUTPUT_FORMAT,
) -> dict[str, Any]:
    render_quality = resolve_image_render_quality(image_params, resolved_size)
    background = _resolve_image_background(image_params, prompt)
    output_format_is_explicit = image_params.output_format in IMAGE_OUTPUT_FORMAT_VALUES
    output_format = (
        image_params.output_format
        if output_format_is_explicit
        else default_output_format
        if default_output_format in IMAGE_OUTPUT_FORMAT_VALUES
        else DEFAULT_IMAGE_OUTPUT_FORMAT
    )
    output_format_source = "request" if output_format_is_explicit else "system_default"
    if background == "transparent" and output_format == "jpeg":
        output_format = "png"
        output_format_source = "transparent_background"
    upstream_request: dict[str, Any] = {
        "responses_model": DEFAULT_IMAGE_RESPONSES_MODEL,
        "render_quality": render_quality,
        "output_format": output_format,
        "output_format_source": output_format_source,
        "background": background,
        "moderation": (
            image_params.moderation
            if image_params.moderation in _IMAGE_MODERATION_VALUES
            else "low"
        ),
    }
    billing_tier = requested_image_billing_tier(image_params)
    if billing_tier is not None:
        upstream_request["billing_tier"] = billing_tier
        upstream_request["billing_tier_source"] = "request_quality"
    if (
        output_format in {"jpeg", "webp"}
        and image_params.output_compression is not None
    ):
        upstream_request["output_compression"] = image_params.output_compression
    return upstream_request


def _chat_param_vector_store_ids(chat_params: ChatParamsIn) -> list[str]:
    vector_store_ids: list[str] = []
    seen: set[str] = set()
    for raw in chat_params.vector_store_ids:
        value = raw.strip()
        if not value:
            continue
        if not _VECTOR_STORE_ID_RE.fullmatch(value):
            raise _http(
                "invalid_vector_store_id",
                "invalid vector_store_ids entry",
                422,
            )
        if value not in seen:
            seen.add(value)
            vector_store_ids.append(value)
    return vector_store_ids


def _chat_upstream_request(chat_params: ChatParamsIn) -> dict[str, Any] | None:
    req: dict[str, Any] = {}
    if chat_params.web_search:
        req["web_search"] = True
    if chat_params.file_search:
        vector_store_ids = _chat_param_vector_store_ids(chat_params)
        req["file_search"] = True
        if vector_store_ids:
            req["vector_store_ids"] = vector_store_ids
    if chat_params.code_interpreter:
        req["code_interpreter"] = True
    if chat_params.image_generation:
        req["image_generation"] = True
    return req or None


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _default_attachment_role(intent: Intent) -> str:
    return "ask_target" if intent == Intent.VISION_QA else "reference"


def _message_attachment_roles(
    body: PostMessageIn,
    *,
    attachment_ids: list[str],
    intent: Intent,
) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    items: list[MessageAttachmentIn | str]
    if body.attachments:
        items = list(body.attachments)
    else:
        items = list(attachment_ids)
    default_role = _default_attachment_role(intent)
    for item in items:
        if isinstance(item, str):
            roles.append({"image_id": item, "role": default_role})
            continue
        role: dict[str, Any] = {
            "image_id": item.image_id,
            "role": item.role,
        }
        if item.label:
            role["label"] = item.label
        if item.weight is not None:
            role["weight"] = item.weight
        roles.append(role)
    return roles


def message_request_metadata(
    body: PostMessageIn,
    *,
    attachment_ids: list[str],
    mask_image_id: str | None,
    intent: Intent,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    source = _clean_optional_text(body.source)
    action_source = _clean_optional_text(body.action_source)
    trace_id = _clean_optional_text(body.trace_id)
    attachment_roles = _message_attachment_roles(
        body,
        attachment_ids=attachment_ids,
        intent=intent,
    )
    if source:
        metadata["source"] = source
    if action_source:
        metadata["action_source"] = action_source
    if trace_id:
        metadata["trace_id"] = trace_id
    if attachment_roles:
        metadata["attachment_roles"] = attachment_roles
    if attachment_ids:
        metadata["input_images"] = [dict(item) for item in attachment_roles]
        metadata["primary_input_image_id"] = attachment_ids[0]
        metadata["source_image_id"] = attachment_ids[0]
    if mask_image_id:
        metadata["mask_image_id"] = mask_image_id
        input_images = list(metadata.get("input_images") or [])
        input_images.append({"image_id": mask_image_id, "role": "mask"})
        metadata["input_images"] = input_images
    return metadata


def _merge_request_metadata(
    upstream_request: dict[str, Any] | None,
    request_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(upstream_request or {})
    for key, value in (request_metadata or {}).items():
        out.setdefault(key, value)
    return out


def _task_payload_context(
    upstream_request: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(upstream_request, dict):
        return {}
    context: dict[str, Any] = {}
    for key in ("trace_id", "source", "action_source"):
        value = upstream_request.get(key)
        if isinstance(value, str) and value:
            context[key] = value
    input_images = upstream_request.get("input_images")
    if isinstance(input_images, list):
        context["input_images"] = input_images
    return context


def _image_queue_metadata(
    image_params: ImageParamsIn,
    resolved_size: ResolvedSize,
    *,
    action: str | None,
    mask_image_id: str | None,
    size_px: int,
    billing_tier: str | None,
) -> dict[str, Any]:
    _ = image_params, billing_tier
    safe_action = action or (
        GenerationAction.EDIT.value
        if mask_image_id
        else GenerationAction.GENERATE.value
    )
    return generation_queue_metadata(
        upstream_request=None,
        action=safe_action,
        size_requested=resolved_size.size,
        mask_image_id=mask_image_id,
        upstream_pixels=size_px,
    )


async def ensure_file_search_configured(
    db: AsyncSession,
    chat_params: ChatParamsIn,
    *,
    get_spec_fn: Callable[[str], Any] = get_spec,
    get_setting_fn: AsyncCallable = get_setting,
) -> None:
    if not chat_params.file_search:
        return
    if _chat_param_vector_store_ids(chat_params):
        return
    spec = get_spec_fn("chat.file_search_vector_store_ids")
    raw = await get_setting_fn(db, spec) if spec is not None else None
    if raw and any(part.strip() for part in raw.split(",")):
        return
    raise _http(
        "FILE_SEARCH_NOT_CONFIGURED",
        "file_search requires vector_store_ids or a configured default vector store",
        400,
    )


def _select_chat_task_model(
    credential_pin: TaskCredentialPin | None,
    chat_params: ChatParamsIn,
) -> str:
    return (
        credential_pin.fast_chat_model
        if credential_pin and chat_params.fast and credential_pin.fast_chat_model
        else credential_pin.default_chat_model
        if credential_pin
        else DEFAULT_CHAT_MODEL
    )


async def _create_completion_task(
    db: AsyncSession,
    command: CompletionTaskCommand,
    services: CompletionTaskServices,
) -> tuple[str, list[dict[str, Any]]]:
    user_id = command.user_id
    chat_params = command.chat_params
    credential_pin = command.credential_pin
    chat_wallet_preflight = command.chat_wallet_preflight
    task_chat_model = _select_chat_task_model(credential_pin, chat_params)
    if not command.chat_wallet_preflight_done:
        chat_wallet_preflight = await services.ensure_chat_wallet_preflight(
            db,
            user_id=user_id,
            user_email=command.user_email,
            account_mode=command.account_mode,
            model=task_chat_model,
            chat_params=chat_params,
        )
    comp_upstream_request = _merge_request_metadata(
        _chat_upstream_request(chat_params),
        command.request_metadata,
    )
    if chat_wallet_preflight is not None:
        comp_upstream_request.update(chat_wallet_preflight.upstream_metadata())
    comp = Completion(
        message_id=command.assistant_msg.id,
        user_id=user_id,
        model=task_chat_model,
        input_image_ids=(
            command.attachment_ids if command.intent == Intent.VISION_QA else []
        ),
        system_prompt=command.system_prompt,
        text="",
        status=CompletionStatus.QUEUED.value,
        progress_stage=CompletionStage.QUEUED.value,
        attempt=0,
        idempotency_key=command.stored_key,
        upstream_request=comp_upstream_request or None,
        user_api_credential_id=(
            credential_pin.credential_id if credential_pin else None
        ),
        upstream_supplier_id=credential_pin.supplier_id if credential_pin else None,
    )
    db.add(comp)
    await db.flush()
    if chat_wallet_preflight is not None and chat_wallet_preflight.preauth_micro > 0:
        try:
            tx = await billing_core.hold(
                db,
                user_id,
                chat_wallet_preflight.preauth_micro,
                ref_type="completion",
                ref_id=comp.id,
                idempotency_key=f"hold:{comp.id}",
                allow_negative=await services.billing_allow_negative(db),
                meta=chat_wallet_preflight.hold_metadata(),
            )
        except billing_core.BillingError as exc:
            raise _billing_http_error(exc) from exc
        if tx is not None:
            await services.write_audit(
                db,
                event_type="wallet.hold.chat",
                user_id=user_id,
                details={
                    "completion_id": comp.id,
                    "amount_micro": chat_wallet_preflight.preauth_micro,
                    **chat_wallet_preflight.audit_metadata(),
                    "balance_after": tx.balance_after,
                    "hold_after": tx.hold_after,
                },
                autocommit=False,
            )
    completion_payload: dict[str, Any] = {
        "task_id": comp.id,
        "user_id": user_id,
        "kind": "completion",
    }
    completion_payload.update(_task_payload_context(comp_upstream_request))
    return comp.id, [completion_payload]


async def _create_generation_tasks(
    command: GenerationTaskCommand,
    services: GenerationTaskServices,
) -> tuple[list[str], list[dict[str, Any]]]:
    db = command.db
    image_params = command.image_params
    resolved_size = command.resolved_size
    requested_count = max(1, min(10, image_params.count))
    action = (
        GenerationAction.EDIT.value
        if command.intent == Intent.IMAGE_TO_IMAGE
        else GenerationAction.GENERATE.value
    )
    primary = command.attachment_ids[0] if command.attachment_ids else None
    prompt_full = (command.text or "") + command.prompt_suffix
    upstream_request = image_upstream_request(
        image_params,
        resolved_size,
        prompt=prompt_full,
        default_output_format=command.default_image_output_format,
    )
    size_px = (
        (resolved_size.width or 0) * (resolved_size.height or 0)
        if resolved_size.width and resolved_size.height
        else billing_core.DEFAULT_IMAGE_SIZE_THRESHOLDS["1k"]
    )
    billing_tier = services.requested_image_billing_tier(image_params)
    base_upstream_request = _merge_request_metadata(
        upstream_request,
        command.request_metadata,
    )
    base_upstream_request.update(
        _image_queue_metadata(
            image_params,
            resolved_size,
            action=action,
            mask_image_id=command.mask_image_id,
            size_px=size_px,
            billing_tier=billing_tier,
        )
    )
    billing = await prepare_generation_billing(
        command,
        services,
        base_upstream_request=base_upstream_request,
        size_px=size_px,
        billing_tier=billing_tier,
    )
    if command.credential_pin:
        base_upstream_request["responses_model"] = (
            command.credential_pin.default_image_model
            or command.credential_pin.default_chat_model
        )
    batch = GenerationBatch(
        requested_count=requested_count,
        action=action,
        primary_image_id=primary,
        prompt=prompt_full,
        base_upstream_request=base_upstream_request,
        request_trace_id=base_upstream_request.get("trace_id"),
    )
    generation_ids: list[str] = []
    outbox_payloads: list[dict[str, Any]] = []
    for image_index in range(1, requested_count + 1):
        upstream_request = generation_upstream_request(batch, image_index)
        generation = new_generation(
            command,
            services,
            batch,
            image_index=image_index,
            upstream_request=upstream_request,
        )
        db.add(generation)
        await db.flush()
        await hold_generation_billing(
            command,
            services,
            batch,
            billing,
            generation,
            image_index=image_index,
        )
        generation_ids.append(generation.id)
        outbox_payloads.append(
            generation_outbox_payload(
                command,
                services,
                generation,
                image_index=image_index,
            )
        )
    return generation_ids, outbox_payloads


async def _create_outbox_rows(
    db: AsyncSession,
    outbox_payloads: list[dict[str, Any]],
) -> list[OutboxEvent]:
    outbox_rows: list[OutboxEvent] = []
    for payload in outbox_payloads:
        row = OutboxEvent(
            kind=payload["kind"],
            payload=payload,
            published_at=None,
        )
        db.add(row)
        outbox_rows.append(row)
    if outbox_rows:
        await db.flush()
        for payload, row in zip(outbox_payloads, outbox_rows, strict=False):
            payload["outbox_id"] = str(row.id)
            row.payload = dict(payload)
    return outbox_rows


async def create_assistant_task(
    *,
    db: AsyncSession,
    user_id: str,
    account_mode: str,
    conv: Conversation,
    user_msg: Message,
    intent: Intent,
    idempotency_key: str,
    image_params: ImageParamsIn,
    chat_params: ChatParamsIn,
    system_prompt: str | None,
    attachment_ids: list[str],
    text: str,
    user_email: str | None = None,
    default_image_output_format: str = DEFAULT_IMAGE_OUTPUT_FORMAT,
    mask_image_id: str | None = None,
    credential_pin: TaskCredentialPin | None = None,
    credential_pin_resolved: bool = False,
    chat_wallet_preflight_done: bool = False,
    chat_wallet_preflight: ChatWalletPreflight | None = None,
    request_metadata: dict[str, Any] | None = None,
    resolve_task_credential_pin_fn: AsyncCallable = resolve_task_credential_pin,
    ensure_chat_wallet_preflight_fn: AsyncCallable = ensure_chat_wallet_preflight,
    billing_enabled_fn: AsyncCallable = billing_enabled,
    billing_allow_negative_fn: AsyncCallable = billing_allow_negative,
    billing_image_thresholds_fn: AsyncCallable = billing_image_thresholds,
    user_rate_multiplier_fn: AsyncCallable = user_rate_multiplier_x10000,
    apply_rate_multiplier_fn: Callable[[int, int], int] = apply_rate_multiplier_micro,
    requested_image_billing_tier_fn: Callable[
        [ImageParamsIn], str | None
    ] = requested_image_billing_tier,
    write_audit_fn: AsyncCallable = write_audit,
) -> AssistantTaskResult:
    """Build assistant/task/outbox rows inside the caller's transaction."""
    produces_image = intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE)
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )

    resolved_size: ResolvedSize | None = None
    prompt_suffix = ""
    if produces_image:
        try:
            resolved_size = resolve_size(
                aspect=image_params.aspect_ratio,
                mode=image_params.size_mode,
                fixed=image_params.fixed_size,
            )
            prompt_suffix = resolved_size.prompt_suffix
        except Exception as exc:  # noqa: BLE001
            raise _http("invalid_size", f"size resolve failed: {exc}", 422) from exc

    if not credential_pin_resolved:
        credential_pin = await resolve_task_credential_pin_fn(
            db,
            user_id,
            "image" if produces_image else "chat",
            account_mode,
        )

    assistant_msg = Message(
        conversation_id=conv.id,
        role=Role.ASSISTANT.value,
        content={},
        parent_message_id=user_msg.id,
        intent=intent.value,
        status=MessageStatus.PENDING.value,
    )
    db.add(assistant_msg)
    await db.flush()

    stored_key = stored_idempotency_key(conv.id, idempotency_key)
    completion_id: str | None = None
    generation_ids: list[str] = []
    if intent in (Intent.CHAT, Intent.VISION_QA):
        completion_id, outbox_payloads = await _create_completion_task(
            db,
            CompletionTaskCommand(
                user_id=user_id,
                user_email=user_email,
                account_mode=account_mode,
                assistant_msg=assistant_msg,
                intent=intent,
                stored_key=stored_key,
                attachment_ids=attachment_ids,
                chat_params=chat_params,
                system_prompt=system_prompt,
                request_metadata=request_metadata,
                credential_pin=credential_pin,
                chat_wallet_preflight_done=chat_wallet_preflight_done,
                chat_wallet_preflight=chat_wallet_preflight,
            ),
            CompletionTaskServices(
                ensure_chat_wallet_preflight=ensure_chat_wallet_preflight_fn,
                billing_allow_negative=billing_allow_negative_fn,
                write_audit=write_audit_fn,
            ),
        )
    else:
        assert resolved_size is not None
        generation_ids, outbox_payloads = await _create_generation_tasks(
            GenerationTaskCommand(
                db=db,
                user_id=user_id,
                account_mode=account_mode,
                assistant_msg=assistant_msg,
                intent=intent,
                stored_key=stored_key,
                attachment_ids=attachment_ids,
                image_params=image_params,
                text=text,
                resolved_size=resolved_size,
                prompt_suffix=prompt_suffix,
                default_image_output_format=default_image_output_format,
                mask_image_id=mask_image_id,
                credential_pin=credential_pin,
                request_metadata=request_metadata,
            ),
            GenerationTaskServices(
                billing_enabled=billing_enabled_fn,
                billing_allow_negative=billing_allow_negative_fn,
                billing_image_thresholds=billing_image_thresholds_fn,
                user_rate_multiplier=user_rate_multiplier_fn,
                apply_rate_multiplier=apply_rate_multiplier_fn,
                requested_image_billing_tier=requested_image_billing_tier_fn,
                write_audit=write_audit_fn,
                child_idempotency_key=generation_child_idempotency_key,
                defer_seconds=image_multi_generation_defer_s,
                payload_context=_task_payload_context,
                billing_http_error=_billing_http_error,
            ),
        )
    outbox_rows = await _create_outbox_rows(db, outbox_payloads)
    return AssistantTaskResult(
        assistant_msg=assistant_msg,
        completion_id=completion_id,
        generation_ids=generation_ids,
        outbox_payloads=outbox_payloads,
        outbox_rows=outbox_rows,
    )


async def create_generation_batch_for_message(
    command: ExistingMessageGenerationCommand,
) -> AssistantTaskResult:
    return await execute_generation_batch_for_message(
        command,
        ExistingMessageGenerationServices(
            create_generation_tasks=_create_generation_tasks,
            create_outbox_rows=_create_outbox_rows,
            payload_context=_task_payload_context,
            result_factory=AssistantTaskResult,
        ),
    )


async def publish_message_appended(
    *,
    redis: Any,
    user_id: str,
    conv_id: str,
    message_ids: list[str],
    publish_sse_event_fn: AsyncCallable = publish_sse_event,
    publish_sse_events_fn: AsyncCallable = publish_sse_events,
    log: logging.Logger = logger,
) -> None:
    """Best-effort publish for cross-device message list synchronization."""
    if not message_ids:
        return
    try:
        if len(message_ids) == 1:
            message_id = message_ids[0]
            await publish_sse_event_fn(
                redis,
                user_id=user_id,
                channel=conv_channel(conv_id),
                event_name=EV_CONV_MSG_APPENDED,
                data={
                    "conversation_id": conv_id,
                    "message_id": message_id,
                },
            )
        else:
            await publish_sse_events_fn(
                redis,
                [
                    {
                        "user_id": user_id,
                        "channel": conv_channel(conv_id),
                        "event_name": EV_CONV_MSG_APPENDED,
                        "data": {
                            "conversation_id": conv_id,
                            "message_id": message_id,
                        },
                    }
                    for message_id in message_ids
                ],
            )
    except Exception:
        log.warning(
            "publish_message_appended failed user=%s conv=%s messages=%s",
            user_id,
            conv_id,
            message_ids,
            exc_info=True,
        )


async def publish_assistant_task(
    *,
    db: AsyncSession,
    redis: Any,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str,
    outbox_payloads: list[dict[str, Any]],
    outbox_rows: list[OutboxEvent],
    get_arq_pool_fn: AsyncCallable = get_arq_pool,
    publish_sse_event_fn: AsyncCallable = publish_sse_event,
    log: logging.Logger = logger,
) -> None:
    """Best-effort enqueue and publish after the caller commits."""
    try:
        pool = await get_arq_pool_fn()
        for payload in outbox_payloads:
            fn_name = (
                "run_completion"
                if payload["kind"] == "completion"
                else "run_generation"
            )
            enqueue_kwargs: dict[str, Any] = {}
            defer_s = payload.get("defer_s")
            if isinstance(defer_s, (int, float)) and defer_s > 0:
                enqueue_kwargs["_defer_by"] = float(defer_s)
            enqueue_kwargs["_job_id"] = arq_job_id(
                payload["kind"],
                payload["task_id"],
                payload.get("outbox_id"),
            )
            await pool.enqueue_job(
                fn_name,
                payload["task_id"],
                **enqueue_kwargs,
            )
            ev_name = (
                EV_COMP_QUEUED if payload["kind"] == "completion" else EV_GEN_QUEUED
            )
            id_field = (
                "completion_id" if payload["kind"] == "completion" else "generation_id"
            )
            event_data: dict[str, Any] = {
                id_field: payload["task_id"],
                "message_id": assistant_msg_id,
                "conversation_id": conv_id,
                "kind": payload["kind"],
            }
            for key in ("trace_id", "source", "action_source"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    event_data[key] = value
            input_images = payload.get("input_images")
            if isinstance(input_images, list):
                event_data["input_images"] = input_images
            await publish_sse_event_fn(
                redis,
                user_id=user_id,
                channel=task_channel(payload["task_id"]),
                event_name=ev_name,
                data=event_data,
            )
    except Exception:
        log.warning(
            "publish_assistant_task failed user=%s conv=%s msg=%s",
            user_id,
            conv_id,
            assistant_msg_id,
            exc_info=True,
        )
        return

    if outbox_rows:
        try:
            published_at = datetime.now(timezone.utc)
            for row in outbox_rows:
                row.published_at = published_at
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                log.warning(
                    "outbox row rollback failed user=%s msg=%s",
                    user_id,
                    assistant_msg_id,
                    exc_info=True,
                )
            log.warning(
                "outbox row mark-published failed user=%s msg=%s",
                user_id,
                assistant_msg_id,
                exc_info=True,
            )


__all__ = [
    "AssistantTaskResult",
    "TaskCredentialPin",
    "ExistingMessageGenerationCommand",
    "_sanitize_system_prompt_source",
    "billing_allow_negative",
    "billing_enabled",
    "billing_image_thresholds",
    "billing_setting_raw",
    "build_structured_system_prompt",
    "chat_max_tool_invocations",
    "chat_tool_budget_setting_micro",
    "create_assistant_task",
    "create_generation_batch_for_message",
    "ensure_chat_wallet_preflight",
    "ensure_file_search_configured",
    "generation_child_idempotency_key",
    "idempotency_lock_key",
    "idempotency_lookup_keys",
    "image_multi_generation_defer_s",
    "publish_assistant_task",
    "publish_message_appended",
    "resolve_system_prompt_for_message",
    "resolve_task_credential_pin",
    "stored_idempotency_key",
]
