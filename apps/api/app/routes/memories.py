# ruff: noqa: F401
"""User-owned account memory APIs and compatibility facade."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    Completion,
    Conversation,
    MemoryAudit,
    Message,
    User,
    UserMemory,
    UserMemoryScope,
    UserMemoryStaging,
)

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..runtime_settings import embedding_provider_available
from ..sse_publish import publish_sse_event
from .memory_parts import account as _account
from .memory_parts import common as _common
from .memory_parts import conversations as _conversations
from .memory_parts import scopes as _scopes
from .memory_parts import undo as _undo
from .memory_parts.account import AccountMemoryDependencies
from .memory_parts.contracts import (
    ConversationActiveScopeIn,
    ConversationMemoryDisabledIn,
    MemoryAuditOut,
    MemoryConfirmIn,
    MemoryCreateIn,
    MemoryListOut,
    MemoryOut,
    MemoryPatchIn,
    MemoryScopeCreateIn,
    MemoryScopeOut,
    MemoryScopePatchIn,
    MemorySettingsOut,
    MemorySettingsPatchIn,
    MemoryStagingListOut,
    MemoryStagingOut,
    MemoryStagingPatchIn,
    MemoryTimelineOut,
    MemoryType,
    MemoryUndoIn,
    OnboardingSeenPatchIn,
    UsedMemoriesOut,
)
from .memory_parts.conversations import ConversationMemoryDependencies
from .memory_parts.scopes import MemoryScopeDependencies
from .memory_parts.undo import UndoDependencies


router = APIRouter(tags=["memories"])
logger = logging.getLogger(__name__)
_UNDO_TTL_SECONDS = 300
_UNDO_CLAIM_TTL_SECONDS = 300
_STAGING_TTL_DAYS = 7


@runtime_checkable
class _RowcountResult(Protocol):
    rowcount: int | None


def _dml_rowcount(result: object) -> int | None:
    if not isinstance(result, _RowcountResult):
        raise TypeError("expected a DML result with rowcount")
    return result.rowcount


def _http(code: str, msg: str, http: int = 400) -> HTTPException:
    return _common.http_error(code, msg, http)


async def _filter_owned_used_memory_payload(
    db: AsyncSession,
    *,
    user_id: str,
    ids: object,
    summary: object,
) -> UsedMemoriesOut:
    return await _common.filter_owned_used_memory_payload(
        db,
        user_id=user_id,
        ids=ids,
        summary=summary,
    )


def _memory_to_out(memory: UserMemory) -> MemoryOut:
    return _common.memory_to_out(memory)


def _staging_to_out(staging: UserMemoryStaging) -> MemoryStagingOut:
    return _common.staging_to_out(staging)


async def _default_scope(db: AsyncSession, user_id: str) -> UserMemoryScope:
    return await _common.default_scope(db, user_id)


async def _owned_scope(
    db: AsyncSession, user_id: str, scope_id: str | None
) -> UserMemoryScope:
    return await _common.owned_scope(
        db,
        user_id,
        scope_id,
        default_scope_fn=_default_scope,
        http=_http,
    )


async def _owned_memory(db: AsyncSession, user_id: str, memory_id: str) -> UserMemory:
    return await _common.owned_memory(db, user_id, memory_id, http=_http)


async def _owned_staging(
    db: AsyncSession, user_id: str, staging_id: str
) -> UserMemoryStaging:
    return await _common.owned_staging(db, user_id, staging_id, http=_http)


async def _enqueue_memory_reembed(target: str, row_id: str) -> None:
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job("memory_reembed", target, row_id)
    except Exception:
        logger.warning(
            "memory_reembed enqueue failed target=%s id=%s",
            target,
            row_id,
            exc_info=True,
        )


def _audit(
    *,
    user_id: str,
    event_type: str,
    memory_id: str | None = None,
    staging_id: str | None = None,
    old_content: str | None = None,
    new_content: str | None = None,
    source_message_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> MemoryAudit:
    return _common.audit(
        user_id=user_id,
        event_type=event_type,
        memory_id=memory_id,
        staging_id=staging_id,
        old_content=old_content,
        new_content=new_content,
        source_message_id=source_message_id,
        details=details,
    )


def _undo_token_claim_key(undo_token: str) -> str:
    return _undo.undo_token_claim_key(undo_token)


async def _undo_token_consumed(
    db: AsyncSession,
    *,
    user_id: str,
    undo_token: str,
) -> bool:
    return await _undo.undo_token_consumed(
        db,
        user_id=user_id,
        undo_token=undo_token,
    )


async def _claim_undo_token(redis: Any, undo_token: str, *, owner: str) -> bool:
    return await _undo.claim_undo_token(
        redis,
        undo_token,
        owner=owner,
        claim_ttl_seconds=_UNDO_CLAIM_TTL_SECONDS,
    )


async def _release_undo_token_claim(redis: Any, undo_token: str, *, owner: str) -> None:
    await _undo.release_undo_token_claim(
        redis,
        undo_token,
        owner=owner,
        logger=logger,
    )


async def _undo_payload(
    redis: Any,
    *,
    undo_token: str,
    user_id: str,
) -> dict[str, Any]:
    return await _undo.undo_payload(
        redis,
        undo_token=undo_token,
        user_id=user_id,
        http=_http,
    )


async def _cleanup_undo_token(
    redis: Any,
    *,
    undo_token: str,
    user_id: str,
    log_message: str,
) -> None:
    await _undo.cleanup_undo_token(
        redis,
        undo_token=undo_token,
        user_id=user_id,
        log_message=log_message,
        release_claim=_release_undo_token_claim,
        logger=logger,
    )


async def _restore_merged_candidate(
    db: AsyncSession,
    *,
    user_id: str,
    duplicate: UserMemory,
    candidate: object,
) -> str | None:
    return await _undo.restore_merged_candidate(
        db,
        user_id=user_id,
        duplicate=duplicate,
        candidate=candidate,
        owned_scope=_owned_scope,
        audit=_audit,
    )


async def _undo_memory_action(
    db: AsyncSession,
    *,
    user_id: str,
    payload: dict[str, Any],
) -> str | None:
    return await _undo.undo_memory_action(
        db,
        user_id=user_id,
        payload=payload,
        owned_memory=_owned_memory,
        restore_merged_candidate_fn=_restore_merged_candidate,
        audit=_audit,
    )


async def _publish_account_settings_updated(redis: Any, user_id: str) -> None:
    await _common.publish_account_settings_updated(
        redis,
        user_id,
        publish_event=publish_sse_event,
    )


async def _publish_conversation_memory_updated(
    redis: Any,
    *,
    user_id: str,
    conversation_id: str,
    payload: dict[str, Any],
) -> None:
    await _common.publish_conversation_memory_updated(
        redis,
        user_id=user_id,
        conversation_id=conversation_id,
        payload=payload,
        publish_event=publish_sse_event,
    )


async def _disable_memory_for_conversation(
    redis: Any, conversation_id: str, memory_id: str
) -> None:
    await _common.disable_memory_for_conversation(redis, conversation_id, memory_id)


async def _build_memory_settings(user: User, db: AsyncSession) -> MemorySettingsOut:
    return await _common.build_memory_settings(
        user,
        db,
        embedding_available=embedding_provider_available,
    )


def _account_deps() -> AccountMemoryDependencies:
    return AccountMemoryDependencies(
        http=_http,
        dml_rowcount=_dml_rowcount,
        memory_to_out=_memory_to_out,
        staging_to_out=_staging_to_out,
        owned_scope=_owned_scope,
        owned_memory=_owned_memory,
        owned_staging=_owned_staging,
        enqueue_memory_reembed=_enqueue_memory_reembed,
        audit=_audit,
        build_memory_settings=_build_memory_settings,
        embedding_provider_available=embedding_provider_available,
        publish_account_settings_updated=_publish_account_settings_updated,
        get_redis=get_redis,
    )


def _scope_deps() -> MemoryScopeDependencies:
    return MemoryScopeDependencies(
        http=_http,
        dml_rowcount=_dml_rowcount,
        default_scope=_default_scope,
        owned_scope=_owned_scope,
        owned_memory=_owned_memory,
        memory_to_out=_memory_to_out,
        audit=_audit,
        disable_memory_for_conversation=_disable_memory_for_conversation,
        publish_account_settings_updated=_publish_account_settings_updated,
        get_redis=get_redis,
    )


def _conversation_deps() -> ConversationMemoryDependencies:
    return ConversationMemoryDependencies(
        http=_http,
        owned_scope=_owned_scope,
        publish_conversation_memory_updated=_publish_conversation_memory_updated,
        publish_account_settings_updated=_publish_account_settings_updated,
        filter_owned_used_memory_payload=_filter_owned_used_memory_payload,
        get_redis=get_redis,
    )


def _undo_deps() -> UndoDependencies:
    return UndoDependencies(
        get_redis=get_redis,
        undo_payload=_undo_payload,
        undo_token_consumed=_undo_token_consumed,
        cleanup_undo_token=_cleanup_undo_token,
        claim_undo_token=_claim_undo_token,
        undo_memory_action=_undo_memory_action,
        audit=_audit,
        release_undo_token_claim=_release_undo_token_claim,
        enqueue_memory_reembed=_enqueue_memory_reembed,
    )


@router.get("/me/memory-settings", response_model=MemorySettingsOut)
async def get_memory_settings(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemorySettingsOut:
    return await _account.get_memory_settings_impl(user, db, deps=_account_deps())


@router.patch(
    "/me/memory-settings",
    response_model=MemorySettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_settings(
    body: MemorySettingsPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemorySettingsOut:
    return await _account.patch_memory_settings_impl(
        body, user, db, deps=_account_deps()
    )


@router.patch(
    "/me/onboarding-seen",
    response_model=MemorySettingsOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_onboarding_seen(
    body: OnboardingSeenPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemorySettingsOut:
    return await _account.patch_onboarding_seen_impl(
        body, user, db, deps=_account_deps()
    )


@router.get("/me/memories", response_model=MemoryListOut)
async def list_memories(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: MemoryType | None = None,
    pinned: bool | None = None,
    disabled: bool | None = None,
    scope_id: str | None = None,
) -> MemoryListOut:
    return await _account.list_memories_impl(
        user,
        db,
        type=type,
        pinned=pinned,
        disabled=disabled,
        scope_id=scope_id,
        deps=_account_deps(),
    )


@router.post(
    "/me/memories",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_memory(
    body: MemoryCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    return await _account.create_memory_impl(body, user, db, deps=_account_deps())


@router.patch(
    "/me/memories/{memory_id}",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory(
    memory_id: str,
    body: MemoryPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    return await _account.patch_memory_impl(
        memory_id, body, user, db, deps=_account_deps()
    )


@router.delete(
    "/me/memories/{memory_id}",
    dependencies=[Depends(verify_csrf)],
)
async def forget_memory(
    memory_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await _account.forget_memory_impl(memory_id, user, db, deps=_account_deps())


@router.delete(
    "/me/memories",
    dependencies=[Depends(verify_csrf)],
)
async def clear_memories(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    confirmation: Annotated[str | None, Header(alias="X-Confirm-Clear-Memory")] = None,
) -> dict[str, int]:
    return await _account.clear_memories_impl(
        user,
        db,
        confirmation=confirmation,
        deps=_account_deps(),
    )


@router.get("/me/memories/export")
async def export_memories(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    return await _account.export_memories_impl(user, db)


@router.post(
    "/me/memories/undo",
    dependencies=[Depends(verify_csrf)],
)
async def undo_memory_write(
    body: MemoryUndoIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await _undo.undo_memory_write_impl(body, user, db, deps=_undo_deps())


@router.get("/me/memories/staging", response_model=MemoryStagingListOut)
async def list_memory_staging(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryStagingListOut:
    return await _account.list_memory_staging_impl(user, db, deps=_account_deps())


@router.patch(
    "/me/memories/staging/{staging_id}",
    response_model=MemoryStagingOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_staging(
    staging_id: str,
    body: MemoryStagingPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryStagingOut:
    return await _account.patch_memory_staging_impl(
        staging_id, body, user, db, deps=_account_deps()
    )


@router.post(
    "/me/memories/staging/{staging_id}/accept",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def accept_memory_staging(
    staging_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    return await _account.accept_memory_staging_impl(
        staging_id, user, db, deps=_account_deps()
    )


@router.post(
    "/me/memories/staging/{staging_id}/reject",
    dependencies=[Depends(verify_csrf)],
)
async def reject_memory_staging(
    staging_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await _account.reject_memory_staging_impl(
        staging_id, user, db, deps=_account_deps()
    )


@router.get("/me/memories/timeline", response_model=MemoryTimelineOut)
async def memory_timeline(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> MemoryTimelineOut:
    return await _account.memory_timeline_impl(
        user,
        db,
        cursor=cursor,
        limit=limit,
    )


@router.get("/me/memory-scopes", response_model=list[MemoryScopeOut])
async def list_memory_scopes(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[MemoryScopeOut]:
    return await _scopes.list_memory_scopes_impl(user, db)


@router.post(
    "/me/memory-scopes",
    response_model=MemoryScopeOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_memory_scope(
    body: MemoryScopeCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryScopeOut:
    return await _scopes.create_memory_scope_impl(body, user, db, deps=_scope_deps())


@router.patch(
    "/me/memory-scopes/{scope_id}",
    response_model=MemoryScopeOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_scope(
    scope_id: str,
    body: MemoryScopePatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryScopeOut:
    return await _scopes.patch_memory_scope_impl(
        scope_id, body, user, db, deps=_scope_deps()
    )


@router.delete(
    "/me/memory-scopes/{scope_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_memory_scope(
    scope_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, int]:
    return await _scopes.delete_memory_scope_impl(
        scope_id, user, db, deps=_scope_deps()
    )


@router.patch(
    "/me/memories/{memory_id}/scope",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_memory_scope_assignment(
    memory_id: str,
    body: ConversationActiveScopeIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    return await _scopes.patch_memory_scope_assignment_impl(
        memory_id, body, user, db, deps=_scope_deps()
    )


@router.post(
    "/me/memories/{memory_id}/confirm",
    response_model=MemoryOut,
    dependencies=[Depends(verify_csrf)],
)
async def confirm_memory(
    memory_id: str,
    body: MemoryConfirmIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MemoryOut:
    return await _scopes.confirm_memory_impl(
        memory_id, body, user, db, deps=_scope_deps()
    )


@router.patch(
    "/conversations/{conv_id}/memory-disabled",
    dependencies=[Depends(verify_csrf)],
)
async def patch_conversation_memory_disabled(
    conv_id: str,
    body: ConversationMemoryDisabledIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await _conversations.patch_conversation_memory_disabled_impl(
        conv_id, body, user, db, deps=_conversation_deps()
    )


@router.patch(
    "/conversations/{conv_id}/active-scope",
    dependencies=[Depends(verify_csrf)],
)
async def patch_conversation_active_scope(
    conv_id: str,
    body: ConversationActiveScopeIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str | None]:
    return await _conversations.patch_conversation_active_scope_impl(
        conv_id, body, user, db, deps=_conversation_deps()
    )


@router.get("/conversations/{conv_id}/used-memories", response_model=UsedMemoriesOut)
async def get_conversation_used_memories(
    conv_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> UsedMemoriesOut:
    return await _conversations.get_conversation_used_memories_impl(
        conv_id, user, db, deps=_conversation_deps()
    )


async def cleanup_expired_staging(db: AsyncSession) -> int:
    return await _account.cleanup_expired_staging_impl(db)
