"""Final typed composition for the completion runtime."""

from __future__ import annotations

# ruff: noqa: F401
import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

import httpx
from PIL import Image as PILImage
from sqlalchemy import select, update
from sqlalchemy import text as sa_text

from lumen_core import billing as billing_core
from lumen_core.byok_retention import (
    BYOK_DEFAULT_DELETE_ENABLED,
    ByokRetentionPolicy,
    applies_to_account_mode as byok_retention_applies_to_account_mode,
    cutoffs as byok_retention_cutoffs,
)
from ..bindings import (
    CompletionBillingBindings,
    CompletionBindings,
    CompletionContextBindings,
    CompletionEventBindings,
    CompletionLeaseRetryBindings,
    CompletionPersistenceBindings,
    CompletionToolBindings,
    CompletionUpstreamBindings,
)
from ..contracts import CompletionCommand, CompletionResult
from ..runtime import CompletionRuntime
from lumen_core.constants import (
    DEFAULT_CHAT_INSTRUCTIONS,
    DEFAULT_CHAT_MODEL,
    EV_COMP_IMAGE,
    EV_COMP_PROGRESS,
    CompletionStatus,
    GenerationErrorCode as EC,
    RETRY_BACKOFF_SECONDS,
)
from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    compose_summary_guardrail,
    count_tokens,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    get_input_budget,
)
from lumen_core.model_entities import (
    Completion,
    Conversation,
    Image,
    ImageVariant,
    Message,
    User,
)
from lumen_core.model_base import new_uuid7
from lumen_core.pricing import parse_usage
from lumen_core.queue_metadata import completion_queue_metadata, merge_queue_metadata

