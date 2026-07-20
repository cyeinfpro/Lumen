"""Small adapters for creating Canvas-backed media tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import Intent, Role
from lumen_core.models import Conversation, Message, User
from lumen_core.schemas import ChatParamsIn, ImageParamsIn, VideoCreateIn

from ..redis_client import get_redis
from .message_submission import (
    create_assistant_task as _create_assistant_task,
    publish_assistant_task as _publish_assistant_task,
)
from .video.submission import (
    create_video_generation_record as _create_video_generation_record,
    invalidate_video_balance_cache as invalidate_balance_cache,
)
from .video_publish import publish_video_queued


@dataclass
class CanvasImageSubmission:
    generation_ids: list[str]
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    outbox_payloads: list[dict[str, Any]]
    outbox_rows: list[Any]


@dataclass
class CanvasVideoSubmission:
    generation: Any
    publish_payload: dict[str, Any]


_CANVAS_ATTACHMENT_ROLES = frozenset(
    {
        "reference",
        "subject",
        "product",
        "style",
        "edit_target",
        "background",
        "other",
    }
)


def _canvas_message_attachments(
    attachment_ids: list[str],
    metadata: dict[str, Any],
) -> list[dict[str, str]]:
    raw_roles = metadata.get("attachment_roles")
    role_by_image_id: dict[str, str] = {}
    if isinstance(raw_roles, list):
        for item in raw_roles:
            if not isinstance(item, dict):
                continue
            image_id = item.get("image_id")
            role = item.get("role")
            if (
                isinstance(image_id, str)
                and image_id
                and isinstance(role, str)
                and role in _CANVAS_ATTACHMENT_ROLES
            ):
                role_by_image_id.setdefault(image_id, role)
    return [
        {
            "image_id": image_id,
            "role": role_by_image_id.get(image_id, "reference"),
        }
        for image_id in attachment_ids
    ]


async def get_or_create_canvas_conversation(
    db: AsyncSession,
    *,
    user: User,
    canvas: Any,
) -> Conversation:
    """Return the Canvas' single archived conversation."""
    conversation_id = getattr(canvas, "conversation_id", None)
    conv = None
    if conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if conv is None:
        conv = Conversation(
            user_id=user.id,
            title=getattr(canvas, "title", None) or "无限画布",
            archived=True,
            default_params={},
        )
        db.add(conv)
        await db.flush()
        canvas.conversation_id = conv.id

    params = dict(conv.default_params or {})
    params.update(
        {
            "workflow_type": "infinite_canvas",
            "hidden_from_conversations": True,
            "canvas_id": canvas.id,
        }
    )
    conv.default_params = params
    conv.title = getattr(canvas, "title", None) or conv.title
    conv.archived = True
    return conv


async def create_canvas_image_task(
    db: AsyncSession,
    *,
    user: User,
    canvas: Any,
    prompt: str,
    attachment_ids: list[str],
    mask_image_id: str | None,
    image_params: ImageParamsIn,
    idempotency_key: str,
    metadata: dict[str, Any],
) -> CanvasImageSubmission:
    conv = await get_or_create_canvas_conversation(
        db,
        user=user,
        canvas=canvas,
    )
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": prompt,
            "attachments": _canvas_message_attachments(attachment_ids, metadata),
            **metadata,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        user_email=getattr(user, "email", None),
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=Intent.IMAGE_TO_IMAGE if attachment_ids else Intent.TEXT_TO_IMAGE,
        idempotency_key=idempotency_key,
        image_params=image_params,
        chat_params=ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=prompt,
        mask_image_id=mask_image_id,
        request_metadata=metadata,
    )
    return CanvasImageSubmission(
        generation_ids=list(result.generation_ids),
        conversation_id=conv.id,
        user_message_id=user_msg.id,
        assistant_message_id=result.assistant_msg.id,
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )


async def publish_canvas_image_task(
    db: AsyncSession,
    *,
    user_id: str,
    submission: CanvasImageSubmission,
) -> None:
    await _publish_assistant_task(
        db=db,
        redis=get_redis(),
        user_id=user_id,
        conv_id=submission.conversation_id,
        assistant_msg_id=submission.assistant_message_id,
        outbox_payloads=submission.outbox_payloads,
        outbox_rows=submission.outbox_rows,
    )


async def create_canvas_video_task(
    db: AsyncSession,
    *,
    body: VideoCreateIn,
    user: User,
    request: Request,
    metadata: dict[str, Any],
) -> CanvasVideoSubmission:
    publish_payload: dict[str, Any] = {}
    async with db.begin_nested():
        generation = await _create_video_generation_record(
            db,
            body,
            user,
            request=request,
            workflow_metadata=metadata,
            defer_commit=True,
            deferred_publish_payload=publish_payload,
        )
    return CanvasVideoSubmission(
        generation=generation,
        publish_payload=publish_payload,
    )


async def publish_canvas_video_task(*, submission: CanvasVideoSubmission) -> None:
    await invalidate_balance_cache(str(submission.publish_payload["user_id"]))
    await publish_video_queued(submission.publish_payload)
