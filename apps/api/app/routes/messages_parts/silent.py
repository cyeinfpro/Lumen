"""Silent image generation workflow for reroll and upscale requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS, MAX_PROMPT_CHARS, Intent, Role
from lumen_core.model_entities import (
    Conversation,
    Generation,
    Image,
    Message,
    User,
)
from lumen_core.schema_models import (
    ChatParamsIn,
    ImageParamsIn,
    MessageOut,
)

from ...services.active_user import (
    ActiveUserFenceError,
    account_mode_from_user,
    active_user_fence_http_error,
    lock_active_user_snapshot,
)
from ...services.agent_conversations import studio_conversation_filter
from ...services.message_idempotency import (
    SILENT_GENERATION_IDEMPOTENCY_OPERATION,
    idempotency_request_metadata,
    require_matching_task_idempotency,
    task_idempotency_metadata,
)


SILENT_GENERATION_REQUEST_HASH_KEY = "request_hash"


class SilentGenerationIn(BaseModel):
    # Keep retry compatibility with v1.2.87-v1.2.90 browser journals that
    # persisted ``semantic-`` + SHA-256 keys before the client-side fix.
    idempotency_key: str = Field(min_length=1, max_length=96)
    parent_message_id: str
    intent: Literal["text_to_image", "image_to_image"] = "text_to_image"
    image_params: ImageParamsIn = Field(default_factory=ImageParamsIn)
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    attachment_image_ids: list[str] = Field(
        default_factory=list,
        max_length=MAX_MESSAGE_ATTACHMENTS,
    )


class SilentGenerationOut(BaseModel):
    assistant_message: MessageOut
    generation_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class SilentGenerationRuntime:
    get_redis: Callable[[], Any]
    http_error: Callable[..., Exception]
    ensure_conversation_visible: Callable[..., Awaitable[None]]
    retention_policy_for_user: Callable[..., Awaitable[Any | None]]
    request_hash: Callable[[SilentGenerationIn], str]
    request_hash_key: str
    lookup_silent_generation: Callable[..., Awaitable[SilentGenerationOut | None]]
    lock_idempotency_key: Callable[..., Awaitable[bool]]
    message_user_visible_filters: Callable[..., tuple[Any, ...]]
    byok_image_visible_filter: Callable[..., Awaitable[Any | None]]
    get_spec: Callable[[str], Any]
    get_setting: Callable[..., Awaitable[Any]]
    create_assistant_task: Callable[..., Awaitable[Any]]
    await_post_commit_publishes: Callable[..., Awaitable[None]]
    publish_message_appended: Callable[..., Awaitable[None]]
    publish_assistant_task: Callable[..., Awaitable[None]]
    default_image_output_format: str
    image_output_format_values: frozenset[str]


def silent_generation_request_hash(body: SilentGenerationIn) -> str:
    payload = {
        "parent_message_id": body.parent_message_id,
        "intent": body.intent,
        "prompt": body.prompt,
        "attachment_image_ids": list(body.attachment_image_ids),
        "image_params": body.image_params.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stored_silent_generation_request_hash(generation: Generation) -> Any:
    request = getattr(generation, "upstream_request", None)
    if not isinstance(request, dict):
        return None
    return request.get(SILENT_GENERATION_REQUEST_HASH_KEY)


async def lookup_silent_generation(
    db: AsyncSession,
    *,
    user: User,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
    parent_message_id: str,
    request_hash: str,
    retention_policy: Any | None,
    idempotency_lookup_keys_fn: Callable[[str, str], tuple[str, ...]],
    message_user_visible_filters_fn: Callable[..., tuple[Any, ...]],
    stored_request_hash_fn: Callable[[Generation], Any],
    http_error_fn: Callable[..., Exception],
) -> SilentGenerationOut | None:
    lookup_keys = idempotency_lookup_keys_fn(conv_id, idempotency_key)
    anchor = (
        await db.execute(
            select(Generation)
            .join(Message, Message.id == Generation.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Generation.user_id == user_id,
                Generation.idempotency_key.in_(lookup_keys),
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                studio_conversation_filter(),
            )
            .order_by(Generation.created_at.asc(), Generation.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        return None
    stored_operation, stored_fingerprint = task_idempotency_metadata(anchor)
    has_persisted_contract = (
        stored_operation is not None or stored_fingerprint is not None
    )
    if has_persisted_contract:
        require_matching_task_idempotency(
            [anchor],
            operation_namespace=SILENT_GENERATION_IDEMPOTENCY_OPERATION,
            request_fingerprint=request_hash,
            http_error=http_error_fn,
        )
    assistant_msg = (
        await db.execute(
            select(Message).where(
                Message.id == anchor.message_id,
                Message.conversation_id == conv_id,
                Message.role == Role.ASSISTANT.value,
                *message_user_visible_filters_fn(
                    user,
                    retention_policy=retention_policy,
                ),
            )
        )
    ).scalar_one_or_none()
    if assistant_msg is None:
        raise http_error_fn("not_found", "assistant message not found", 404)
    stored_parent_message_id = assistant_msg.parent_message_id
    if not isinstance(stored_parent_message_id, str) or not stored_parent_message_id:
        raise http_error_fn("idempotency_conflict", "idempotency_key conflict", 409)
    stored_parent = (
        await db.execute(
            select(Message).where(
                Message.id == stored_parent_message_id,
                Message.conversation_id == conv_id,
                *message_user_visible_filters_fn(
                    user,
                    retention_policy=retention_policy,
                ),
            )
        )
    ).scalar_one_or_none()
    if stored_parent is None:
        raise http_error_fn("not_found", "parent message not found", 404)
    generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id == anchor.message_id,
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not generations:
        generations = [anchor]
    if has_persisted_contract:
        require_matching_task_idempotency(
            generations,
            operation_namespace=SILENT_GENERATION_IDEMPOTENCY_OPERATION,
            request_fingerprint=request_hash,
            http_error=http_error_fn,
        )
    if stored_parent_message_id != parent_message_id:
        raise http_error_fn("idempotency_conflict", "idempotency_key conflict", 409)

    stored_hashes = [stored_request_hash_fn(item) for item in generations]
    present_hashes = [value for value in stored_hashes if value is not None]
    if not has_persisted_contract:
        if not present_hashes or (
            len(present_hashes) != len(stored_hashes)
            or any(
                not isinstance(value, str) or value != request_hash
                for value in present_hashes
            )
        ):
            raise http_error_fn(
                "idempotency_conflict",
                "idempotency_key conflict",
                409,
            )
    return SilentGenerationOut(
        assistant_message=MessageOut.model_validate(assistant_msg),
        generation_ids=[generation.id for generation in generations],
    )


async def create_silent_generation(
    conv_id: str,
    body: SilentGenerationIn,
    user: User,
    db: AsyncSession,
    *,
    runtime: SilentGenerationRuntime,
    session_id: str | None = None,
) -> SilentGenerationOut:
    expected_account_mode = account_mode_from_user(user)
    redis = runtime.get_redis()
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user.id,
                Conversation.deleted_at.is_(None),
                studio_conversation_filter(),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise runtime.http_error("not_found", "conversation not found", 404)
    await runtime.ensure_conversation_visible(db, conv, user)

    request_hash = runtime.request_hash(body)
    retention_policy = await runtime.retention_policy_for_user(db, user)
    lookup_args = {
        "user": user,
        "user_id": user.id,
        "conv_id": conv_id,
        "idempotency_key": body.idempotency_key,
        "parent_message_id": body.parent_message_id,
        "request_hash": request_hash,
        "retention_policy": retention_policy,
    }
    prior = await runtime.lookup_silent_generation(db, **lookup_args)
    if prior is not None:
        return prior
    if await runtime.lock_idempotency_key(
        db,
        user.id,
        conv_id,
        body.idempotency_key,
    ):
        prior = await runtime.lookup_silent_generation(db, **lookup_args)
        if prior is not None:
            return prior

    parent_msg = (
        await db.execute(
            select(Message).where(
                Message.id == body.parent_message_id,
                Message.conversation_id == conv_id,
                *runtime.message_user_visible_filters(
                    user,
                    retention_policy=retention_policy,
                ),
            )
        )
    ).scalar_one_or_none()
    if not parent_msg:
        raise runtime.http_error("not_found", "parent message not found", 404)

    attachment_ids = list(body.attachment_image_ids or [])
    if attachment_ids:
        image_retention_filter = await runtime.byok_image_visible_filter(db, user)
        rows = (
            (
                await db.execute(
                    select(Image.id).where(
                        Image.id.in_(attachment_ids),
                        Image.user_id == user.id,
                        Image.deleted_at.is_(None),
                        *(
                            (image_retention_filter,)
                            if image_retention_filter is not None
                            else ()
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(rows) != len(attachment_ids):
            raise runtime.http_error(
                "invalid_attachment",
                "attachment not owned or deleted",
                400,
            )

    intent = Intent(body.intent)
    default_image_output_format = runtime.default_image_output_format
    spec = runtime.get_spec("image.output_format")
    if spec is not None:
        raw_default_format = await runtime.get_setting(db, spec)
        if raw_default_format in runtime.image_output_format_values:
            default_image_output_format = raw_default_format
    image_params = body.image_params
    try:
        snapshot = await lock_active_user_snapshot(
            db,
            user.id,
            expected_account_mode,
            session_id=session_id,
        )
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    user = snapshot.user
    result = await runtime.create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=snapshot.account_mode,
        conv=conv,
        user_msg=parent_msg,
        intent=intent,
        idempotency_key=body.idempotency_key,
        image_params=image_params,
        chat_params=ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=body.prompt,
        default_image_output_format=default_image_output_format,
        request_metadata=idempotency_request_metadata(
            {runtime.request_hash_key: request_hash},
            operation_namespace=SILENT_GENERATION_IDEMPOTENCY_OPERATION,
            request_fingerprint=request_hash,
        ),
    )
    conv.last_activity_at = datetime.now(timezone.utc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        prior = await runtime.lookup_silent_generation(db, **lookup_args)
        if prior is not None:
            return prior
        raise runtime.http_error(
            "idempotency_conflict",
            "idempotency_key conflict",
            409,
        )

    await db.refresh(result.assistant_msg)
    await runtime.await_post_commit_publishes(
        (
            "message_appended",
            runtime.publish_message_appended(
                redis=redis,
                user_id=user.id,
                conv_id=conv_id,
                message_ids=[result.assistant_msg.id],
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
    return SilentGenerationOut(
        assistant_message=MessageOut.model_validate(result.assistant_msg),
        generation_ids=result.generation_ids,
    )