from .... import completion_billing, runtime_settings
from .... import billing as worker_billing
from ....byok_runtime import (
    byok_error_message,
    byok_error_to_generation_code,
    classify_user_credential_error,
    record_user_credential_runtime_error,
    resolve_user_credential_runtime,
)
from ....db import SessionLocal, affected_rows
from ....observability import (
    completion_cancel_check_errors_total,
    get_tracer,
    safe_outcome,
    task_duration_seconds,
    upstream_calls_total,
)
from ....provider_runtime.errors import UpstreamError
from ....provider_runtime.upstream_services import ImageUpstreamRuntime
from ....retry import RetryDecision, is_retriable
from ....sse_publish import publish_event as _publish_sse_event
from ....storage import storage
from ....storage_writes import StorageWriteCoordinator
from ....upstream_parts.responses_client import stream_completion
from ... import context_summary, memory_extraction
from ... import outbox as _completion_outbox
from ...state import is_completion_terminal
from .. import context_loading as _completion_context_loading
from .. import history as _completion_history
from .. import stream as _completion_stream
from .. import tool_images as _completion_tool_images
from ..artifact_codec import (
    compute_blurhash as _generation_compute_blurhash,
    make_display as _make_display,
    make_preview as _make_preview,
    make_thumb as _make_thumb,
    sha256 as _sha256,
)
from ..artifact_storage import (
    cleanup_completion_image_files_on_error as _cleanup_storage_on_error,
    delete_completion_image_files as _delete_storage_files,
    write_completion_image_files as _write_generation_files,
)
from ..citation_text import (
    apply_url_citations as _apply_url_citations,
    extract_completed_output_text as _extract_completed_output_text,
    extract_url_citations as _extract_url_citations,
    finalize_completion_text as _finalize_completion_text,
    markdown_link as _markdown_link,
)
from ..context import (
    PackedContext,
    estimated_summary_source as _estimated_summary_source,
    fallback_pack as _fallback_pack,
    make_quality_probes as _make_quality_probes,
    pack_with_existing_summary as _pack_with_existing_summary,
    packed_with_input as _packed_with_input,
)
from ..context_enrichment import (
    inject_user_memory_context,
    record_completion_context_metadata,
)
from ..context_loading import (
    context_circuit_open as _context_circuit_open,
    pick_current_user as _pick_current_user,
    pick_first_user as _pick_first_user,
)
from ..history import (
    STICKY_TEXT_CHAR_LIMIT as _STICKY_TEXT_CHAR_LIMIT,
    SummaryBoundary as _SummaryBoundary,
    instructions_with_summary_guardrail as _instructions_with_summary_guardrail,
    message_after_summary as _message_after_summary,
    message_created_at as _message_created_at,
    role_eq as _role_eq,
    sticky_text_from_message as _sticky_text_from_message,
    summary_age_seconds as _summary_age_seconds,
    summary_compressed_at as _summary_compressed_at,
    summary_covers_boundary as _summary_covers_boundary,
    summary_created_at as _summary_created_at,
    truncate_sticky_text as _truncate_sticky_text,
    with_summary_guardrail as _with_summary_guardrail,
)
from ..image_storage_runtime import (
    CompletionToolImageBudget,
    CompletionToolImageCodec,
    CompletionToolImageEvents,
    CompletionToolImageRepository,
    CompletionToolImageService,
    CompletionToolImageStorage,
)
from ..request_metadata import (
    completion_upstream_provider_event as _completion_upstream_provider_event,
    content_str_list as _content_str_list,
    merge_completion_upstream_metadata as _merge_completion_upstream_metadata,
    normalize_reasoning_effort_for_upstream as _normalize_reasoning_effort_for_upstream,
    split_csv_ids as _split_csv_ids,
)
from ..runner import run_completion as _run_completion
from ..services import build_completion_services
from ..stream import (
    LeaseLost as _LeaseLost,
    TaskCancelled as _TaskCancelled,
    ToolIdleTimeout as _ToolIdleTimeout,
    extract_reasoning_delta as _extract_reasoning_delta,
    extract_reasoning_text_from_item as _extract_reasoning_text_from_item,
    extract_reasoning_text_from_response as _extract_reasoning_text_from_response,
    next_completion_stream_event as _next_completion_stream_event,
    raise_for_terminal_response_event as _raise_for_terminal_response_event,
)
from ..tool_images import (
    CompletionUsageAccumulator as _CompletionUsageAccumulator,
    completion_event_payload as _completion_event_payload,
    decode_upstream_image_b64 as _decode_upstream_image_b64,
    estimate_completion_request_input_tokens as _estimate_completion_request_input_tokens,
    estimate_completion_tool_output_tokens as _estimate_completion_tool_output_tokens,
    extract_image_events_from_response as _extract_image_events_from_response,
    fallback_completion_usage_tokens as _fallback_completion_usage_tokens,
    settle_cancelled_completion_billing as _settle_cancelled_completion_billing,
    tool_image_dedupe_key as _tool_image_dedupe_key,
)
from ..tool_state import (
    CODE_INTERPRETER_TOOL_TYPE as _CODE_INTERPRETER_TOOL_TYPE,
    CompletionToolTracker as _CompletionToolTracker,
    FILE_SEARCH_TOOL_TYPE as _FILE_SEARCH_TOOL_TYPE,
    IMAGE_GENERATION_TOOL_TYPE as _IMAGE_GENERATION_TOOL_TYPE,
    ToolCallState as _ToolCallState,
    WEB_SEARCH_TOOL_TYPE as _WEB_SEARCH_TOOL_TYPE,
    extract_tool_call_update as _extract_tool_call_update,
    first_str as _first_str,
    merge_tool_call_state as _merge_tool_call_state,
    normalize_tool_status as _normalize_tool_status,
    normalize_tool_type as _normalize_tool_type,
    summarize_tool_error as _summarize_tool_error,
    tool_display_label as _tool_display_label,
    tool_status_rank as _tool_status_rank,
)
from ....upstream_parts.responses import (
    extract_response_image_b64 as _extract_response_image_b64,
    extract_response_revised_prompt as _extract_response_revised_prompt,
)

__all__ = [name for name in tuple(locals()) if not name.startswith("__")]


