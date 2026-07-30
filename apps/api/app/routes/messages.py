"""Message route facade preserving the historical module contract.

Domain behavior lives in ``messages_parts``. This module keeps route
registration, compatibility aliases, and monkeypatch seams stable.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Awaitable, Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core import billing as billing_core  # noqa: F401
from lumen_core.byok_retention import (
    applies_to_user as byok_retention_applies_to_user,
    is_user_visible as byok_retention_is_user_visible,
    user_visible_filter as byok_retention_user_visible_filter,
)
from lumen_core.constants import (
    IMAGE_MULTI_GEN_STAGGER_CAP_S,  # noqa: F401 - compatibility facade
    IMAGE_MULTI_GEN_STAGGER_S,  # noqa: F401 - compatibility facade
    MAX_MESSAGE_ATTACHMENTS,  # noqa: F401 - imported compatibility surface
    MAX_PROMPT_CHARS,  # noqa: F401 - imported compatibility surface
    Intent,  # noqa: F401 - compatibility facade for route tests
    MessageStatus,  # noqa: F401 - compatibility facade for route tests
    Role,  # noqa: F401 - compatibility facade for route tests
)
from lumen_core.memory import (  # noqa: F401 - imported compatibility surface
    canonical_memory_text,
    extract_memories,
)
from lumen_core.models import (
    Completion,  # noqa: F401 - imported compatibility surface
    Conversation,
    Generation,
    Image,  # noqa: F401 - imported compatibility surface
    MemoryAudit,  # noqa: F401 - imported compatibility surface
    Message,
    User,
    UserMemory,  # noqa: F401 - imported compatibility surface
    UserMemoryScope,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,  # noqa: F401 - imported compatibility surface
    MessageOut,
    PostMessageIn,
    PostMessageOut,
)

from ..arq_pool import get_arq_pool
from ..audit import write_audit
from ..byok_service import (  # noqa: F401
    read_byok_settings,
    read_byok_settings_cached,
    retention_policy_from_settings,
)
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..intent import resolve_intent
from ..ratelimit import MESSAGES_LIMITER
from ..redis_client import get_redis
from ..runtime_settings import embedding_provider_available, get_setting
from ..services import message_submission as _message_submission
from ..services.message_request import (
    AssistantContextRuntime,
    MessageTransactionRuntime,
)
from ..sse_publish import publish_sse_event, publish_sse_events
from ..task_billing import (
    ChatWalletPreflight as _ChatWalletPreflight,
    apply_rate_multiplier_micro as _apply_rate_multiplier_micro,
    requested_image_billing_tier as _requested_image_billing_tier,
    user_rate_multiplier_x10000 as _user_rate_multiplier_x10000,
)
from .messages_parts import memory as _memory
from .messages_parts import publishing as _publishing
from .messages_parts import queries as _queries
from .messages_parts import silent as _silent
from .messages_parts import submission as _submission


router = APIRouter()
logger = logging.getLogger(__name__)

AssistantTaskResult = _message_submission.AssistantTaskResult
_TaskCredentialPin = _message_submission.TaskCredentialPin
_IMAGE_OUTPUT_FORMAT_VALUES = _message_submission.IMAGE_OUTPUT_FORMAT_VALUES
_DEFAULT_IMAGE_OUTPUT_FORMAT = _message_submission.DEFAULT_IMAGE_OUTPUT_FORMAT
_idempotency_lock_key = _message_submission.idempotency_lock_key
_stored_idempotency_key = _message_submission.stored_idempotency_key
_generation_child_idempotency_key = _message_submission.generation_child_idempotency_key
_image_multi_generation_defer_s = _message_submission.image_multi_generation_defer_s
_idempotency_lookup_keys = _message_submission.idempotency_lookup_keys
_image_params_with_fast_default = _message_submission.image_params_with_fast_default
_chat_params_with_fast_default = _message_submission.chat_params_with_fast_default
_wants_transparent_background = _message_submission.wants_transparent_background
_image_upstream_request = _message_submission.image_upstream_request
_message_request_metadata = _message_submission.message_request_metadata
build_structured_system_prompt = _message_submission.build_structured_system_prompt
resolve_system_prompt_for_message = (
    _message_submission.resolve_system_prompt_for_message
)

ALLOWED_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh"}
)
_SILENT_GENERATION_REQUEST_HASH_KEY = _silent.SILENT_GENERATION_REQUEST_HASH_KEY
_POST_COMMIT_PUBLISH_TIMEOUT_S = 2.0
_CONFIRM_REPLY_YES_RE = _memory.CONFIRM_REPLY_YES_RE
_CONFIRM_REPLY_NO_RE = _memory.CONFIRM_REPLY_NO_RE

SilentGenerationIn = _silent.SilentGenerationIn
SilentGenerationOut = _silent.SilentGenerationOut


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def choose_system_prompt(
    *,
    explicit_prompt: str | None,
    conversation_prompt: str | None,
    legacy_conversation_prompt: str | None,
    global_prompt: str | None,
) -> str | None:
    for candidate in (
        explicit_prompt,
        conversation_prompt,
        legacy_conversation_prompt,
        global_prompt,
    ):
        prompt = _message_submission._sanitize_system_prompt_source(candidate)
        if prompt is not None:
            return prompt
    return None


_billing_setting_raw = _message_submission.billing_setting_raw
_billing_enabled = _message_submission.billing_enabled
_billing_allow_negative = _message_submission.billing_allow_negative
_billing_image_thresholds = _message_submission.billing_image_thresholds
_chat_tool_budget_setting_micro = _message_submission.chat_tool_budget_setting_micro
_chat_max_tool_invocations = _message_submission.chat_max_tool_invocations


async def _ensure_chat_wallet_preflight(
    db: AsyncSession,
    *,
    user_id: str,
    user_email: str | None,
    account_mode: str,
    model: str,
    chat_params: ChatParamsIn | None = None,
) -> _ChatWalletPreflight | None:
    return await _message_submission.ensure_chat_wallet_preflight(
        db,
        user_id=user_id,
        user_email=user_email,
        account_mode=account_mode,
        model=model,
        chat_params=chat_params,
        billing_enabled_fn=_billing_enabled,
        billing_allow_negative_fn=_billing_allow_negative,
        user_rate_multiplier_fn=_user_rate_multiplier_x10000,
        chat_tool_budget_setting_fn=_chat_tool_budget_setting_micro,
        chat_max_tool_invocations_fn=_chat_max_tool_invocations,
    )


async def _resolve_fast_default(db: AsyncSession) -> bool:
    return await _message_submission.resolve_fast_default(
        db,
        get_spec_fn=get_spec,
        get_setting_fn=get_setting,
    )


async def _ensure_file_search_configured(
    db: AsyncSession,
    chat_params: ChatParamsIn,
) -> None:
    await _message_submission.ensure_file_search_configured(
        db,
        chat_params,
        get_spec_fn=get_spec,
        get_setting_fn=get_setting,
    )


async def _lock_idempotency_key(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> bool:
    return await _queries.lock_idempotency_key(
        db,
        user_id,
        conv_id,
        idempotency_key,
        idempotency_lock_key_fn=_idempotency_lock_key,
    )


def _message_alive_filters() -> tuple[Any, ...]:
    return _queries.message_alive_filters()


async def _byok_retention_policy_for_user(
    db: AsyncSession,
    user: User,
) -> Any | None:
    return await _queries.byok_retention_policy_for_user(
        db,
        user,
        applies_to_user_fn=byok_retention_applies_to_user,
        read_byok_settings_cached_fn=read_byok_settings_cached,
        retention_policy_from_settings_fn=retention_policy_from_settings,
    )


def _message_user_visible_filters(
    user: User,
    *,
    retention_policy: Any | None,
) -> tuple[Any, ...]:
    return _queries.message_user_visible_filters(
        user,
        retention_policy=retention_policy,
        message_alive_filters_fn=_message_alive_filters,
        user_visible_filter_fn=byok_retention_user_visible_filter,
    )


async def _ensure_conversation_visible_to_user(
    db: AsyncSession,
    conv: Conversation,
    user: User,
) -> None:
    await _queries.ensure_conversation_visible_to_user(
        db,
        conv,
        user,
        retention_policy_for_user_fn=_byok_retention_policy_for_user,
        is_user_visible_fn=byok_retention_is_user_visible,
        http_error_fn=_http,
    )


async def _byok_image_visible_filter(
    db: AsyncSession,
    user: User,
) -> Any | None:
    return await _queries.byok_image_visible_filter(
        db,
        user,
        retention_policy_for_user_fn=_byok_retention_policy_for_user,
        user_visible_filter_fn=byok_retention_user_visible_filter,
    )


async def _default_memory_scope(
    db: AsyncSession,
    user_id: str,
) -> UserMemoryScope:
    return await _memory.default_memory_scope(db, user_id)


async def _enqueue_memory_reembed(target: str, row_id: str) -> None:
    await _memory.enqueue_memory_reembed(
        target,
        row_id,
        get_arq_pool_fn=get_arq_pool,
        log=logger,
    )


async def _memory_undo_token(payload: dict[str, Any]) -> str | None:
    return await _memory.memory_undo_token(payload, get_redis_fn=get_redis)


async def _disable_memory_for_conversation(
    conversation_id: str,
    memory_id: str,
) -> None:
    await _memory.disable_memory_for_conversation(
        conversation_id,
        memory_id,
        get_redis_fn=get_redis,
    )


def _confirmation_reply_decision(
    text: str,
) -> Literal["yes", "no", "skip"] | None:
    return _memory.confirmation_reply_decision(text)


async def _apply_pending_confirmation_reply(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    user_msg: Message,
    text: str,
) -> None:
    await _memory.apply_pending_confirmation_reply(
        db=db,
        user=user,
        conv=conv,
        user_msg=user_msg,
        text=text,
        decision_fn=_confirmation_reply_decision,
        disable_memory_fn=_disable_memory_for_conversation,
    )


async def _apply_explicit_memory_write(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    user_msg: Message,
    assistant_msg: Message,
    text: str,
    reembed_ids: list[str] | None = None,
) -> None:
    await _memory.apply_explicit_memory_write(
        db=db,
        user=user,
        conv=conv,
        user_msg=user_msg,
        assistant_msg=assistant_msg,
        text=text,
        reembed_ids=reembed_ids,
        embedding_provider_available_fn=embedding_provider_available,
        default_memory_scope_fn=_default_memory_scope,
        memory_undo_token_fn=_memory_undo_token,
    )


async def _resolve_task_credential_pin(
    db: AsyncSession,
    user_id: str,
    required_purpose: str,
    account_mode: str,
) -> _TaskCredentialPin | None:
    return await _message_submission.resolve_task_credential_pin(
        db,
        user_id,
        required_purpose,
        account_mode,
        read_byok_settings_cached_fn=read_byok_settings_cached,
    )


_select_chat_task_model = _message_submission._select_chat_task_model


async def _create_assistant_task(**kwargs: Any) -> AssistantTaskResult:
    return await _message_submission.create_assistant_task(
        **kwargs,
        resolve_task_credential_pin_fn=_resolve_task_credential_pin,
        ensure_chat_wallet_preflight_fn=_ensure_chat_wallet_preflight,
        billing_enabled_fn=_billing_enabled,
        billing_allow_negative_fn=_billing_allow_negative,
        billing_image_thresholds_fn=_billing_image_thresholds,
        user_rate_multiplier_fn=_user_rate_multiplier_x10000,
        apply_rate_multiplier_fn=_apply_rate_multiplier_micro,
        requested_image_billing_tier_fn=_requested_image_billing_tier,
        write_audit_fn=write_audit,
    )


async def _publish_message_appended(**kwargs: Any) -> None:
    await _publishing.publish_message_appended(
        **kwargs,
        publish_sse_event_fn=publish_sse_event,
        publish_sse_events_fn=publish_sse_events,
        log=logger,
    )


async def _publish_assistant_task(**kwargs: Any) -> None:
    await _publishing.publish_assistant_task(
        **kwargs,
        get_arq_pool_fn=get_arq_pool,
        publish_sse_event_fn=publish_sse_event,
        log=logger,
    )


async def create_assistant_task(**kwargs: Any) -> AssistantTaskResult:
    """Public adapter preserving the legacy task-creation patch point."""
    return await _create_assistant_task(**kwargs)


async def publish_assistant_task(**kwargs: Any) -> None:
    """Public adapter preserving the legacy task-publishing patch point."""
    await _publish_assistant_task(**kwargs)


async def _await_post_commit_publish(
    label: str,
    awaitable: Awaitable[Any],
    *,
    user_id: str,
    conv_id: str,
    assistant_msg_id: str | None = None,
) -> None:
    await _publishing.await_post_commit_publish(
        label,
        awaitable,
        user_id=user_id,
        conv_id=conv_id,
        assistant_msg_id=assistant_msg_id,
        await_many_fn=_await_post_commit_publishes,
    )


async def _await_post_commit_publishes(
    *publishes: tuple[str, Awaitable[Any], str | None],
    user_id: str,
    conv_id: str,
) -> None:
    await _publishing.await_post_commit_publishes(
        *publishes,
        user_id=user_id,
        conv_id=conv_id,
        timeout_s=_POST_COMMIT_PUBLISH_TIMEOUT_S,
        log=logger,
    )


async def _lookup_idempotent_post(
    db: AsyncSession,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
) -> PostMessageOut | None:
    return await _queries.lookup_idempotent_post(
        db,
        user_id,
        conv_id,
        idempotency_key,
        message_alive_filters_fn=_message_alive_filters,
        idempotency_lookup_keys_fn=_idempotency_lookup_keys,
    )


def _assistant_context_runtime() -> AssistantContextRuntime:
    return AssistantContextRuntime(
        resolve_system_prompt=resolve_system_prompt_for_message,
        resolve_credential_pin=_resolve_task_credential_pin,
        get_setting=get_setting,
        default_image_output_format=_DEFAULT_IMAGE_OUTPUT_FORMAT,
        image_output_format_values=_IMAGE_OUTPUT_FORMAT_VALUES,
    )


def _message_transaction_runtime() -> MessageTransactionRuntime:
    return MessageTransactionRuntime(
        apply_pending_confirmation_reply=_apply_pending_confirmation_reply,
        create_assistant_task=_create_assistant_task,
        apply_explicit_memory_write=_apply_explicit_memory_write,
        lookup_idempotent_post=_lookup_idempotent_post,
        http_error=_http,
    )


def _submission_runtime() -> _submission.SubmissionRuntime:
    return _submission.SubmissionRuntime(
        get_redis=get_redis,
        messages_limiter=MESSAGES_LIMITER,
        http_error=_http,
        ensure_conversation_visible=_ensure_conversation_visible_to_user,
        lookup_idempotent_post=_lookup_idempotent_post,
        lock_idempotency_key=_lock_idempotency_key,
        byok_image_visible_filter=_byok_image_visible_filter,
        resolve_intent=resolve_intent,
        message_request_metadata=_message_request_metadata,
        resolve_fast_default=_resolve_fast_default,
        image_params_with_fast_default=_image_params_with_fast_default,
        chat_params_with_fast_default=_chat_params_with_fast_default,
        ensure_file_search_configured=_ensure_file_search_configured,
        assistant_context_runtime=_assistant_context_runtime,
        message_transaction_runtime=_message_transaction_runtime,
        enqueue_memory_reembed=_enqueue_memory_reembed,
        await_post_commit_publishes=_await_post_commit_publishes,
        publish_message_appended=_publish_message_appended,
        publish_assistant_task=_publish_assistant_task,
        allowed_reasoning_efforts=ALLOWED_REASONING_EFFORTS,
    )


async def submit_user_message(
    conv_id: str,
    body: PostMessageIn,
    user: User,
    db: AsyncSession,
) -> PostMessageOut:
    """Submit a message from HTTP or the Telegram authenticated adapter."""
    return await _submission.submit_user_message(
        conv_id,
        body,
        user,
        db,
        runtime=_submission_runtime(),
    )


@router.post(
    "/conversations/{conv_id}/messages",
    response_model=PostMessageOut,
    dependencies=[Depends(verify_csrf)],
)
async def post_message(
    conv_id: str,
    body: PostMessageIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PostMessageOut:
    return await submit_user_message(conv_id, body, user, db)


def _silent_generation_request_hash(body: SilentGenerationIn) -> str:
    return _silent.silent_generation_request_hash(body)


def _stored_silent_generation_request_hash(generation: Generation) -> Any:
    return _silent.stored_silent_generation_request_hash(generation)


async def _lookup_silent_generation(
    db: AsyncSession,
    *,
    user: User,
    user_id: str,
    conv_id: str,
    idempotency_key: str,
    parent_message_id: str,
    request_hash: str,
    retention_policy: Any | None,
) -> SilentGenerationOut | None:
    return await _silent.lookup_silent_generation(
        db,
        user=user,
        user_id=user_id,
        conv_id=conv_id,
        idempotency_key=idempotency_key,
        parent_message_id=parent_message_id,
        request_hash=request_hash,
        retention_policy=retention_policy,
        idempotency_lookup_keys_fn=_idempotency_lookup_keys,
        message_user_visible_filters_fn=_message_user_visible_filters,
        stored_request_hash_fn=_stored_silent_generation_request_hash,
        http_error_fn=_http,
    )


def _silent_generation_runtime() -> _silent.SilentGenerationRuntime:
    return _silent.SilentGenerationRuntime(
        get_redis=get_redis,
        http_error=_http,
        ensure_conversation_visible=_ensure_conversation_visible_to_user,
        retention_policy_for_user=_byok_retention_policy_for_user,
        request_hash=_silent_generation_request_hash,
        request_hash_key=_SILENT_GENERATION_REQUEST_HASH_KEY,
        lookup_silent_generation=_lookup_silent_generation,
        lock_idempotency_key=_lock_idempotency_key,
        message_user_visible_filters=_message_user_visible_filters,
        byok_image_visible_filter=_byok_image_visible_filter,
        get_spec=get_spec,
        get_setting=get_setting,
        resolve_fast_default=_resolve_fast_default,
        image_params_with_fast_default=_image_params_with_fast_default,
        create_assistant_task=_create_assistant_task,
        await_post_commit_publishes=_await_post_commit_publishes,
        publish_message_appended=_publish_message_appended,
        publish_assistant_task=_publish_assistant_task,
        default_image_output_format=_DEFAULT_IMAGE_OUTPUT_FORMAT,
        image_output_format_values=_IMAGE_OUTPUT_FORMAT_VALUES,
    )


@router.post(
    "/conversations/{conv_id}/generations",
    response_model=SilentGenerationOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_silent_generation(
    conv_id: str,
    body: SilentGenerationIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SilentGenerationOut:
    return await _silent.create_silent_generation(
        conv_id,
        body,
        user,
        db,
        runtime=_silent_generation_runtime(),
    )


@router.get(
    "/conversations/{conv_id}/messages/{message_id}",
    response_model=MessageOut,
)
async def get_message(
    conv_id: str,
    message_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MessageOut:
    return await _queries.get_message(
        db,
        user_id=user.id,
        conv_id=conv_id,
        message_id=message_id,
        message_alive_filters_fn=_message_alive_filters,
        http_error_fn=_http,
    )


publish_message_appended = _publish_message_appended
DEFAULT_IMAGE_OUTPUT_FORMAT = _DEFAULT_IMAGE_OUTPUT_FORMAT
await_post_commit_publish = _await_post_commit_publish
await_post_commit_publishes = _await_post_commit_publishes
idempotency_lookup_keys = _idempotency_lookup_keys
message_alive_filters = _message_alive_filters
