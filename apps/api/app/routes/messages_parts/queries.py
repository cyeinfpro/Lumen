"""Visibility and idempotency queries for message routes."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Completion,
    Conversation,
    Generation,
    Image,
    Message,
    User,
)
from lumen_core.schema_models import (
    MessageOut,
    PostMessageOut,
)


async def lock_idempotency_key(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
    *,
    idempotency_lock_key_fn: Callable[[str, str, str], str],
) -> bool:
    connection = getattr(db, "connection", None)
    if connection is None:
        return False
    bind = await connection()
    if bind.dialect.name != "postgresql":
        return False
    lock_key = idempotency_lock_key_fn(user_id, conv_id, idempotency_key)
    await db.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_key))))
    return True


def message_alive_filters() -> tuple[Any, ...]:
    deleted_at = getattr(Message, "deleted_at", None)
    if deleted_at is None:
        return ()
    return (deleted_at.is_(None),)


async def byok_retention_policy_for_user(
    db: AsyncSession,
    user: User,
    *,
    applies_to_user_fn: Callable[[User], bool],
    read_byok_settings_cached_fn: Callable[[AsyncSession], Awaitable[Any]],
    retention_policy_from_settings_fn: Callable[[Any], Any],
) -> Any | None:
    if not applies_to_user_fn(user):
        return None
    settings = await read_byok_settings_cached_fn(db)
    return retention_policy_from_settings_fn(settings)


def message_user_visible_filters(
    user: User,
    *,
    retention_policy: Any | None,
    message_alive_filters_fn: Callable[[], tuple[Any, ...]],
    user_visible_filter_fn: Callable[..., Any],
) -> tuple[Any, ...]:
    filters = list(message_alive_filters_fn())
    if retention_policy is not None:
        retention_filter = user_visible_filter_fn(
            user,
            Message.created_at,
            policy=retention_policy,
        )
        if retention_filter is not None:
            filters.append(retention_filter)
    return tuple(filters)


async def ensure_conversation_visible_to_user(
    db: AsyncSession,
    conv: Conversation,
    user: User,
    *,
    retention_policy_for_user_fn: Callable[[AsyncSession, User], Awaitable[Any | None]],
    is_user_visible_fn: Callable[..., bool],
    http_error_fn: Callable[..., Exception],
) -> None:
    policy = await retention_policy_for_user_fn(db, user)
    if policy is None:
        return
    if not is_user_visible_fn(
        account_mode=user.account_mode,
        created_at=conv.last_activity_at,
        policy=policy,
    ):
        raise http_error_fn("not_found", "conversation not found", 404)


async def byok_image_visible_filter(
    db: AsyncSession,
    user: User,
    *,
    retention_policy_for_user_fn: Callable[[AsyncSession, User], Awaitable[Any | None]],
    user_visible_filter_fn: Callable[..., Any],
) -> Any | None:
    policy = await retention_policy_for_user_fn(db, user)
    if policy is None:
        return None
    return user_visible_filter_fn(user, Image.created_at, policy=policy)


async def lookup_idempotent_post(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
    *,
    operation_namespace: str,
    request_fingerprint: str,
    message_alive_filters_fn: Callable[[], tuple[Any, ...]],
    idempotency_lookup_keys_fn: Callable[[str, str], tuple[str, ...]],
    require_matching_task_idempotency_fn: Callable[..., None],
    http_error_fn: Callable[[str, str, int], Exception],
) -> PostMessageOut | None:
    alive_filters = message_alive_filters_fn()
    lookup_keys = idempotency_lookup_keys_fn(conv_id, idempotency_key)
    comp_hit = (
        await db.execute(
            select(Completion)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user_id,
                Completion.idempotency_key.in_(lookup_keys),
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    gen_anchor = (
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
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if comp_hit is not None and gen_anchor is not None:
        raise http_error_fn(
            "idempotency_conflict",
            "idempotency_key matched multiple task types",
            409,
        )
    task_hit = comp_hit or gen_anchor
    if task_hit is not None:
        require_matching_task_idempotency_fn(
            [task_hit],
            operation_namespace=operation_namespace,
            request_fingerprint=request_fingerprint,
            http_error=http_error_fn,
        )
    if comp_hit is not None:
        anchor_msg_id = comp_hit.message_id
    elif gen_anchor is not None:
        anchor_msg_id = gen_anchor.message_id
    else:
        return None
    assistant_msg = (
        await db.execute(
            select(Message).where(
                Message.id == anchor_msg_id,
                Message.conversation_id == conv_id,
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if assistant_msg is None:
        return None
    gen_hits: list[Generation] = []
    if gen_anchor is not None:
        gen_hits = list(
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == user_id,
                        Generation.message_id == anchor_msg_id,
                    )
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                )
            )
            .scalars()
            .all()
        )
        require_matching_task_idempotency_fn(
            gen_hits,
            operation_namespace=operation_namespace,
            request_fingerprint=request_fingerprint,
            http_error=http_error_fn,
        )
    user_msg = None
    if assistant_msg.parent_message_id:
        user_msg = (
            await db.execute(
                select(Message).where(
                    Message.id == assistant_msg.parent_message_id,
                    Message.conversation_id == conv_id,
                    *alive_filters,
                )
            )
        ).scalar_one_or_none()
    if user_msg is None:
        return None
    return PostMessageOut(
        user_message=MessageOut.model_validate(user_msg),
        assistant_message=MessageOut.model_validate(assistant_msg),
        completion_id=comp_hit.id if comp_hit else None,
        generation_ids=[generation.id for generation in gen_hits],
    )


async def get_message(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    message_id: str,
    message_alive_filters_fn: Callable[[], tuple[Any, ...]],
    http_error_fn: Callable[..., Exception],
) -> MessageOut:
    msg = (
        await db.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *message_alive_filters_fn(),
            )
        )
    ).scalar_one_or_none()
    if msg is None:
        raise http_error_fn("not_found", "message not found", 404)
    return MessageOut.model_validate(msg)
