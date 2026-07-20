from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import and_, or_, select

from lumen_core.context_window import estimate_message_tokens
from lumen_core.models import Image, Message

from .common import LoadedSummaryMessages
from .messages import uncaptioned_image_ids


async def message_position(
    session: Any,
    message_id: str,
) -> tuple[Any, str] | None:
    msg = await session.get(Message, message_id)
    if msg is None:
        return None
    return msg.created_at, msg.id


async def load_messages_for_summary(
    session: Any,
    conv_id: str,
    after_message_id: str | None,
    before_boundary_id: str,
    *,
    position_loader: Callable[[Any, str], Awaitable[tuple[Any, str] | None]],
) -> LoadedSummaryMessages:
    """Load messages in (after_message_id, before_boundary_id] oldest first."""
    before_pos = await position_loader(session, before_boundary_id)
    if before_pos is None:
        return LoadedSummaryMessages([], 0, 0, 0)
    before_created_at, before_id = before_pos

    conditions: list[Any] = [
        Message.conversation_id == conv_id,
        Message.deleted_at.is_(None),
        or_(
            Message.created_at < before_created_at,
            and_(Message.created_at == before_created_at, Message.id <= before_id),
        ),
    ]
    if after_message_id:
        after_pos = await position_loader(session, after_message_id)
        if after_pos is not None:
            after_created_at, after_id = after_pos
            conditions.append(
                or_(
                    Message.created_at > after_created_at,
                    and_(Message.created_at == after_created_at, Message.id > after_id),
                )
            )

    rows = list(
        (
            await session.execute(
                select(Message)
                .where(*conditions)
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
        ).scalars()
    )
    image_caption_count = sum(_message_caption_count(msg) for msg in rows)
    token_estimate = sum(estimate_message_tokens(msg.role, msg.content) for msg in rows)
    return LoadedSummaryMessages(
        rows,
        len(rows),
        token_estimate,
        image_caption_count,
    )


def _message_caption_count(message: Message) -> int:
    content = message.content if isinstance(message.content, dict) else {}
    return sum(
        1
        for attachment in content.get("attachments") or []
        if isinstance(attachment, dict)
        and attachment.get("image_id")
        and attachment.get("caption")
    )


async def caption_images_for_summary(
    session: Any,
    messages: Sequence[Message],
    settings: Any,
    *,
    settings_int: Callable[[Any, str, int], int],
    settings_str: Callable[[Any, str, str], str],
    release_business_transaction: Callable[[Any], Awaitable[None]],
    logger: logging.Logger,
) -> dict[str, str]:
    if settings_int(settings, "context.image_caption_enabled", 1) <= 0:
        return {}
    image_ids = uncaptioned_image_ids(messages)
    if not image_ids:
        return {}

    try:
        rows = list(
            (
                await session.execute(
                    select(Image).where(
                        Image.id.in_(image_ids),
                        Image.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("context_summary.image_caption_load_failed err=%r", exc)
        return {}
    if not rows:
        return {}

    await release_business_transaction(session)
    try:
        from .. import context_image_caption

        model = settings_str(
            settings,
            "context.image_caption_model",
            "gpt-5.4-mini",
        )
        return await context_image_caption.batch_caption_images(
            session,
            rows,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("context_summary.image_caption_failed err=%s", exc)
        return {}
