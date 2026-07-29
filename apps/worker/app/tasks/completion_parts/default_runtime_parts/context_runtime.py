"""Context loading and enrichment adapters for the completion runtime."""

from __future__ import annotations

from typing import Any, Callable

from lumen_core.byok_retention import ByokRetentionPolicy

from .. import context_loading
from ..context import PackedContext
from ..history import SummaryBoundary


async def resolve_byok_retention_policy(
    *,
    runtime_settings: Any,
    default_delete_enabled: bool,
) -> ByokRetentionPolicy:
    return ByokRetentionPolicy(
        hide_enabled=bool(
            await runtime_settings.resolve_int("byok.retention_hide_enabled", 1)
        ),
        delete_enabled=bool(
            await runtime_settings.resolve_int(
                "byok.retention_delete_enabled",
                int(default_delete_enabled),
            )
        ),
        hide_days=await runtime_settings.resolve_int("byok.retention_hide_days", 3),
        delete_days=await runtime_settings.resolve_int("byok.retention_delete_days", 7),
    ).normalized()


async def message_retention_filter_for_account(
    account_mode: str | None,
    *,
    applies_to_account_mode: Callable[[str | None], bool],
    resolve_policy: Callable[[], Any],
    retention_cutoffs: Callable[..., Any],
    message_model: Any,
) -> Any | None:
    if not applies_to_account_mode(account_mode):
        return None
    policy = await resolve_policy()
    if not policy.hide_enabled:
        return None
    return message_model.created_at >= retention_cutoffs(policy=policy).visible_after


def count_message_tokens(
    role: str,
    content: dict[str, Any] | None,
    *,
    token_counter: Callable[[str], int],
    history_module: Any,
) -> int:
    return history_module._count_message_tokens_with_counter(
        role,
        content,
        token_counter=token_counter,
    )


def estimate_system_prompt_tokens_once(
    system_prompt: str | None,
    *,
    default_instructions: str,
    estimate_system_prompt_tokens: Callable[[str], int],
    estimate_text_tokens: Callable[[str], int],
) -> int:
    prompt = system_prompt or default_instructions
    estimated = estimate_system_prompt_tokens(prompt)
    if estimated <= 0:
        return 0
    return max(0, estimated - estimate_text_tokens(prompt))


async def attachment_to_data_url(
    session: Any,
    image_id: str,
    *,
    storage_get_bytes: Callable[..., Any],
    logger: Any,
) -> str | None:
    return await context_loading._attachment_to_data_url(
        session,
        image_id,
        storage_get_bytes=storage_get_bytes,
        logger=logger,
    )


async def message_to_input_item(
    session: Any,
    message: Any,
    *,
    attachment_loader: Callable[..., Any],
) -> dict[str, Any] | None:
    return await context_loading._message_to_input_item(
        session,
        message,
        attachment_to_data_url=attachment_loader,
    )


async def build_input_from_packed_context(
    session: Any,
    packed: PackedContext,
    *,
    message_converter: Callable[..., Any],
) -> list[dict[str, Any]]:
    return await context_loading._build_input_from_packed_context(
        session,
        packed,
        message_to_input_item=message_converter,
    )


async def load_rows_desc(
    session: Any,
    *,
    conversation_id: str,
    target: Any,
    budget_tokens: int | None,
    system_prompt: str | None,
    retention_filter: Any | None,
    count_message_tokens: Callable[..., int],
    estimate_system_prompt_tokens: Callable[..., int],
) -> tuple[list[Any], int, bool]:
    return await context_loading._load_rows_desc(
        session,
        conversation_id=conversation_id,
        target=target,
        budget_tokens=budget_tokens,
        system_prompt=system_prompt,
        retention_filter=retention_filter,
        count_message_tokens=count_message_tokens,
        estimate_system_prompt_tokens=estimate_system_prompt_tokens,
    )


