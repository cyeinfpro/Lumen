"""Explicit composition root and compatibility facade for completion tasks."""

from __future__ import annotations

# This facade intentionally exposes the historical module namespace while all
# implementation logic lives in dependency-explicit leaf modules.
# ruff: noqa: F403, F405
from .default_runtime_parts.compat import *

from .default_runtime_parts import (
    composition,
    context_adapter,
    delivery_runtime,
    lease_runtime,
    persistence_runtime,
    tool_runtime,
)

__all__ = PUBLIC_FACADE_EXPORTS

logger = logging.getLogger(__name__)
_tracer = get_tracer("lumen.worker.completion")

_LEASE_TTL_S = 300
_LEASE_RENEW_S = 30
_MAX_ATTEMPTS = 3
_PG_FLUSH_EVERY_CHARS = 128
_PG_FLUSH_RETRIES = 3
_PG_FLUSH_BACKOFF_S = 0.2
_CONTEXT_COMPRESSION_ENABLED_DEFAULT = 1
_CONTEXT_COMPRESSION_TRIGGER_PERCENT_DEFAULT = 80
_CONTEXT_SUMMARY_TARGET_TOKENS_DEFAULT = 1200
_CONTEXT_SUMMARY_MIN_RECENT_MESSAGES_DEFAULT = 16
_CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS_DEFAULT = 30
_CHAT_TOOL_VECTOR_STORE_SETTING = "chat.file_search_vector_store_ids"
_CHAT_IMAGE_TOOL_SIZE = "1024x1024"
_CANCEL_CHECK_EVERY_DELTAS = 4
_CANCEL_POLL_INTERVAL_S = 0.1
_MAX_TOOL_INVOCATIONS_DEFAULT = 8
_TOOL_IDLE_TIMEOUT_S_DEFAULT = 30.0
_CHAT_TOOL_IMAGE_BUDGET_SETTING = "chat.tool_image_generation_micro"
_TOOL_LIMIT_FALLBACK_TEXT = (
    "Tool invocation limit reached. Continue with the information already "
    "available and do not call any tools."
)
_RUNNING_COMPLETION_STATUSES = (CompletionStatus.STREAMING.value,)

_CompletionToolInsufficientBalance = tool_runtime.CompletionToolInsufficientBalance
_CompletionEpochSuperseded = persistence_runtime.CompletionEpochSuperseded
_completion_lock_key = persistence_runtime.completion_lock_key
_fallback_completion_tool_image_tokens = (
    completion_billing.fallback_completion_tool_image_tokens
)
_image_output_tokens_for_budget = completion_billing.image_output_tokens_for_budget
run_completion = _run_completion

_context_adapter = context_adapter.ContextAdapter(
    context_adapter.ContextAdapterDependencies(
        runtime_settings=lambda: runtime_settings,
        count_tokens=lambda: count_tokens,
        estimate_system_prompt_tokens=lambda: estimate_system_prompt_tokens,
        estimate_text_tokens=lambda: estimate_text_tokens,
        get_input_budget=lambda: get_input_budget,
        input_token_budget=lambda: CONTEXT_INPUT_TOKEN_BUDGET,
        context_summary=lambda: context_summary,
        memory_extraction=lambda: memory_extraction,
        storage_get_bytes=storage.get_bytes,
        logger=logger,
        pick_first_user=_pick_first_user,
        pick_current_user=_pick_current_user,
        context_circuit_open=_context_circuit_open,
        compression_enabled_default=_CONTEXT_COMPRESSION_ENABLED_DEFAULT,
        compression_trigger_percent_default=_CONTEXT_COMPRESSION_TRIGGER_PERCENT_DEFAULT,
        summary_target_tokens_default=_CONTEXT_SUMMARY_TARGET_TOKENS_DEFAULT,
        summary_min_recent_messages_default=_CONTEXT_SUMMARY_MIN_RECENT_MESSAGES_DEFAULT,
        summary_min_interval_seconds_default=_CONTEXT_SUMMARY_MIN_INTERVAL_SECONDS_DEFAULT,
        attachment_to_data_url=lambda: _attachment_to_data_url,
        message_to_input_item=lambda: _message_to_input_item,
        count_message_tokens=lambda: _count_message_tokens,
        estimate_system_prompt_tokens_once=lambda: _estimate_system_prompt_tokens_once,
        message_retention_filter=lambda: _message_retention_filter_for_account,
        resolve_summary_model=lambda: _resolve_summary_model,
        resolve_int_setting=lambda: _resolve_int_setting,
        ensure_context_summary=lambda: _ensure_context_summary,
        build_input_from_packed_context=lambda: _build_input_from_packed_context,
        load_rows_desc=lambda: _load_rows_desc,
        load_rows_desc_after_summary=lambda: _load_rows_desc_after_summary,
        pick_first_user_from_summary=lambda: _pick_first_user_from_summary,
        pick_current_user_with_lookup=lambda: _pick_current_user_with_lookup,
        context_loading_hooks=lambda: _context_loading_hooks,
        pack_recent_history=lambda: _pack_recent_history,
        resolve_byok_retention_policy=lambda: _resolve_byok_retention_policy,
        get_message=lambda: _get_message,
    )
)
_resolve_byok_retention_policy = _context_adapter.resolve_byok_retention_policy
_message_retention_filter_for_account = (
    _context_adapter.message_retention_filter_for_account
)
_count_message_tokens = _context_adapter.count_message_tokens
_estimate_system_prompt_tokens_once = (
    _context_adapter.estimate_system_prompt_tokens_once
)


