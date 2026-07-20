from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent, Role
from lumen_core.models import Conversation, Image, Message, User
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    PostMessageIn,
    PostMessageOut,
)


AsyncCallable = Callable[..., Awaitable[Any]]
HttpErrorFactory = Callable[[str, str, int], Exception]


@dataclass(frozen=True)
class AssistantRequestContext:
    system_prompt: str | None
    default_image_output_format: str
    credential_pin: Any | None
    credential_pin_resolved: bool


@dataclass(frozen=True)
class AssistantContextRuntime:
    resolve_system_prompt: AsyncCallable
    resolve_credential_pin: AsyncCallable
    get_setting: AsyncCallable
    default_image_output_format: str
    image_output_format_values: set[str]


@dataclass(frozen=True)
class MessageTransactionRuntime:
    apply_pending_confirmation_reply: AsyncCallable
    create_assistant_task: AsyncCallable
    apply_explicit_memory_write: AsyncCallable
    lookup_idempotent_post: AsyncCallable
    http_error: HttpErrorFactory


@dataclass(frozen=True)
class MessageTransactionResult:
    idempotent_response: PostMessageOut | None
    user_message: Message | None
    assistant_task: Any | None
    reembed_ids: list[str]


def is_chat_intent(intent: Intent) -> bool:
    return intent in (Intent.CHAT, Intent.VISION_QA)


async def validate_attachment_ids(
    db: AsyncSession,
    *,
    user_id: str,
    attachment_ids: list[str],
    visibility_filter: Any | None,
    http_error: HttpErrorFactory,
) -> None:
    if not attachment_ids:
        return
    rows = (
        (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(attachment_ids),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                    *((visibility_filter,) if visibility_filter is not None else ()),
                )
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != len(attachment_ids):
        raise http_error(
            "invalid_attachment",
            "one or more attachment images are not owned or were deleted",
            400,
        )


async def validate_mask_image(
    db: AsyncSession,
    *,
    user_id: str,
    intent: Intent,
    attachment_ids: list[str],
    mask_image_id: str | None,
    visibility_filter: Any | None,
    http_error: HttpErrorFactory,
) -> None:
    if mask_image_id is None:
        return
    if intent != Intent.IMAGE_TO_IMAGE:
        raise http_error(
            "mask_requires_image_to_image",
            f"mask requires intent=image_to_image (got intent={intent.value})",
            422,
        )
    if len(attachment_ids) != 1:
        raise http_error(
            "mask_requires_single_reference_image",
            f"mask requires exactly one reference image (got {len(attachment_ids)})",
            422,
        )
    mask_row = (
        await db.execute(
            select(Image.id).where(
                Image.id == mask_image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                *((visibility_filter,) if visibility_filter is not None else ()),
            )
        )
    ).scalar_one_or_none()
    if mask_row is None:
        raise http_error("mask_not_found", "mask image not found", 404)


def build_user_content(
    body: PostMessageIn,
    *,
    request_metadata: dict[str, Any],
    attachment_ids: list[str],
    chat_params: ChatParamsIn,
    intent: Intent,
    allowed_reasoning_efforts: set[str],
    http_error: HttpErrorFactory,
) -> dict[str, Any]:
    attachments = request_metadata.get("attachment_roles")
    if not isinstance(attachments, list):
        attachments = [{"image_id": image_id} for image_id in attachment_ids]
    content: dict[str, Any] = {
        "text": body.text or "",
        "attachments": attachments,
    }
    for key in ("source", "action_source", "trace_id", "input_images", "mask_image_id"):
        if request_metadata.get(key):
            content[key] = request_metadata[key]
    if not is_chat_intent(intent):
        return content
    if chat_params.reasoning_effort:
        if chat_params.reasoning_effort not in allowed_reasoning_efforts:
            raise http_error(
                "invalid_reasoning_effort",
                "invalid reasoning_effort",
                422,
            )
        content["reasoning_effort"] = chat_params.reasoning_effort
    for key in (
        "fast",
        "web_search",
        "file_search",
        "code_interpreter",
        "image_generation",
    ):
        if getattr(chat_params, key):
            content[key] = True
    if chat_params.file_search and chat_params.vector_store_ids:
        content["vector_store_ids"] = [
            value.strip()
            for value in chat_params.vector_store_ids
            if isinstance(value, str) and value.strip()
        ]
    return content


async def resolve_assistant_context(
    db: AsyncSession,
    runtime: AssistantContextRuntime,
    *,
    user: User,
    conversation: Conversation,
    intent: Intent,
    chat_params: ChatParamsIn,
    account_mode: str,
) -> AssistantRequestContext:
    system_prompt = None
    credential_pin = None
    credential_pin_resolved = False
    if is_chat_intent(intent):
        system_prompt = await runtime.resolve_system_prompt(
            db,
            user_id=user.id,
            default_system_prompt_id=user.default_system_prompt_id,
            conv=conversation,
            explicit_prompt=chat_params.system_prompt,
        )
        credential_pin = await runtime.resolve_credential_pin(
            db,
            user.id,
            "chat",
            account_mode,
        )
        credential_pin_resolved = True

    output_format = runtime.default_image_output_format
    if intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE):
        spec = get_spec("image.output_format")
        if spec is not None:
            raw_format = await runtime.get_setting(db, spec)
            if raw_format in runtime.image_output_format_values:
                output_format = raw_format
    return AssistantRequestContext(
        system_prompt=system_prompt,
        default_image_output_format=output_format,
        credential_pin=credential_pin,
        credential_pin_resolved=credential_pin_resolved,
    )