async def load_rows_desc_after_summary(
    session: Any,
    *,
    conversation_id: str,
    target: Any,
    summary: dict[str, Any],
    retention_filter: Any | None,
) -> list[Any]:
    return await context_loading._load_rows_desc_after_summary(
        session,
        conversation_id=conversation_id,
        target=target,
        summary=summary,
        retention_filter=retention_filter,
    )


async def get_message(
    session: Any,
    message_id: str | None,
    *,
    logger: Any,
) -> Any | None:
    return await context_loading._get_message(session, message_id, logger=logger)


async def pick_first_user_from_summary(
    session: Any,
    summary: dict[str, Any],
    *,
    get_message: Callable[..., Any],
) -> Any | None:
    return await context_loading._pick_first_user_from_summary(
        session,
        summary,
        get_message=get_message,
    )


async def pick_current_user_with_lookup(
    session: Any,
    rows_desc: list[Any],
    target: Any,
    summary: dict[str, Any] | None,
    *,
    get_message: Callable[..., Any],
) -> Any | None:
    return await context_loading._pick_current_user_with_lookup(
        session,
        rows_desc,
        target,
        summary,
        get_message=get_message,
    )


async def resolve_summary_model(*, runtime_settings: Any, logger: Any) -> str:
    return await context_loading._resolve_summary_model(
        runtime_settings=runtime_settings,
        logger=logger,
    )


async def resolve_int_setting(
    spec_key: str,
    default: int,
    *,
    runtime_settings: Any,
    logger: Any,
) -> int:
    return await context_loading._resolve_int_setting(
        spec_key,
        default,
        runtime_settings=runtime_settings,
        logger=logger,
    )


async def ensure_context_summary(
    session: Any,
    conversation: Any,
    boundary: SummaryBoundary,
    *,
    target_tokens: int,
    model: str,
    redis: Any | None,
    service: Any,
    logger: Any,
) -> dict[str, Any] | None:
    return await context_loading._ensure_context_summary(
        session,
        conversation,
        boundary,
        target_tokens=target_tokens,
        model=model,
        redis=redis,
        service=service,
        logger=logger,
    )


def build_context_loading_hooks(**kwargs: Any) -> context_loading.ContextLoadingHooks:
    return context_loading.ContextLoadingHooks(**kwargs)


async def pack_recent_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    redis: Any | None,
    chat_model: str | None,
    account_mode: str | None,
    hooks: context_loading.ContextLoadingHooks,
) -> PackedContext:
    return await context_loading._pack_recent_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
        redis=redis,
        chat_model=chat_model,
        account_mode=account_mode,
        hooks=hooks,
    )


async def build_input_from_history(
    session: Any,
    *,
    conversation_id: str,
    up_to_message_id: str,
    system_prompt: str | None,
    pack_recent_history: Callable[..., Any],
) -> list[dict[str, Any]]:
    return await context_loading._build_input_from_history(
        session,
        conversation_id=conversation_id,
        up_to_message_id=up_to_message_id,
        system_prompt=system_prompt,
        pack_recent_history=pack_recent_history,
    )


async def inject_memory_context(
    session: Any,
    *,
    input_list: list[dict[str, Any]],
    user_id: str,
    conversation_id: str | None,
    parent_user_message_id: str | None,
    redis: Any | None,
    injector: Callable[..., Any],
    memory_extraction: Any,
    message_model: Any,
) -> dict[str, Any]:
    return await injector(
        session,
        input_list=input_list,
        user_id=user_id,
        conversation_id=conversation_id,
        parent_user_message_id=parent_user_message_id,
        memory_extraction=memory_extraction,
        message_model=message_model,
        redis=redis,
    )


async def record_context_metadata(
    session: Any,
    *,
    task_id: str,
    attempt_epoch: int,
    packed: PackedContext,
    recorder: Callable[..., Any],
    completion_model: Any,
) -> None:
    await recorder(
        session,
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        packed=packed,
        completion_model=completion_model,
    )