@dataclass(frozen=True, slots=True)
class CompletionAdapterCallbacks:
    inject_user_memory_context: Any
    pack_recent_history: Any
    record_context_metadata: Any
    chat_tools_from_content: Any
    configure_chat_tools: Any
    publish_tool_progress: Any
    publish_tool_updates: Any
    build_tool_image_service: Any
    tool_limited_completion_body: Any
    completion_model: Any
    message_model: Any
    session_factory: Any
    acquire_completion_xact_lock: Any
    cleanup_completion_runtime: Any
    flush_completion_text: Any
    record_upstream_metadata: Any
    stream_completion: Any
    settle_failed_billing: Any
    event_hooks: Any
    deliver_event: Any
    stage_event: Any
    tracer: Any
    logger: Any
    publish_event: Any
    completion_epoch_superseded: Any
    lease_lost: Any
    task_cancelled: Any
    tool_idle_timeout: Any
    acquire_lease: Any
    classify_exception: Any
    completion_preflight_failure: Any
    is_cancelled: Any
    iter_stream_with_abort: Any
    lease_renewer: Any
    raise_if_cancelled: Any
    watch_cancel: Any
    cancel_check_every_deltas: int
    cancel_poll_interval_s: float
    max_attempts: int
    max_tool_invocations: int
    pg_flush_every_chars: int
    running_statuses: tuple[str, ...]
    tool_idle_timeout_s: float


