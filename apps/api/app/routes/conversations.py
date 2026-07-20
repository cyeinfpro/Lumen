# ruff: noqa: F401
"""Conversation HTTP facade.

The route module owns HTTP contracts, dependency injection, and compatibility
wrappers. Query, context, and compaction behavior lives under
``app.services.conversations`` so the domain code does not depend on routes.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, desc, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core
from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    is_user_visible as byok_retention_is_user_visible,
    user_visible_filter as byok_retention_user_visible_filter,
)
from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.context_window import estimate_message_tokens
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    Message,
    SystemPrompt,
)
from lumen_core.schemas import ConversationOut, ConversationPatchIn

from ..audit import hash_email, request_ip_hash, write_audit
from ..arq_pool import get_arq_pool
from ..billing_cache_state import invalidate_balance_cache
from ..byok_service import read_byok_settings_cached, retention_policy_from_settings
from ..db import affected_rows, get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..services.conversation_cleanup import (
    cancel_conversation_memory_extractions as _cancel_conversation_memory_extractions,
    conversation_wallet_exists as _conversation_wallet_exists,
    release_conversation_generation_queue_state as _release_conversation_generation_queue_state,
)
from ..services.conversations.compaction import (
    CIRCUIT_BREAKER_KEY,
    COMPACTION_MESSAGE_LOAD_LIMIT,
    MANUAL_COMPACT_ACTIVE_TTL_SECONDS,
    MANUAL_COMPACT_JOB_TTL_SECONDS,
    MANUAL_COMPACT_RETRY_AFTER_SECONDS,
    build_compact_summary_payload as _build_compact_summary_payload,
    check_manual_compact_cooldown as _check_manual_compact_cooldown,
    classify_compact_failure as _classify_compact_failure,
    compact_conversation as _compact_conversation_service,
    compact_payload_from_job as _compact_payload_from_job,
    compact_pending_payload as _compact_pending_payload,
    enqueue_manual_compact_job as _enqueue_manual_compact_job,
    get_compact_conversation_status as _get_compact_status_service,
    import_worker_context_summary as _service_import_worker_context_summary,
    load_messages_for_compaction as _load_messages_for_compaction,
    manual_compact_active_key as _manual_compact_active_key,
    manual_compact_job_id as _manual_compact_job_id,
    manual_compact_job_key as _manual_compact_job_key,
    redis_get_json as _redis_get_json,
    redis_set_json as _redis_set_json,
    redis_set_nx_json as _redis_set_nx_json,
)
from ..services.conversations.context import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    CONTEXT_RESPONSE_TOKEN_RESERVE,
    CONTEXT_TOTAL_TOKEN_TARGET,
    MANUAL_COMPACT_DEFAULT_COOLDOWN_SECONDS,
    MANUAL_COMPACT_DEFAULT_MIN_INPUT_TOKENS,
    SUMMARY_MIN_RECENT_DEFAULT_MESSAGES,
    SUMMARY_MODEL_DEFAULT,
    SUMMARY_TARGET_DEFAULT_TOKENS,
    circuit_breaker_retry_after as _circuit_breaker_retry_after,
    compaction_source_messages as _compaction_source_messages,
    estimate_context_window as _estimate_context_window_service,
    estimate_messages_tokens as _estimate_messages_tokens,
    estimate_sticky_tokens as _estimate_sticky_tokens,
    first_user_message as _first_user_message,
    load_message_by_id as _load_message_by_id,
    load_prompt_content as _load_prompt_content,
    manual_compact_cooldown_key as _manual_compact_cooldown_key,
    manual_compact_limit_status as _manual_compact_limit_status,
    message_after_summary as _message_after_summary,
    parse_summary_datetime as _parse_summary_datetime,
    simple_structured_system_prompt as _simple_structured_system_prompt,
    setting_float as _setting_float,
    setting_int as _setting_int,
    setting_str as _setting_str,
    sticky_text_from_message as _sticky_text_from_message,
    summary_boundary as _summary_boundary,
    summary_int as _summary_int,
    summary_str as _summary_str,
    summary_updated_at as _summary_updated_at,
    truncate_sticky_text as _truncate_sticky_text,
    with_summary_guardrail as _with_summary_guardrail,
)
from ..services.conversations.contracts import (
    ConversationCompactIn,
    ConversationCompactOut,
    ConversationContextOut,
    ConversationListOut,
    ManualCompactIn,
    MessageListOut,
)
from ..services.conversations.cursor import (
    CURSOR_VERSION,
    coerce_aware as _coerce_aware,
    cursor_field_datetime as _cursor_field_datetime,
    cursor_field_str as _cursor_field_str,
    dec_cursor as _dec_cursor,
    enc_cursor as _enc_cursor,
    exclude_workflow_conversations as _exclude_workflow_conversations,
    message_alive_filters as _message_alive_filters,
)
from ..services.conversations.messages import (
    image_to_out as _image_to_out,
    list_messages as _list_messages_service,
)


router = APIRouter()
TASK_INCLUDE_LIMIT = 100
MANUAL_COMPACT_MIN_TARGET_TOKENS = 300
MANUAL_COMPACT_MAX_TARGET_TOKENS = 4000
MANUAL_COMPACT_EXTRA_INSTRUCTION_MAX_CHARS = 1000
logger = logging.getLogger(__name__)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"error": {"code": "not_found", "message": "conversation not found"}},
    )


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"code": code, "message": message}},
    )


def _trace_id(request: Request | None = None) -> str:
    if request is not None:
        existing = getattr(getattr(request, "state", None), "request_id", None)
        if isinstance(existing, str) and existing:
            return existing
    return uuid.uuid4().hex[:12]


def _service_unavailable(
    reason: str,
    *,
    trace_id: str | None = None,
) -> HTTPException:
    error: dict[str, Any] = {
        "code": "compression_unavailable",
        "message": "compression unavailable",
        "reason": reason,
        "details": {"reason": reason},
    }
    if trace_id:
        error["trace_id"] = trace_id
        error["details"]["trace_id"] = trace_id
    return HTTPException(status_code=503, detail={"error": error})


async def _get_owned_conv(
    db: AsyncSession,
    conv_id: str,
    user_id: str,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conv:
        raise _not_found()
    return conv


async def _get_owned_visible_conv(
    db: AsyncSession,
    conv_id: str,
    user: Any,
) -> Conversation:
    conv = await _get_owned_conv(db, conv_id, user.id)
    if byok_retention_applies_to_user(user):
        policy = retention_policy_from_settings(await read_byok_settings_cached(db))
        if not byok_retention_is_user_visible(
            account_mode=getattr(user, "account_mode", None),
            created_at=conv.last_activity_at,
            policy=policy,
        ):
            raise _not_found()
    return conv


async def _get_owned_conv_for_update(
    db: AsyncSession,
    conv_id: str,
    user_id: str,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation)
            .where(
                Conversation.id == conv_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
            .with_for_update(of=Conversation)
        )
    ).scalar_one_or_none()
    if not conv:
        raise _not_found()
    return conv


async def _soft_delete_conversation_generated_images(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    deleted_at: datetime,
) -> int:
    generation_ids = (
        select(Generation.id)
        .join(Message, Message.id == Generation.message_id)
        .where(
            Message.conversation_id == conv_id,
            Generation.user_id == user_id,
        )
    )
    result = await db.execute(
        update(Image)
        .where(
            Image.user_id == user_id,
            Image.deleted_at.is_(None),
            Image.owner_generation_id.in_(generation_ids),
        )
        .values(deleted_at=deleted_at)
        .execution_options(synchronize_session=False)
    )
    return affected_rows(result)


async def _release_conversation_task_hold(
    db: AsyncSession,
    *,
    user_id: str,
    ref_type: str,
    ref_id: str,
    reason: str,
) -> bool:
    try:
        transaction = await billing_core.release(
            db,
            user_id,
            ref_type=ref_type,
            ref_id=ref_id,
            idempotency_key=f"conversation_delete:{ref_type}:{ref_id}",
            meta={"reason": reason},
        )
    except billing_core.BillingError as exc:
        raise _bad_request(exc.code, exc.message) from exc
    return transaction is not None


async def _cancel_conversation_active_tasks(
    db: AsyncSession,
    *,
    conv_id: str,
    user_id: str,
    canceled_at: datetime,
    account_mode: str = "wallet",
) -> dict[str, Any]:
    message_ids = select(Message.id).where(Message.conversation_id == conv_id)
    generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.message_id.in_(message_ids),
                    Generation.status.in_(
                        [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    completions = list(
        (
            await db.execute(
                select(Completion)
                .where(
                    Completion.user_id == user_id,
                    Completion.message_id.in_(message_ids),
                    Completion.status.in_(
                        [
                            CompletionStatus.QUEUED.value,
                            CompletionStatus.STREAMING.value,
                        ]
                    ),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    queued_generations: list[str] = []
    running_generations: list[str] = []
    streaming_completions: list[str] = []
    active_generation_ids = [generation.id for generation in generations]
    active_completion_ids = [completion.id for completion in completions]
    holds_released = 0
    release_queued_holds = account_mode == "wallet"
    if not release_queued_holds and (
        any(g.status == GenerationStatus.QUEUED.value for g in generations)
        or any(c.status == CompletionStatus.QUEUED.value for c in completions)
    ):
        release_queued_holds = await _conversation_wallet_exists(db, user_id)

    for generation in generations:
        if generation.status == GenerationStatus.QUEUED.value:
            queued_generations.append(generation.id)
            generation.status = GenerationStatus.CANCELED.value
            generation.progress_stage = "finalizing"
            generation.finished_at = canceled_at
            generation.error_code = "cancelled"
            generation.error_message = "conversation deleted"
            if release_queued_holds:
                holds_released += int(
                    await _release_conversation_task_hold(
                        db,
                        user_id=user_id,
                        ref_type="generation",
                        ref_id=billing_core.generation_billing_ref_id(generation),
                        reason="conversation deleted",
                    )
                )
        elif generation.status == GenerationStatus.RUNNING.value:
            running_generations.append(generation.id)
    for completion in completions:
        if completion.status == CompletionStatus.QUEUED.value:
            completion.status = CompletionStatus.CANCELED.value
            completion.progress_stage = "finalizing"
            completion.finished_at = canceled_at
            completion.error_code = "cancelled"
            completion.error_message = "conversation deleted"
            if release_queued_holds:
                holds_released += int(
                    await _release_conversation_task_hold(
                        db,
                        user_id=user_id,
                        ref_type="completion",
                        ref_id=billing_core.completion_billing_ref_id(completion),
                        reason="conversation deleted",
                    )
                )
        elif completion.status == CompletionStatus.STREAMING.value:
            streaming_completions.append(completion.id)
    return {
        "generations_canceled": len(generations),
        "completions_canceled": len(completions),
        "holds_released": holds_released,
        "active_generation_ids": active_generation_ids,
        "active_completion_ids": active_completion_ids,
        "queued_generation_ids": queued_generations,
        "running_generation_ids": running_generations,
        "streaming_completion_ids": streaming_completions,
    }


async def _post_commit_conversation_task_cleanup(
    *,
    user_id: str,
    cleanup: dict[str, Any],
) -> None:
    queued_ids = [
        task_id
        for task_id in cleanup.get("queued_generation_ids", [])
        if isinstance(task_id, str)
    ]
    cancel_ids = [
        task_id
        for task_id in [
            *cleanup.get("running_generation_ids", []),
            *cleanup.get("streaming_completion_ids", []),
        ]
        if isinstance(task_id, str)
    ]
    if not queued_ids and not cancel_ids:
        if cleanup.get("holds_released", 0) > 0:
            try:
                await invalidate_balance_cache(user_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "conversation deletion balance cache invalidation failed "
                    "user=%s err=%s",
                    user_id,
                    exc,
                )
        return
    try:
        redis = get_redis()
        for task_id in queued_ids:
            await _release_conversation_generation_queue_state(redis, task_id)
        for task_id in cancel_ids:
            await redis.set(f"task:{task_id}:cancel", "1", ex=3600)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "conversation deletion cancel signal write failed user=%s err=%s",
            user_id,
            exc,
        )
    if cleanup.get("holds_released", 0) > 0:
        try:
            await invalidate_balance_cache(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "conversation deletion balance cache invalidation failed user=%s err=%s",
                user_id,
                exc,
            )


class ConversationCreateIn(BaseModel):
    title: str = ""
    default_system: str | None = None
    default_params: dict[str, Any] | None = None
    default_system_prompt_id: str | None = None


@router.get("", response_model=ConversationListOut)
async def list_conversations(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    q: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> ConversationListOut:
    retention_filter = None
    if byok_retention_applies_to_user(user):
        policy = retention_policy_from_settings(await read_byok_settings_cached(db))
        retention_filter = byok_retention_user_visible_filter(
            user,
            Conversation.last_activity_at,
            policy=policy,
        )
    stmt = select(Conversation).where(
        Conversation.user_id == user.id,
        Conversation.deleted_at.is_(None),
    )
    if retention_filter is not None:
        stmt = stmt.where(retention_filter)
    stmt = _exclude_workflow_conversations(stmt)
    if q:
        q_trimmed = q.strip()[:200]
        if q_trimmed:
            q_escaped = (
                q_trimmed.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            stmt = stmt.where(Conversation.title.ilike(f"%{q_escaped}%", escape="\\"))
    current = _dec_cursor(cursor)
    if current is not None:
        last_activity = _cursor_field_datetime(current, "la")
        current_id = _cursor_field_str(current, "id")
        stmt = stmt.where(
            or_(
                Conversation.last_activity_at < last_activity,
                and_(
                    Conversation.last_activity_at == last_activity,
                    Conversation.id < current_id,
                ),
            )
        )
    stmt = stmt.order_by(
        desc(Conversation.last_activity_at),
        desc(Conversation.id),
    ).limit(limit + 1)
    rows = (await db.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = _enc_cursor(
            {"la": last.last_activity_at.isoformat(), "id": last.id}
        )
    return ConversationListOut(
        items=[ConversationOut.model_validate(conversation) for conversation in items],
        next_cursor=next_cursor,
    )


@router.post("", response_model=ConversationOut, dependencies=[Depends(verify_csrf)])
async def create_conversation(
    body: ConversationCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    if body.title and len(body.title) > 500:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "invalid_title",
                    "message": "title exceeds 500 characters",
                }
            },
        )
    if body.default_system_prompt_id is not None:
        prompt_exists = (
            await db.execute(
                select(SystemPrompt.id).where(
                    SystemPrompt.id == body.default_system_prompt_id,
                    SystemPrompt.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if prompt_exists is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "system_prompt_not_found",
                        "message": "system prompt not found",
                    }
                },
            )
    conversation = Conversation(
        user_id=user.id,
        title=body.title or "",
        default_system=body.default_system,
        default_system_prompt_id=body.default_system_prompt_id,
        default_params=body.default_params or {},
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.get("/{conv_id}", response_model=ConversationOut)
async def get_conversation(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    conversation = await _get_owned_visible_conv(db, conv_id, user)
    return ConversationOut.model_validate(conversation)


@router.patch(
    "/{conv_id}",
    response_model=ConversationOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_conversation(
    conv_id: str,
    body: ConversationPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationOut:
    conversation = await _get_owned_visible_conv(db, conv_id, user)
    if body.title is not None:
        if len(body.title) > 500:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": {
                        "code": "invalid_title",
                        "message": "title exceeds 500 characters",
                    }
                },
            )
        conversation.title = body.title
    if body.pinned is not None:
        conversation.pinned = body.pinned
    if body.archived is not None:
        conversation.archived = body.archived
    if body.default_params is not None:
        conversation.default_params = body.default_params
    if "default_system" in body.model_fields_set:
        conversation.default_system = body.default_system
    if "default_system_prompt_id" in body.model_fields_set:
        if body.default_system_prompt_id is not None:
            prompt_exists = (
                await db.execute(
                    select(SystemPrompt.id).where(
                        SystemPrompt.id == body.default_system_prompt_id,
                        SystemPrompt.user_id == user.id,
                    )
                )
            ).scalar_one_or_none()
            if prompt_exists is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": {
                            "code": "system_prompt_not_found",
                            "message": "system prompt not found",
                        }
                    },
                )
        conversation.default_system_prompt_id = body.default_system_prompt_id
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.delete("/{conv_id}", dependencies=[Depends(verify_csrf)])
async def delete_conversation(
    conv_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    conversation = await _get_owned_conv_for_update(db, conv_id, user.id)
    now = datetime.now(timezone.utc)
    conversation.deleted_at = now
    deleted_images = await _soft_delete_conversation_generated_images(
        db,
        conv_id=conversation.id,
        user_id=user.id,
        deleted_at=now,
    )
    task_cleanup = await _cancel_conversation_active_tasks(
        db,
        conv_id=conversation.id,
        user_id=user.id,
        canceled_at=now,
        account_mode=getattr(user, "account_mode", "wallet"),
    )
    memory_extractions_canceled = await _cancel_conversation_memory_extractions(
        db,
        conv_id=conversation.id,
        user_id=user.id,
        canceled_at=now,
    )
    await write_audit(
        db,
        event_type="conversation.delete",
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details={
            "conversation_id": conversation.id,
            "images_deleted": deleted_images,
            "generations_canceled": task_cleanup["generations_canceled"],
            "completions_canceled": task_cleanup["completions_canceled"],
            "memory_extractions_canceled": memory_extractions_canceled,
        },
        autocommit=False,
    )
    await db.commit()
    await _post_commit_conversation_task_cleanup(
        user_id=user.id,
        cleanup=task_cleanup,
    )
    return {"ok": True}


@router.get("/{conv_id}/messages", response_model=MessageListOut)
async def list_messages(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    since: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    include: str | None = Query(
        default=None,
        description='逗号分隔；含 "tasks" 时附带返回 generations/completions/images',
    ),
) -> MessageListOut:
    return await _list_messages_service(
        conv_id,
        user,
        db,
        cursor=cursor,
        since=since,
        limit=limit,
        include=include,
        get_owned_visible_conv=_get_owned_visible_conv,
    )


async def _estimate_context_window(
    db: AsyncSession,
    *,
    conv: Conversation,
    user_id: str,
    user_default_prompt_id: str | None,
    redis: Any | None = None,
) -> ConversationContextOut:
    return await _estimate_context_window_service(
        db,
        conv=conv,
        user_id=user_id,
        user_default_prompt_id=user_default_prompt_id,
        redis=redis,
    )


@router.get("/{conv_id}/context", response_model=ConversationContextOut)
async def get_conversation_context(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ConversationContextOut:
    conversation = await _get_owned_visible_conv(db, conv_id, user)
    return await _estimate_context_window(
        db,
        conv=conversation,
        user_id=user.id,
        user_default_prompt_id=user.default_system_prompt_id,
        redis=get_redis(),
    )


def _import_worker_context_summary() -> Any | None:
    return _service_import_worker_context_summary()


def _import_ensure_context_summary() -> Any | None:
    module = _import_worker_context_summary()
    return getattr(module, "ensure_context_summary", None) if module else None


@router.post("/{conv_id}/compact", dependencies=[Depends(verify_csrf)])
async def compact_conversation(
    conv_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    body: ManualCompactIn | None = None,
) -> dict[str, Any]:
    """Manually compact a conversation's history.

    Why: gives the user an escape hatch when auto-compaction has not fired yet
    but the context window is already feeling full. Boundary is the latest live
    message; ensure_context_summary owns the cooldown / lock / circuit logic.
    """
    return await _compact_conversation_service(
        conv_id,
        request,
        user,
        db,
        body,
        get_redis_fn=get_redis,
        get_arq_pool_fn=get_arq_pool,
        import_ensure_fn=_import_ensure_context_summary,
        get_owned_conv_fn=_get_owned_conv,
    )


@router.get("/{conv_id}/compact/status")
async def get_compact_conversation_status(
    conv_id: str,
    job_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    return await _get_compact_status_service(
        conv_id,
        job_id,
        request,
        user,
        db,
        get_redis_fn=get_redis,
        get_owned_visible_conv_fn=_get_owned_visible_conv,
    )
