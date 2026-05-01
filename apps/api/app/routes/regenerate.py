"""重新生成（V1.0 收尾）。

POST /conversations/{cid}/messages/{mid}/regenerate

把指定 assistant message 标 canceled，找到它的 parent user message 作为输入，
按 RegenerateIn.intent 用与 send_message 完全一致的助手任务装配路径再跑一遍。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    Intent,
    MessageStatus,
    Role,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    Message,
)
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    RegenerateIn,
    RegenerateOut,
)
from lumen_core.runtime_settings import get_spec

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from .messages import (
    _DEFAULT_IMAGE_OUTPUT_FORMAT,
    _await_post_commit_publish,
    _create_assistant_task,
    _message_alive_filters,
    _publish_assistant_task,
    _publish_message_appended,
    resolve_system_prompt_for_message,
)


router = APIRouter()


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


_INTENT_BY_STR: dict[str, Intent] = {
    "chat": Intent.CHAT,
    "vision_qa": Intent.VISION_QA,
    "text_to_image": Intent.TEXT_TO_IMAGE,
    "image_to_image": Intent.IMAGE_TO_IMAGE,
}
_IMAGE_RENDER_QUALITY_VALUES = {"auto", "low", "medium", "high"}
_IMAGE_OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
_IMAGE_BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
_IMAGE_MODERATION_VALUES = {"auto", "low"}


async def _default_image_output_format(db: AsyncSession) -> str:
    spec = get_spec("image.output_format")
    if spec is not None:
        raw_default_format = await get_setting(db, spec)
        if raw_default_format in _IMAGE_OUTPUT_FORMAT_VALUES:
            return raw_default_format
    return _DEFAULT_IMAGE_OUTPUT_FORMAT


async def _lookup_idempotent_regenerate(
    db: AsyncSession, user_id: str, conv_id: str, idempotency_key: str
) -> RegenerateOut | None:
    alive_filters = _message_alive_filters()
    comp_hit = (
        await db.execute(
            select(Completion)
            .join(Message, Message.id == Completion.message_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Completion.user_id == user_id,
                Completion.idempotency_key == idempotency_key,
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
                Generation.idempotency_key == idempotency_key,
                Message.conversation_id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                *alive_filters,
            )
        )
    ).scalar_one_or_none()
    if not comp_hit and not gen_anchor:
        return None
    anchor_msg_id = comp_hit.message_id if comp_hit else gen_anchor.message_id
    gen_hits: list[Generation] = []
    if gen_anchor is not None:
        gen_hits = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id == anchor_msg_id,
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
    return RegenerateOut(
        assistant_message_id=anchor_msg_id,
        completion_id=comp_hit.id if comp_hit else None,
        generation_ids=[g.id for g in gen_hits],
    )


def _chat_params_from_user_content(content: dict[str, Any]) -> ChatParamsIn:
    effort = content.get("reasoning_effort")
    if effort not in ("none", "minimal", "low", "medium", "high", "xhigh"):
        effort = None
    vector_store_ids = content.get("vector_store_ids")
    if not isinstance(vector_store_ids, list):
        vector_store_ids = []
    return ChatParamsIn(
        reasoning_effort=effort,
        fast=content.get("fast") is True,
        web_search=content.get("web_search") is True,
        file_search=content.get("file_search") is True,
        vector_store_ids=[v for v in vector_store_ids if isinstance(v, str)],
        code_interpreter=content.get("code_interpreter") is True,
        image_generation=content.get("image_generation") is True,
    )


def _str_option(value: Any, allowed: set[str], default: str | None) -> str | None:
    return value if isinstance(value, str) and value in allowed else default


def _compression_option(value: Any) -> int | None:
    if value is None:
        return None
    try:
        compression = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= compression <= 100:
        return compression
    return None


async def _image_params_from_target(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    target_msg_id: str,
) -> ImageParamsIn:
    gens = (
        await db.execute(
            select(Generation)
            .join(Message, Message.id == Generation.message_id)
            .where(
                Generation.user_id == user_id,
                Generation.message_id == target_msg_id,
                Message.conversation_id == conv_id,
                *_message_alive_filters(),
            )
            .order_by(Generation.created_at.asc(), Generation.id.asc())
        )
    ).scalars().all()
    if not gens:
        return ImageParamsIn()
    first = gens[0]
    fixed_size = first.size_requested if "x" in (first.size_requested or "") else None
    upstream_request = first.upstream_request if isinstance(first.upstream_request, dict) else {}
    output_format = None
    output_compression = None
    output_format_source = upstream_request.get("output_format_source")
    raw_output_format = upstream_request.get("output_format")
    if output_format_source == "request" or (
        output_format_source is None and raw_output_format == "webp"
    ):
        output_format = _str_option(
            raw_output_format,
            _IMAGE_OUTPUT_FORMAT_VALUES,
            None,
        )
        output_compression = _compression_option(
            upstream_request.get("output_compression")
        )
    try:
        return ImageParamsIn(
            aspect_ratio=first.aspect_ratio,
            size_mode="fixed" if fixed_size else "auto",
            fixed_size=fixed_size,
            count=max(1, min(16, len(gens))),
            fast=bool(upstream_request.get("fast", False)),
            render_quality=_str_option(
                upstream_request.get("render_quality"),
                _IMAGE_RENDER_QUALITY_VALUES,
                "auto",
            ),
            output_format=output_format,
            output_compression=output_compression,
            background=_str_option(
                upstream_request.get("background"),
                _IMAGE_BACKGROUND_VALUES,
                "auto",
            ),
            moderation=_str_option(
                upstream_request.get("moderation"),
                _IMAGE_MODERATION_VALUES,
                "low",
            ),
        )
    except Exception:
        return ImageParamsIn()


@router.post(
    "/conversations/{conv_id}/messages/{message_id}/regenerate",
    response_model=RegenerateOut,
    dependencies=[Depends(verify_csrf)],
)
async def regenerate_message(
    conv_id: str,
    message_id: str,
    body: RegenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RegenerateOut:
    redis = get_redis()
    await MESSAGES_LIMITER.check(redis, f"rl:msg:{user.id}")

    # ---- ownership: conversation ----
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
        raise _http("not_found", "conversation not found", 404)

    # ---- target assistant message must belong to this conv & be assistant role ----
    target = (
        await db.execute(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conv.id,
                *_message_alive_filters(),
            )
        )
    ).scalar_one_or_none()
    if target is None or target.role != Role.ASSISTANT.value:
        raise _http("not_found", "assistant message not found", 404)

    # ---- find parent user message (the regen input) ----
    user_msg: Message | None = None
    if target.parent_message_id:
        user_msg = (
            await db.execute(
                select(Message).where(
                    Message.id == target.parent_message_id,
                    Message.conversation_id == conv.id,
                    Message.role == Role.USER.value,
                    *_message_alive_filters(),
                )
            )
        ).scalar_one_or_none()
    if user_msg is None:
        raise _http(
            "user_message_missing",
            "parent user message not found; cannot regenerate",
            422,
        )

    # ---- idempotency short-circuit ---------------------------------------
    # If the same idempotency_key was already used by this user, return its result.
    prior = await _lookup_idempotent_regenerate(
        db, user.id, conv.id, body.idempotency_key
    )
    if prior is not None:
        return prior

    intent = _INTENT_BY_STR.get(body.intent)
    if intent is None:
        raise _http("invalid_intent", "invalid regenerate intent", 422)

    # ---- vision/i2i sanity: pull attachments from user message ----
    user_content = user_msg.content or {}
    attachment_ids: list[str] = []
    for att in user_content.get("attachments") or []:
        if isinstance(att, dict) and att.get("image_id"):
            attachment_ids.append(att["image_id"])
    if attachment_ids:
        rows = (
            await db.execute(
                select(Image.id).where(
                    Image.id.in_(attachment_ids),
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if len(rows) != len(attachment_ids):
            raise _http(
                "invalid_attachment",
                "one or more attachment images were deleted",
                400,
            )

    if intent == Intent.IMAGE_TO_IMAGE and not attachment_ids:
        raise _http(
            "missing_reference_image",
            "image_to_image requires the original user message to have at least one "
            "reference image",
            400,
        )

    text = user_content.get("text") or ""

    # ---- transactional: cancel old assistant + sub-tasks, then create new ---
    now = datetime.now(timezone.utc)

    # Cancel any in-flight generations bound to the old assistant message.
    await db.execute(
        update(Generation)
        .where(
            Generation.message_id == target.id,
            Generation.status.in_(
                (
                    GenerationStatus.QUEUED.value,
                    GenerationStatus.RUNNING.value,
                )
            ),
        )
        .values(
            status=GenerationStatus.CANCELED.value,
            finished_at=now,
        )
    )
    # And any in-flight completion.
    await db.execute(
        update(Completion)
        .where(
            Completion.message_id == target.id,
            Completion.status.in_(
                (
                    CompletionStatus.QUEUED.value,
                    CompletionStatus.STREAMING.value,
                )
            ),
        )
        .values(
            status=CompletionStatus.CANCELED.value,
            finished_at=now,
        )
    )

    # Mark old assistant message canceled (don't delete — keep history).
    target.status = MessageStatus.CANCELED.value

    # Reuse the same helper used by POST /messages so behaviour is bit-identical.
    image_params = await _image_params_from_target(
        db, user_id=user.id, conv_id=conv.id, target_msg_id=target.id
    )
    default_image_output_format = (
        await _default_image_output_format(db)
        if intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE)
        else _DEFAULT_IMAGE_OUTPUT_FORMAT
    )
    chat_params = _chat_params_from_user_content(user_content)
    system_prompt = None
    if intent in (Intent.CHAT, Intent.VISION_QA):
        system_prompt = (
            await db.execute(
                select(Completion.system_prompt)
                .where(
                    Completion.user_id == user.id,
                    Completion.message_id == target.id,
                )
                .order_by(Completion.created_at.desc(), Completion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if system_prompt is None:
            system_prompt = await resolve_system_prompt_for_message(
                db,
                user_id=user.id,
                default_system_prompt_id=user.default_system_prompt_id,
                conv=conv,
                explicit_prompt=chat_params.system_prompt,
            )

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=body.idempotency_key,
        image_params=image_params,
        chat_params=chat_params,
        system_prompt=system_prompt,
        attachment_ids=attachment_ids,
        text=text,
        default_image_output_format=default_image_output_format,
    )

    conv.last_activity_at = now
    try:
        await db.commit()
    except IntegrityError:
        # Why: concurrent regenerate with same idempotency_key won the race;
        # rely on the unique constraint and return prior result.
        await db.rollback()
        prior = await _lookup_idempotent_regenerate(
            db, user.id, conv.id, body.idempotency_key
        )
        if prior is not None:
            return prior
        raise _http("idempotency_conflict", "idempotency_key conflict", 409)
    await db.refresh(result.assistant_msg)

    await _await_post_commit_publish(
        "message_appended",
        _publish_message_appended(
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            message_ids=[result.assistant_msg.id],
        ),
        user_id=user.id,
        conv_id=conv_id,
    )
    await _await_post_commit_publish(
        "assistant_task",
        _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user.id,
            conv_id=conv_id,
            assistant_msg_id=result.assistant_msg.id,
            outbox_payloads=result.outbox_payloads,
            outbox_rows=result.outbox_rows,
        ),
        user_id=user.id,
        conv_id=conv_id,
        assistant_msg_id=result.assistant_msg.id,
    )

    return RegenerateOut(
        assistant_message_id=result.assistant_msg.id,
        completion_id=result.completion_id,
        generation_ids=result.generation_ids,
    )


__all__ = ["router"]