async def persist_message_request(
    db: AsyncSession,
    runtime: MessageTransactionRuntime,
    *,
    user: User,
    conversation: Conversation,
    conv_id: str,
    body: PostMessageIn,
    intent: Intent,
    user_content: dict[str, Any],
    image_params: ImageParamsIn,
    chat_params: ChatParamsIn,
    assistant_context: AssistantRequestContext,
    attachment_ids: list[str],
    mask_image_id: str | None,
    request_metadata: dict[str, Any],
    account_mode: str,
    now: datetime,
) -> MessageTransactionResult:
    user_message = Message(
        conversation_id=conv_id,
        role=Role.USER.value,
        content=user_content,
        intent=None,
        status=None,
    )
    reembed_ids: list[str] = []
    try:
        db.add(user_message)
        await db.flush()
        if is_chat_intent(intent):
            await runtime.apply_pending_confirmation_reply(
                db=db,
                user=user,
                conv=conversation,
                user_msg=user_message,
                text=body.text or "",
            )
        result = await runtime.create_assistant_task(
            db=db,
            user_id=user.id,
            user_email=user.email,
            account_mode=account_mode,
            conv=conversation,
            user_msg=user_message,
            intent=intent,
            idempotency_key=body.idempotency_key,
            image_params=image_params,
            chat_params=chat_params,
            system_prompt=assistant_context.system_prompt,
            attachment_ids=attachment_ids,
            text=body.text or "",
            default_image_output_format=(assistant_context.default_image_output_format),
            mask_image_id=mask_image_id,
            credential_pin=assistant_context.credential_pin,
            credential_pin_resolved=(assistant_context.credential_pin_resolved),
            request_metadata=request_metadata,
        )
        if is_chat_intent(intent):
            await runtime.apply_explicit_memory_write(
                db=db,
                user=user,
                conv=conversation,
                user_msg=user_message,
                assistant_msg=result.assistant_msg,
                text=body.text or "",
                reembed_ids=reembed_ids,
            )
        conversation.last_activity_at = now
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await runtime.lookup_idempotent_post(
            db,
            user.id,
            conv_id,
            body.idempotency_key,
        )
        if prior is not None:
            return MessageTransactionResult(prior, None, None, [])
        raise runtime.http_error(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        )
    except Exception:
        await db.rollback()
        raise
    return MessageTransactionResult(
        idempotent_response=None,
        user_message=user_message,
        assistant_task=result,
        reembed_ids=reembed_ids,
    )