def build_bindings(
    *,
    callbacks: CompletionAdapterCallbacks,
    image_upstream_runtime: Any,
    storage_writes: Any | None,
) -> CompletionBindings:
    return CompletionBindings(
        context=CompletionContextBindings(
            DEFAULT_CHAT_MODEL=DEFAULT_CHAT_MODEL,
            _inject_user_memory_context=callbacks.inject_user_memory_context,
            _instructions_with_summary_guardrail=_instructions_with_summary_guardrail,
            _pack_recent_history=callbacks.pack_recent_history,
            _record_completion_context_metadata=callbacks.record_context_metadata,
            runtime_settings=runtime_settings,
        ),
        tools=CompletionToolBindings(
            _CompletionToolTracker=_CompletionToolTracker,
            _CompletionUsageAccumulator=_CompletionUsageAccumulator,
            _chat_tools_from_content=callbacks.chat_tools_from_content,
            _completion_tool_images=_completion_tool_images,
            _configure_chat_tools=callbacks.configure_chat_tools,
            _estimate_completion_request_input_tokens=(
                _estimate_completion_request_input_tokens
            ),
            _estimate_completion_tool_output_tokens=(
                _estimate_completion_tool_output_tokens
            ),
            _extract_image_events_from_response=_extract_image_events_from_response,
            _extract_response_image_b64=_extract_response_image_b64,
            _extract_response_revised_prompt=_extract_response_revised_prompt,
            _publish_completion_tool_progress=callbacks.publish_tool_progress,
            _publish_completion_tool_updates=callbacks.publish_tool_updates,
            tool_image_service=callbacks.build_tool_image_service(storage_writes),
            _summarize_tool_error=_summarize_tool_error,
            _tool_image_dedupe_key=_tool_image_dedupe_key,
            _tool_limited_completion_body=callbacks.tool_limited_completion_body,
        ),
        persistence=CompletionPersistenceBindings(
            Completion=callbacks.completion_model,
            Message=callbacks.message_model,
            SessionLocal=callbacks.session_factory,
            User=User,
            _acquire_completion_xact_lock=callbacks.acquire_completion_xact_lock,
            _cleanup_completion_runtime=callbacks.cleanup_completion_runtime,
            _flush_completion_text=callbacks.flush_completion_text,
            affected_rows=affected_rows,
            is_completion_terminal=is_completion_terminal,
            new_uuid7=new_uuid7,
            select=select,
            update=update,
        ),
        upstream=CompletionUpstreamBindings(
            UpstreamError=UpstreamError,
            _apply_url_citations=_apply_url_citations,
            _completion_upstream_provider_event=_completion_upstream_provider_event,
            _extract_completed_output_text=_extract_completed_output_text,
            _extract_reasoning_delta=_extract_reasoning_delta,
            _extract_reasoning_text_from_response=_extract_reasoning_text_from_response,
            _extract_url_citations=_extract_url_citations,
            _finalize_completion_text=_finalize_completion_text,
            _merge_completion_upstream_metadata=_merge_completion_upstream_metadata,
            _normalize_reasoning_effort_for_upstream=(
                _normalize_reasoning_effort_for_upstream
            ),
            _raise_for_terminal_response_event=_raise_for_terminal_response_event,
            _record_completion_upstream_metadata=callbacks.record_upstream_metadata,
            stream_completion=partial(
                callbacks.stream_completion,
                runtime=image_upstream_runtime,
            ),
        ),
        billing=CompletionBillingBindings(
            _fallback_completion_tool_image_tokens=(
                completion_billing.fallback_completion_tool_image_tokens
            ),
            _settle_cancelled_completion_billing=(_settle_cancelled_completion_billing),
            _settle_failed_completion_billing=callbacks.settle_failed_billing,
            byok_error_message=byok_error_message,
            byok_error_to_generation_code=byok_error_to_generation_code,
            classify_user_credential_error=classify_user_credential_error,
            parse_usage=parse_usage,
            record_user_credential_runtime_error=(record_user_credential_runtime_error),
            resolve_user_credential_runtime=resolve_user_credential_runtime,
            worker_billing=worker_billing,
        ),
        events=CompletionEventBindings(
            _COMPLETION_EVENT_HOOKS=callbacks.event_hooks,
            _completion_event_payload=_completion_event_payload,
            _deliver_completion_event=callbacks.deliver_event,
            _stage_completion_event=callbacks.stage_event,
            _tracer=callbacks.tracer,
            logger=callbacks.logger,
            memory_extraction=memory_extraction,
            publish_event=callbacks.publish_event,
            upstream_calls_total=upstream_calls_total,
        ),
        retry=CompletionLeaseRetryBindings(
            RETRY_BACKOFF_SECONDS=RETRY_BACKOFF_SECONDS,
            RetryDecision=RetryDecision,
            _CANCEL_CHECK_EVERY_DELTAS=callbacks.cancel_check_every_deltas,
            _CANCEL_POLL_INTERVAL_S=callbacks.cancel_poll_interval_s,
            _CompletionEpochSuperseded=callbacks.completion_epoch_superseded,
            _LeaseLost=callbacks.lease_lost,
            _MAX_ATTEMPTS=callbacks.max_attempts,
            _MAX_TOOL_INVOCATIONS_DEFAULT=callbacks.max_tool_invocations,
            _PG_FLUSH_EVERY_CHARS=callbacks.pg_flush_every_chars,
            _RUNNING_COMPLETION_STATUSES=callbacks.running_statuses,
            _TOOL_IDLE_TIMEOUT_S_DEFAULT=callbacks.tool_idle_timeout_s,
            _TaskCancelled=callbacks.task_cancelled,
            _ToolIdleTimeout=callbacks.tool_idle_timeout,
            _acquire_lease=callbacks.acquire_lease,
            _classify_exception=callbacks.classify_exception,
            _completion_preflight_failure=callbacks.completion_preflight_failure,
            _is_cancelled=callbacks.is_cancelled,
            _iter_completion_stream_with_abort=callbacks.iter_stream_with_abort,
            _lease_renewer=callbacks.lease_renewer,
            _raise_if_completion_cancelled=callbacks.raise_if_cancelled,
            _watch_completion_cancel=callbacks.watch_cancel,
            completion_queue_metadata=completion_queue_metadata,
            merge_queue_metadata=merge_queue_metadata,
        ),
    )


def build_runtime(
    *,
    bindings: Any,
    build_services: Callable[[Any], Any],
    runner: Callable[..., Any],
    image_upstream_runtime: Any,
) -> CompletionRuntime:
    services = build_services(bindings)

    async def execute(command: CompletionCommand) -> CompletionResult:
        return await runner(command, bindings, services)

    return CompletionRuntime(
        services=services,
        runner=execute,
        image_upstream_runtime=image_upstream_runtime,
    )
