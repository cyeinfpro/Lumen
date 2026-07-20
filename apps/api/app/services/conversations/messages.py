"""Message pagination and optional task/image aggregation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
)
from lumen_core.schemas import (
    CompletionOut,
    GenerationOut,
    ImageOut,
    MessageOut,
)

from ...byok_service import read_byok_settings_cached, retention_policy_from_settings
from .cursor import (
    coerce_aware,
    cursor_field_datetime,
    cursor_field_str,
    dec_cursor,
    enc_cursor,
    message_alive_filters,
)
from ...deps import CurrentUser
from .contracts import MessageListOut


TASK_INCLUDE_LIMIT = 100


async def _retention_filter(
    db: AsyncSession,
    user: Any,
    column: Any,
) -> Any | None:
    from lumen_core.byok_retention import (
        applies_to_user,
        user_visible_filter,
    )

    if not applies_to_user(user):
        return None
    policy = retention_policy_from_settings(await read_byok_settings_cached(db))
    return user_visible_filter(user, column, policy=policy)


async def _message_statement(
    db: AsyncSession,
    *,
    conv_id: str,
    user: Any,
    cursor: str | None,
    since: str | None,
    limit: int,
) -> tuple[Any, bool]:
    alive_filters = message_alive_filters()
    retention_filter = await _retention_filter(db, user, Message.created_at)
    stmt = (
        select(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.conversation_id == conv_id,
            Conversation.id == conv_id,
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
            *alive_filters,
        )
    )
    if retention_filter is not None:
        stmt = stmt.where(retention_filter)

    if since:
        parsed_dt: datetime | None
        try:
            parsed_dt = datetime.fromisoformat(since)
        except ValueError:
            parsed_dt = None
        if parsed_dt is not None:
            stmt = stmt.where(Message.created_at > coerce_aware(parsed_dt))
        else:
            ref = (
                await db.execute(
                    select(Message.created_at).where(
                        Message.id == since,
                        Message.conversation_id == conv_id,
                        Message.conversation_id.in_(
                            select(Conversation.id).where(
                                Conversation.id == conv_id,
                                Conversation.user_id == user.id,
                                Conversation.deleted_at.is_(None),
                            )
                        ),
                        *alive_filters,
                    )
                )
            ).scalar_one_or_none()
            if ref is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": {
                            "code": "invalid_since",
                            "message": (
                                "since must be an ISO8601 timestamp or a message "
                                "id in this conversation"
                            ),
                        }
                    },
                )
            stmt = stmt.where(
                or_(
                    Message.created_at > ref,
                    and_(Message.created_at == ref, Message.id > since),
                )
            )

    uses_desc_order = False
    cur = dec_cursor(cursor)
    if cur is not None:
        ca = cursor_field_datetime(cur, "ca")
        cur_id = cursor_field_str(cur, "id")
        stmt = stmt.where(
            or_(
                Message.created_at < ca,
                and_(
                    Message.created_at == ca,
                    Message.id < cur_id,
                ),
            )
        ).order_by(desc(Message.created_at), desc(Message.id))
        uses_desc_order = True
    elif since:
        stmt = stmt.order_by(Message.created_at.asc(), Message.id.asc())
    else:
        stmt = stmt.order_by(desc(Message.created_at), desc(Message.id))
        uses_desc_order = True
    return stmt.limit(limit + 1), uses_desc_order


async def _load_task_rows(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    message_ids: list[str],
) -> tuple[list[Generation], list[Completion]]:
    gens = (
        (
            await db.execute(
                select(Generation)
                .join(Message, Message.id == Generation.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Generation.message_id.in_(message_ids),
                    Generation.user_id == user_id,
                    Conversation.id == conv_id,
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(desc(Generation.created_at), desc(Generation.id))
                .limit(TASK_INCLUDE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    comps = (
        (
            await db.execute(
                select(Completion)
                .join(Message, Message.id == Completion.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Completion.message_id.in_(message_ids),
                    Completion.user_id == user_id,
                    Conversation.id == conv_id,
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(desc(Completion.created_at), desc(Completion.id))
                .limit(TASK_INCLUDE_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return list(gens), list(comps)


def _collect_image_ids(
    messages: list[Message],
) -> tuple[set[str], set[str]]:
    attachment_ids: set[str] = set()
    assistant_ids: set[str] = set()
    for message in messages:
        content = message.content if isinstance(message.content, dict) else {}
        if message.role == "user":
            for attachment in content.get("attachments") or []:
                image_id = (
                    attachment.get("image_id") if isinstance(attachment, dict) else None
                )
                if isinstance(image_id, str):
                    attachment_ids.add(image_id)
        elif message.role == "assistant":
            for image_ref in content.get("images") or []:
                image_id = (
                    image_ref.get("image_id") if isinstance(image_ref, dict) else None
                )
                if isinstance(image_id, str):
                    assistant_ids.add(image_id)
    return attachment_ids, assistant_ids


async def _load_images(
    db: AsyncSession,
    *,
    user: Any,
    gen_ids: list[str],
    image_ids: set[str],
) -> list[Image]:
    if not gen_ids and not image_ids:
        return []
    stmt = select(Image).where(
        Image.user_id == user.id,
        Image.deleted_at.is_(None),
    )
    image_retention_filter = await _retention_filter(db, user, Image.created_at)
    if gen_ids and image_ids:
        predicates = [
            and_(Image.owner_generation_id.in_(gen_ids), image_retention_filter)
            if image_retention_filter is not None
            else Image.owner_generation_id.in_(gen_ids),
            Image.id.in_(image_ids),
        ]
        stmt = stmt.where(or_(*predicates))
    elif gen_ids:
        stmt = stmt.where(Image.owner_generation_id.in_(gen_ids))
        if image_retention_filter is not None:
            stmt = stmt.where(image_retention_filter)
    else:
        stmt = stmt.where(Image.id.in_(image_ids))
    stmt = stmt.order_by(desc(Image.created_at), desc(Image.id)).limit(
        TASK_INCLUDE_LIMIT
    )
    return list((await db.execute(stmt)).scalars().all())


async def _image_variant_map(
    db: AsyncSession,
    images: list[Image],
) -> dict[str, set[str]]:
    if not images:
        return {}
    rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_([image.id for image in images])
            )
        )
    ).all()
    variants: dict[str, set[str]] = {}
    for image_id, kind in rows:
        variants.setdefault(image_id, set()).add(kind)
    return variants


def image_to_out(
    image: Image,
    variant_kinds: set[str] | None = None,
) -> ImageOut:
    variant_kinds = variant_kinds or set()
    metadata = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
    billing_label = (
        metadata.get("billing_label")
        if isinstance(metadata.get("billing_label"), str)
        else None
    )
    billing_exempt_reason = (
        metadata.get("billing_exempt_reason")
        if isinstance(metadata.get("billing_exempt_reason"), str)
        else None
    )
    is_dual_race_bonus = metadata.get("is_dual_race_bonus") is True
    billing_free = (
        metadata.get("billing_free") is True
        or is_dual_race_bonus
        or billing_label == "free"
    )
    return ImageOut(
        id=image.id,
        source=image.source,
        parent_image_id=image.parent_image_id,
        owner_generation_id=image.owner_generation_id,
        width=image.width,
        height=image.height,
        mime=image.mime,
        blurhash=image.blurhash,
        url=f"/api/images/{image.id}/binary",
        display_url=f"/api/images/{image.id}/variants/display2048",
        preview_url=(
            f"/api/images/{image.id}/variants/preview1024"
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            f"/api/images/{image.id}/variants/thumb256"
            if "thumb256" in variant_kinds
            else None
        ),
        metadata_jsonb=metadata,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )


async def _include_tasks_and_images(
    db: AsyncSession,
    *,
    user: Any,
    conv_id: str,
    items: list[Message],
    output: MessageListOut,
) -> None:
    if not items:
        return
    gens, comps = await _load_task_rows(
        db,
        conv_id=conv_id,
        user_id=user.id,
        message_ids=[message.id for message in items],
    )
    attachment_ids, assistant_ids = _collect_image_ids(items)
    images = await _load_images(
        db,
        user=user,
        gen_ids=[generation.id for generation in gens],
        image_ids=attachment_ids | assistant_ids,
    )
    variants = await _image_variant_map(db, images)
    output.generations = [
        GenerationOut.model_validate(generation) for generation in gens
    ]
    output.completions = [
        CompletionOut.model_validate(completion) for completion in comps
    ]
    output.images = [image_to_out(image, variants.get(image.id)) for image in images]


async def list_messages(
    conv_id: str,
    user: CurrentUser,
    db: AsyncSession,
    *,
    cursor: str | None = None,
    since: str | None = None,
    limit: int = 50,
    include: str | None = None,
    get_owned_visible_conv: Callable[..., Awaitable[Any]],
) -> MessageListOut:
    await get_owned_visible_conv(db, conv_id, user)
    stmt, uses_desc_order = await _message_statement(
        db,
        conv_id=conv_id,
        user=user,
        cursor=cursor,
        since=since,
        limit=limit,
    )
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = list(rows[:limit])
    if uses_desc_order:
        items.reverse()
    next_cursor = None
    if has_more and items:
        last = items[0] if uses_desc_order else items[-1]
        next_cursor = enc_cursor({"ca": last.created_at.isoformat(), "id": last.id})
    output = MessageListOut(
        items=[MessageOut.model_validate(message) for message in items],
        next_cursor=next_cursor,
    )
    include_set = {part.strip() for part in (include or "").split(",") if part.strip()}
    if "tasks" in include_set:
        await _include_tasks_and_images(
            db,
            user=user,
            conv_id=conv_id,
            items=items,
            output=output,
        )
    return output


__all__ = [
    "TASK_INCLUDE_LIMIT",
    "image_to_out",
    "list_messages",
]
