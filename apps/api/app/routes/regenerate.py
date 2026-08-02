"""重新生成（V1.0 收尾）。

POST /conversations/{cid}/messages/{mid}/regenerate

把指定 assistant message 标 canceled，找到它的 parent user message 作为输入，
按 RegenerateIn.intent 用与 send_message 完全一致的助手任务装配路径再跑一遍。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.constants import (
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

from ..billing_cache_state import invalidate_balance_cache
from ..db import get_db
from ..deps import CurrentUser, durable_session_id, verify_csrf
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from ..services.active_user import (
    ActiveUserFenceError,
    active_user_fence_http_error,
    lock_active_user,
)
from ..services.generation_queue import (
    release_generation_queue_state,
)
from ..services.regenerate_task_cleanup import (
    cancel_regenerate_target_active_tasks as _cancel_regenerate_target_active_tasks_service,
    post_commit_regenerate_cancel_cleanup as _post_commit_regenerate_cancel_cleanup_service,
)
from . import regenerate_options as _regenerate_options
from .messages import (
    DEFAULT_IMAGE_OUTPUT_FORMAT as _DEFAULT_IMAGE_OUTPUT_FORMAT,
    await_post_commit_publishes as _await_post_commit_publishes,
    create_assistant_task as _create_assistant_task,
    idempotency_lookup_keys as _idempotency_lookup_keys,
    message_alive_filters as _message_alive_filters,
    publish_assistant_task as _publish_assistant_task,
    publish_message_appended as _publish_message_appended,
    resolve_system_prompt_for_message,
)

_release_generation_queue_state = release_generation_queue_state
_chat_params_from_user_content = _regenerate_options.chat_params_from_user_content
_str_option = _regenerate_options.str_option
_bool_option = _regenerate_options.bool_option
_compression_option = _regenerate_options.compression_option
_IMAGE_RENDER_QUALITY_VALUES = _regenerate_options.IMAGE_RENDER_QUALITY_VALUES
_IMAGE_OUTPUT_FORMAT_VALUES = _regenerate_options.IMAGE_OUTPUT_FORMAT_VALUES
_IMAGE_BACKGROUND_VALUES = _regenerate_options.IMAGE_BACKGROUND_VALUES
_IMAGE_MODERATION_VALUES = _regenerate_options.IMAGE_MODERATION_VALUES


router = APIRouter()
logger = logging.getLogger(__name__)


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


async def _release_regenerate_cancel_hold(
    db: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
) -> bool:
    try:
        tx = await billing_core.release(
            db,
            user_id,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=f"regenerate_cancel:{ref_type}:{ref_id}",
            meta={"reason": "regenerate_cancel"},
        )
    except billing_core.BillingError as exc:
        raise _http(exc.code, exc.message, exc.status_code) from exc
    return tx is not None


async def _regenerate_wallet_exists(db: AsyncSession, user_id: str) -> bool:
    wallet = await billing_core.get_wallet(db, user_id, lock=False, create=False)
    return wallet is not None


async def _cancel_regenerate_target_active_tasks(
    db: AsyncSession,
    *,
    target_msg_id: str,
    user_id: str,
    canceled_at: datetime,
    account_mode: str,
    queue_redis: Any | None = None,
) -> dict[str, Any]:
    return await _cancel_regenerate_target_active_tasks_service(
        db,
        target_msg_id=target_msg_id,
        user_id=user_id,
        canceled_at=canceled_at,
        account_mode=account_mode,
        queue_redis=queue_redis,
        release_hold=_release_regenerate_cancel_hold,
        wallet_exists=_regenerate_wallet_exists,
        logger=logger,
    )


async def _post_commit_regenerate_cancel_cleanup(
    redis: Any,
    *,
    user_id: str,
    cleanup: dict[str, Any],
) -> None:
    await _post_commit_regenerate_cancel_cleanup_service(
        redis,
        user_id=user_id,
        cleanup=cleanup,
        release_queue_state=_release_generation_queue_state,
        invalidate_balance=invalidate_balance_cache,
        logger=logger,
    )


_INTENT_BY_STR = MappingProxyType(
    {
        "chat": Intent.CHAT,
        "vision_qa": Intent.VISION_QA,
        "text_to_image": Intent.TEXT_TO_IMAGE,
        "image_to_image": Intent.IMAGE_TO_IMAGE,
    }
)


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
    lookup_keys = _idempotency_lookup_keys(conv_id, idempotency_key)
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
    if comp_hit is not None:
        anchor_msg_id = comp_hit.message_id
    elif gen_anchor is not None:
        anchor_msg_id = gen_anchor.message_id
    else:
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
    return RegenerateOut(
        assistant_message_id=anchor_msg_id,
        completion_id=comp_hit.id if comp_hit else None,
        generation_ids=[g.id for g in gen_hits],
    )


async def _ordered_target_generations(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    target_msg_id: str,
) -> list[Generation]:
    """Canonical ``first generation`` selector for a regenerate target.

    Both ``_image_params_from_target`` (size/format) and
    ``_mask_image_id_from_target`` (mask pairing) MUST use this same ordering
    so size + mask come from the same row. See review note REGEN-01.
    """
    return list(
        (
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
        )
        .scalars()
        .all()
    )


async def _image_params_from_target(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    target_msg_id: str,
) -> ImageParamsIn:
    gens = await _ordered_target_generations(
        db, user_id=user_id, conv_id=conv_id, target_msg_id=target_msg_id
    )
    if not gens:
        return ImageParamsIn()
    first = gens[0]
    fixed_size = first.size_requested if "x" in (first.size_requested or "") else None
    upstream_request = (
        first.upstream_request if isinstance(first.upstream_request, dict) else {}
    )
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
        return ImageParamsIn.model_validate(
            {
                "aspect_ratio": first.aspect_ratio,
                "size_mode": "fixed" if fixed_size else "auto",
                "fixed_size": fixed_size,
                "count": max(1, min(16, len(gens))),
                "quality": _str_option(
                    upstream_request.get("billing_tier"),
                    {"1k", "2k", "4k"},
                    None,
                ),
                "fast": _bool_option(upstream_request.get("fast"), False),
                "render_quality": _str_option(
                    upstream_request.get("render_quality"),
                    _IMAGE_RENDER_QUALITY_VALUES,
                    "auto",
                ),
                "output_format": output_format,
                "output_compression": output_compression,
                "background": _str_option(
                    upstream_request.get("background"),
                    _IMAGE_BACKGROUND_VALUES,
                    "auto",
                ),
                "moderation": _str_option(
                    upstream_request.get("moderation"),
                    _IMAGE_MODERATION_VALUES,
                    "low",
                ),
            }
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "regenerate image params fallback target_msg=%s err=%s",
            target_msg_id,
            exc,
        )
        return ImageParamsIn()


async def _mask_image_id_from_target(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    target_msg_id: str,
) -> str | None:
    # Why: must read the mask from the SAME row that _image_params_from_target
    # picks (gens[0]). Picking the first *mask-bearing* row could pair its
    # mask with the first row's size/format and inpaint at the wrong
    # resolution. A single i2i target's gens all share one mask, so reading
    # gens[0].mask_image_id is sufficient.
    gens = await _ordered_target_generations(
        db, user_id=user_id, conv_id=conv_id, target_msg_id=target_msg_id
    )
    if not gens:
        return None
    mask_id = gens[0].mask_image_id
    if not mask_id:
        return None
    alive = (
        await db.execute(
            select(Image.id).where(
                Image.id == mask_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if alive is None:
        raise _http("mask_not_found", "original mask image was deleted", 404)
    return mask_id


async def _regenerate_messages(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    message_id: str,
) -> tuple[Conversation, Message, Message]:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise _http("not_found", "conversation not found", 404)

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

    user_msg = None
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
    return conv, target, user_msg


def _attachment_ids_from_content(user_content: dict[str, Any]) -> list[str]:
    return [
        attachment["image_id"]
        for attachment in user_content.get("attachments") or []
        if isinstance(attachment, dict) and attachment.get("image_id")
    ]


async def _validated_attachment_ids(
    db: AsyncSession,
    *,
    user_id: str,
    user_content: dict[str, Any],
    intent: Intent,
) -> list[str]:
    attachment_ids = _attachment_ids_from_content(user_content)
    if attachment_ids:
        rows = (
            (
                await db.execute(
                    select(Image.id).where(
                        Image.id.in_(attachment_ids),
                        Image.user_id == user_id,
                        Image.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
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
    return attachment_ids


async def _regenerate_system_prompt(
    db: AsyncSession,
    *,
    user: CurrentUser,
    conv: Conversation,
    target: Message,
    intent: Intent,
    chat_params: ChatParamsIn,
) -> str | None:
    if intent not in (Intent.CHAT, Intent.VISION_QA):
        return None
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
    if system_prompt is not None:
        return system_prompt
    return await resolve_system_prompt_for_message(
        db,
        user_id=user.id,
        default_system_prompt_id=user.default_system_prompt_id,
        conv=conv,
        explicit_prompt=chat_params.system_prompt,
    )


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
    request: Request = None,
) -> RegenerateOut:
    redis = get_redis()
    await MESSAGES_LIMITER.check(redis, f"rl:msg:{user.id}")

    conv, target, user_msg = await _regenerate_messages(
        db,
        user_id=user.id,
        conv_id=conv_id,
        message_id=message_id,
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
    attachment_ids = await _validated_attachment_ids(
        db,
        user_id=user.id,
        user_content=user_content,
        intent=intent,
    )

    text = user_content.get("text") or ""

    # ---- transactional: cancel old assistant + sub-tasks, then create new ---
    try:
        session_id = durable_session_id(request)
        if session_id:
            await lock_active_user(db, user.id, session_id=session_id)
        else:
            await lock_active_user(db, user.id)
    except ActiveUserFenceError as exc:
        raise active_user_fence_http_error(exc) from exc
    now = datetime.now(timezone.utc)
    cleanup = await _cancel_regenerate_target_active_tasks(
        db,
        target_msg_id=target.id,
        user_id=user.id,
        canceled_at=now,
        account_mode=getattr(user, "account_mode", "wallet"),
        queue_redis=redis,
    )

    # Mark old assistant message canceled (don't delete — keep history).
    target.status = MessageStatus.CANCELED.value

    # Reuse the same helper used by POST /messages so behaviour is bit-identical.
    image_params = await _image_params_from_target(
        db, user_id=user.id, conv_id=conv.id, target_msg_id=target.id
    )
    mask_image_id = (
        await _mask_image_id_from_target(
            db,
            user_id=user.id,
            conv_id=conv.id,
            target_msg_id=target.id,
        )
        if intent == Intent.IMAGE_TO_IMAGE
        else None
    )
    default_image_output_format = (
        await _default_image_output_format(db)
        if intent in (Intent.TEXT_TO_IMAGE, Intent.IMAGE_TO_IMAGE)
        else _DEFAULT_IMAGE_OUTPUT_FORMAT
    )
    chat_params = _chat_params_from_user_content(user_content)
    system_prompt = await _regenerate_system_prompt(
        db,
        user=user,
        conv=conv,
        target=target,
        intent=intent,
        chat_params=chat_params,
    )

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
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
        mask_image_id=mask_image_id,
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
    await _post_commit_regenerate_cancel_cleanup(
        redis,
        user_id=user.id,
        cleanup=cleanup,
    )

    await _await_post_commit_publishes(
        (
            "message_appended",
            _publish_message_appended(
                redis=redis,
                user_id=user.id,
                conv_id=conv_id,
                message_ids=[result.assistant_msg.id],
            ),
            None,
        ),
        (
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
            result.assistant_msg.id,
        ),
        user_id=user.id,
        conv_id=conv_id,
    )

    return RegenerateOut(
        assistant_message_id=result.assistant_msg.id,
        completion_id=result.completion_id,
        generation_ids=result.generation_ids,
    )


__all__ = ["router"]
