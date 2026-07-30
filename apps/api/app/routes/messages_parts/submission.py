"""Ordinary user-message submission orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent
from lumen_core.model_entities import (
    Conversation,
    User,
)
from lumen_core.schema_models import (
    MessageOut,
    PostMessageIn,
    PostMessageOut,
)

from ...services.message_request import (
    PersistMessageRequestCommand,
    build_user_content,
    is_chat_intent,
    persist_message_request,
    resolve_assistant_context,
    validate_attachment_ids,
    validate_mask_image,
)


@dataclass(frozen=True)
class SubmissionRuntime:
    get_redis: Callable[[], Any]
    messages_limiter: Any
    http_error: Callable[..., Exception]
    ensure_conversation_visible: Callable[..., Awaitable[None]]
    lookup_idempotent_post: Callable[..., Awaitable[PostMessageOut | None]]
    lock_idempotency_key: Callable[..., Awaitable[bool]]
    byok_image_visible_filter: Callable[..., Awaitable[Any | None]]
    resolve_intent: Callable[..., Intent]
    message_request_metadata: Callable[..., dict[str, Any]]
    resolve_fast_default: Callable[[AsyncSession], Awaitable[bool]]
    image_params_with_fast_default: Callable[..., Any]
    chat_params_with_fast_default: Callable[..., Any]
    ensure_file_search_configured: Callable[..., Awaitable[None]]
    assistant_context_runtime: Callable[[], Any]
    message_transaction_runtime: Callable[[], Any]
    enqueue_memory_reembed: Callable[[str, str], Awaitable[None]]
    await_post_commit_publishes: Callable[..., Awaitable[None]]
    publish_message_appended: Callable[..., Awaitable[None]]
    publish_assistant_task: Callable[..., Awaitable[None]]
    allowed_reasoning_efforts: frozenset[str]


async def submit_user_message(
    conv_id: str,
    body: PostMessageIn,
    user: User,
    db: AsyncSession,
    *,
    runtime: SubmissionRuntime,
) -> PostMessageOut:
    redis = runtime.get_redis()
    await runtime.messages_limiter.check(redis, f"rl:msg:{user.id}")

    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise runtime.http_error("not_found", "conversation not found", 404)
    await runtime.ensure_conversation_visible(db, conv, user)

    prior = await runtime.lookup_idempotent_post(
        db,
        user.id,
        conv_id,
        body.idempotency_key,
    )
    if prior is not None:
        return prior
    if await runtime.lock_idempotency_key(
        db,
        user.id,
        conv_id,
        body.idempotency_key,
    ):
        prior = await runtime.lookup_idempotent_post(
            db,
            user.id,
            conv_id,
            body.idempotency_key,
        )
        if prior is not None:
            return prior

    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        image_retention_filter = await runtime.byok_image_visible_filter(db, user)
        await validate_attachment_ids(
            db,
            user_id=user.id,
            attachment_ids=attachment_ids,
            visibility_filter=image_retention_filter,
            http_error=runtime.http_error,
        )

    mask_image_id = (body.mask_image_id or "").strip() or None
    intent = runtime.resolve_intent(
        explicit=body.intent,
        text=body.text or "",
        has_attachment=bool(attachment_ids),
    )
    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise runtime.http_error(
            "missing_reference_image",
            "image_to_image requires at least one reference image",
            400,
        )
    if mask_image_id is not None:
        image_retention_filter = await runtime.byok_image_visible_filter(db, user)
        await validate_mask_image(
            db,
            user_id=user.id,
            intent=intent,
            attachment_ids=attachment_ids,
            mask_image_id=mask_image_id,
            visibility_filter=image_retention_filter,
            http_error=runtime.http_error,
        )

    now = datetime.now(timezone.utc)
    request_metadata = runtime.message_request_metadata(
        body,
        attachment_ids=attachment_ids,
        mask_image_id=mask_image_id,
        intent=intent,
    )
    fast_default = await runtime.resolve_fast_default(db)
    image_params = runtime.image_params_with_fast_default(
        body.image_params,
        fast_default,
    )
    chat_params = runtime.chat_params_with_fast_default(
        body.chat_params,
        fast_default,
    )
    if is_chat_intent(intent):
        await runtime.ensure_file_search_configured(db, chat_params)
    user_content = build_user_content(
        body,
        request_metadata=request_metadata,
        attachment_ids=attachment_ids,
        chat_params=chat_params,
        intent=intent,
        allowed_reasoning_efforts=runtime.allowed_reasoning_efforts,
        http_error=runtime.http_error,
    )
    account_mode = getattr(user, "account_mode", "wallet")
    assistant_context = await resolve_assistant_context(
        db,
        runtime.assistant_context_runtime(),
        user=user,
        conversation=conv,
        intent=intent,
        chat_params=chat_params,
        account_mode=account_mode,
    )
    transaction = await persist_message_request(
        db,
        runtime.message_transaction_runtime(),
        PersistMessageRequestCommand(
            user=user,
            conversation=conv,
            conversation_id=conv_id,
            body=body,
            intent=intent,
            user_content=user_content,
            image_params=image_params,
            chat_params=chat_params,
            assistant_context=assistant_context,
            attachment_ids=attachment_ids,
            mask_image_id=mask_image_id,
            request_metadata=request_metadata,
            account_mode=account_mode,
            now=now,
        ),
    )
    if transaction.idempotent_response is not None:
        return transaction.idempotent_response
    user_msg = transaction.user_message
    result = transaction.assistant_task
    assert user_msg is not None and result is not None
    await db.refresh(user_msg)
    await db.refresh(result.assistant_msg)
    for memory_id in transaction.reembed_ids:
        await runtime.enqueue_memory_reembed("memory", memory_id)

    await runtime.await_post_commit_publishes(
        (
            "message_appended",
            runtime.publish_message_appended(
                redis=redis,
                user_id=user.id,
                conv_id=conv_id,
                message_ids=[user_msg.id, result.assistant_msg.id],
            ),
            None,
        ),
        (
            "assistant_task",
            runtime.publish_assistant_task(
                db=db,
                redis=redis,
                user_id=user.id,
                conv_id=conv_id,
                assistant_msg_id=result.assistant_msg.id,
                outbox_payloads=result.outbox_payloads,
                outbox_rows=result.outbox_rows,
            ),
            result.assistant_msg.id,
        ),
        user_id=user.id,
        conv_id=conv_id,
    )
    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(result.assistant_msg),
        completion_id=result.completion_id,
        generation_ids=result.generation_ids,
    )