async def _record_completion_upstream_metadata(
    *,
    task_id: str,
    attempt_epoch: int,
    provider_event: dict[str, str],
    fast_mode: bool,
) -> None:
    await persistence_runtime.record_upstream_metadata(
        task_id=task_id,
        attempt_epoch=attempt_epoch,
        provider_event=provider_event,
        fast_mode=fast_mode,
        session_factory=SessionLocal,
        completion_model=Completion,
        running_statuses=_RUNNING_COMPLETION_STATUSES,
        merge_metadata=_merge_completion_upstream_metadata,
        logger=logger,
    )


async def _chat_tools_from_content(
    content: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    return await tool_runtime.chat_tools_from_content(
        content,
        runtime_settings=runtime_settings,
        logger=logger,
        content_str_list=_content_str_list,
        split_csv_ids=_split_csv_ids,
        vector_store_setting=_CHAT_TOOL_VECTOR_STORE_SETTING,
        web_search_type=_WEB_SEARCH_TOOL_TYPE,
        file_search_type=_FILE_SEARCH_TOOL_TYPE,
        code_interpreter_type=_CODE_INTERPRETER_TOOL_TYPE,
        image_generation_type=_IMAGE_GENERATION_TOOL_TYPE,
        image_size=_CHAT_IMAGE_TOOL_SIZE,
    )


def _configure_chat_tools(body: dict[str, Any], tools: list[dict[str, Any]]) -> None:
    tool_runtime.configure_chat_tools(body, tools)


async def _settle_failed_completion_billing(
    session: Any,
    completion: Completion,
    *,
    usage_values: tuple[Any, ...],
    reason: str,
) -> None:
    await persistence_runtime.settle_failed_billing(
        session,
        completion,
        usage_values=usage_values,
        reason=reason,
        worker_billing=worker_billing,
    )


_COMPLETION_EVENT_HOOKS = delivery_runtime.build_event_hooks(
    session_factory=lambda: SessionLocal(),
    stage_outbox_event=_completion_outbox._stage_outbox_event,
    raw_publish_event=lambda *args, **kwargs: _publish_sse_event(*args, **kwargs),
    new_event_id=new_uuid7,
    user_model=User,
    conversation_model=Conversation,
    logger=logger,
)
_stage_completion_event, publish_event = delivery_runtime.bind_event_functions(
    _COMPLETION_EVENT_HOOKS
)


async def _publish_completion_tool_progress(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_call: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> None:
    await delivery_runtime.publish_tool_progress(
        redis=redis,
        user_id=user_id,
        channel=channel,
        task_id=task_id,
        message_id=message_id,
        attempt=attempt,
        attempt_epoch=attempt_epoch,
        tool_call=tool_call,
        tool_calls=tool_calls,
        publish_event=publish_event,
        progress_event=EV_COMP_PROGRESS,
    )


async def _deliver_completion_event(
    redis: Any,
    delivery: tuple[str, str, dict[str, Any]],
) -> None:
    await delivery_runtime.deliver_completion_event(
        redis,
        delivery,
        deliver_staged_events=_completion_outbox._deliver_staged_outbox_events,
    )


async def _publish_completion_tool_updates(
    *,
    redis: Any,
    user_id: str,
    channel: str,
    task_id: str,
    message_id: str,
    attempt: int,
    attempt_epoch: int,
    tool_tracker: _CompletionToolTracker,
    updates: list[dict[str, Any]],
) -> None:
    await delivery_runtime.publish_tool_updates(
        redis=redis,
        user_id=user_id,
        channel=channel,
        task_id=task_id,
        message_id=message_id,
        attempt=attempt,
        attempt_epoch=attempt_epoch,
        tool_tracker=tool_tracker,
        updates=updates,
        publish_event=publish_event,
        publish_progress=_publish_completion_tool_progress,
        progress_event=EV_COMP_PROGRESS,
    )


def _tool_limited_completion_body(body: dict[str, Any]) -> dict[str, Any]:
    return tool_runtime.tool_limited_completion_body(
        body,
        fallback_text=_TOOL_LIMIT_FALLBACK_TEXT,
    )


def _compute_blurhash(img: PILImage.Image) -> str | None:
    return tool_runtime.compute_blurhash(
        img,
        encoder=_generation_compute_blurhash,
    )


def _image_format_and_meta(raw_image: bytes) -> tuple[Any, ...]:
    return tool_runtime.image_format_and_meta(
        raw_image,
        compute_blurhash=_compute_blurhash,
        make_display=_make_display,
        make_preview=_make_preview,
        make_thumb=_make_thumb,
        bad_response_error_code=EC.BAD_RESPONSE.value,
    )


def _build_completion_tool_image_service(
    storage_writes: StorageWriteCoordinator | None = None,
) -> CompletionToolImageService:
    return tool_runtime.build_completion_tool_image_service(
        storage_writes=storage_writes,
        dependencies=tool_runtime.ToolImageServiceDependencies(
            default_write_files=_write_generation_files,
            default_cleanup_on_error=_cleanup_storage_on_error,
            reserve_budget=_ensure_completion_tool_image_wallet_budget,
            format_and_meta=_image_format_and_meta,
            sha256=_sha256,
            session_factory=SessionLocal,
            new_id=new_uuid7,
            acquire_lock=_acquire_completion_xact_lock,
            completion_model=Completion,
            running_statuses=_RUNNING_COMPLETION_STATUSES,
            superseded_error_type=_CompletionEpochSuperseded,
            fallback_image_tokens=_fallback_completion_tool_image_tokens,
            image_model=Image,
            image_variant_model=ImageVariant,
            message_model=Message,
            public_url=storage.public_url,
            publish_event=publish_event,
            image_event=EV_COMP_IMAGE,
            bad_response_error_code=EC.BAD_RESPONSE.value,
        ),
    )


async def _ensure_completion_tool_image_wallet_budget(
    *,
    user_id: str,
    task_id: str,
    reserved_micro: int = 0,
) -> int:
    return await tool_runtime.ensure_completion_tool_image_wallet_budget(
        user_id=user_id,
        task_id=task_id,
        reserved_micro=reserved_micro,
        runtime_settings=runtime_settings,
        session_factory=SessionLocal,
        completion_model=Completion,
        worker_billing=worker_billing,
        billing_core=billing_core,
        budget_setting=_CHAT_TOOL_IMAGE_BUDGET_SETTING,
    )


async def _is_cancelled(redis: Any, task_id: str) -> bool:
    return await lease_runtime.is_cancelled(
        redis,
        task_id,
        cancel_check_errors_total=completion_cancel_check_errors_total,
        logger=logger,
    )


async def _raise_if_completion_cancelled(
    redis: Any,
    task_id: str,
    reason: str,
) -> None:
    await lease_runtime.raise_if_cancelled(
        redis,
        task_id,
        reason,
        is_cancelled=_is_cancelled,
    )


async def _watch_completion_cancel(
    redis: Any,
    task_id: str,
    *,
    cancel_requested: asyncio.Event,
    stop_requested: asyncio.Event,
    poll_interval_s: float = _CANCEL_POLL_INTERVAL_S,
) -> None:
    await lease_runtime.watch_cancel(
        redis,
        task_id,
        cancel_requested=cancel_requested,
        stop_requested=stop_requested,
        poll_interval_s=poll_interval_s,
        is_cancelled=_is_cancelled,
    )


async def _iter_completion_stream_with_abort(
    stream: Any,
    *,
    cancel_requested: asyncio.Event,
    lease_lost: asyncio.Event,
    tool_tracker: _CompletionToolTracker,
    tool_idle_timeout_s: float,
) -> Any:
    async for event in lease_runtime.iter_stream_with_abort(
        stream,
        cancel_requested=cancel_requested,
        lease_lost=lease_lost,
        tool_tracker=tool_tracker,
        tool_idle_timeout_s=tool_idle_timeout_s,
        next_event=_next_completion_stream_event,
    ):
        yield event


async def _acquire_completion_xact_lock(
    session: Any,
    completion_id: str,
) -> None:
    await persistence_runtime.acquire_completion_xact_lock(
        session,
        completion_id,
        logger=logger,
    )


async def _acquire_lease(redis: Any, task_id: str, worker_token: str) -> None:
    await lease_runtime.acquire_lease(
        redis,
        task_id,
        worker_token,
        lease_ttl_s=_LEASE_TTL_S,
        lease_lost_error=_LeaseLost,
    )


async def _release_lease(redis: Any, task_id: str, worker_token: str) -> None:
    await lease_runtime.release_lease(
        redis,
        task_id,
        worker_token,
        logger=logger,
    )


async def _lease_renewer(
    redis: Any,
    task_id: str,
    worker_token: str,
    lease_lost: asyncio.Event | None = None,
) -> None:
    await lease_runtime.lease_renewer(
        redis,
        task_id,
        worker_token,
        lease_lost,
        lease_ttl_s=_LEASE_TTL_S,
        lease_renew_s=_LEASE_RENEW_S,
        logger=logger,
    )


async def _cleanup_completion_runtime(**kwargs: Any) -> None:
    await lease_runtime.cleanup_runtime(
        **kwargs,
        dependencies=lease_runtime.CleanupDependencies(
            release_lease=_release_lease,
            task_duration_seconds=task_duration_seconds,
            safe_outcome=safe_outcome,
            logger=logger,
        ),
    )


_attachment_to_data_url = _context_adapter.attachment_to_data_url
_message_to_input_item = _context_adapter.message_to_input_item
_build_input_from_packed_context = _context_adapter.build_input_from_packed_context
_load_rows_desc = _context_adapter.load_rows_desc
_load_rows_desc_after_summary = _context_adapter.load_rows_desc_after_summary
_get_message = _context_adapter.get_message
_pick_first_user_from_summary = _context_adapter.pick_first_user_from_summary
_pick_current_user_with_lookup = _context_adapter.pick_current_user_with_lookup
_resolve_summary_model = _context_adapter.resolve_summary_model
_resolve_int_setting = _context_adapter.resolve_int_setting
_ensure_context_summary = _context_adapter.ensure_context_summary
_context_loading_hooks = _context_adapter.context_loading_hooks
_pack_recent_history = _context_adapter.pack_recent_history
_build_input_from_history = _context_adapter.build_input_from_history


def _classify_exception(exc: BaseException, has_partial: bool) -> RetryDecision:
    return persistence_runtime.classify_exception(
        exc,
        has_partial,
        upstream_error_type=UpstreamError,
        billing_error_type=billing_core.BillingError,
        is_retriable=is_retriable,
        retry_decision_type=RetryDecision,
    )


def _bounded_next_attempt(current_attempt: int | None) -> tuple[int, bool]:
    return persistence_runtime.bounded_next_attempt(
        current_attempt,
        max_attempts=_MAX_ATTEMPTS,
    )


_inject_user_memory_context = _context_adapter.inject_user_memory_context
_record_completion_context_metadata = (
    _context_adapter.record_completion_context_metadata
)


async def _flush_completion_text(
    task_id: str,
    text: str,
    *,
    attempt_epoch: int,
    retries: int = _PG_FLUSH_RETRIES,
) -> None:
    await persistence_runtime.flush_completion_text(
        task_id,
        text,
        attempt_epoch=attempt_epoch,
        retries=retries,
        dependencies=persistence_runtime.FlushDependencies(
            session_factory=SessionLocal,
            completion_model=Completion,
            streaming_status=CompletionStatus.STREAMING.value,
            update=update,
            affected_rows=affected_rows,
            logger=logger,
            backoff_s=_PG_FLUSH_BACKOFF_S,
            upstream_error_type=UpstreamError,
            upstream_error_code=EC.UPSTREAM_ERROR.value,
        ),
    )


async def _completion_preflight_failure(
    session: Any,
    completion: Completion,
) -> tuple[int, tuple[str, str] | None]:
    return await persistence_runtime.completion_preflight_failure(
        session,
        completion,
        worker_billing=worker_billing,
        max_attempts=_MAX_ATTEMPTS,
    )


def build_completion_runtime(
    *,
    image_upstream_runtime: ImageUpstreamRuntime,
    storage_writes: StorageWriteCoordinator | None = None,
) -> CompletionRuntime:
    callbacks = composition.CompletionAdapterCallbacks(
        inject_user_memory_context=_inject_user_memory_context,
        pack_recent_history=_pack_recent_history,
        record_context_metadata=_record_completion_context_metadata,
        chat_tools_from_content=_chat_tools_from_content,
        configure_chat_tools=_configure_chat_tools,
        publish_tool_progress=_publish_completion_tool_progress,
        publish_tool_updates=_publish_completion_tool_updates,
        build_tool_image_service=_build_completion_tool_image_service,
        tool_limited_completion_body=_tool_limited_completion_body,
        completion_model=Completion,
        message_model=Message,
        session_factory=SessionLocal,
        acquire_completion_xact_lock=_acquire_completion_xact_lock,
        cleanup_completion_runtime=_cleanup_completion_runtime,
        flush_completion_text=_flush_completion_text,
        record_upstream_metadata=_record_completion_upstream_metadata,
        stream_completion=stream_completion,
        settle_failed_billing=_settle_failed_completion_billing,
        event_hooks=_COMPLETION_EVENT_HOOKS,
        deliver_event=_deliver_completion_event,
        stage_event=_stage_completion_event,
        tracer=_tracer,
        logger=logger,
        publish_event=publish_event,
        completion_epoch_superseded=_CompletionEpochSuperseded,
        lease_lost=_LeaseLost,
        task_cancelled=_TaskCancelled,
        tool_idle_timeout=_ToolIdleTimeout,
        acquire_lease=_acquire_lease,
        classify_exception=_classify_exception,
        completion_preflight_failure=_completion_preflight_failure,
        is_cancelled=_is_cancelled,
        iter_stream_with_abort=_iter_completion_stream_with_abort,
        lease_renewer=_lease_renewer,
        raise_if_cancelled=_raise_if_completion_cancelled,
        watch_cancel=_watch_completion_cancel,
        cancel_check_every_deltas=_CANCEL_CHECK_EVERY_DELTAS,
        cancel_poll_interval_s=_CANCEL_POLL_INTERVAL_S,
        max_attempts=_MAX_ATTEMPTS,
        max_tool_invocations=_MAX_TOOL_INVOCATIONS_DEFAULT,
        pg_flush_every_chars=_PG_FLUSH_EVERY_CHARS,
        running_statuses=_RUNNING_COMPLETION_STATUSES,
        tool_idle_timeout_s=_TOOL_IDLE_TIMEOUT_S_DEFAULT,
    )
    bindings = composition.build_bindings(
        callbacks=callbacks,
        image_upstream_runtime=image_upstream_runtime,
        storage_writes=storage_writes,
    )
    return composition.build_runtime(
        bindings=bindings,
        build_services=build_completion_services,
        runner=_run_completion,
        image_upstream_runtime=image_upstream_runtime,
    )
