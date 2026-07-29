"""Final typed composition for the completion runtime."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

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
from lumen_core.constants import DEFAULT_CHAT_MODEL, RETRY_BACKOFF_SECONDS
from lumen_core.models import User, new_uuid7
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
from ....db import affected_rows
from ....observability import upstream_calls_total
from ....provider_runtime.errors import UpstreamError
from ....retry import RetryDecision
from ... import memory_extraction
from ...state import is_completion_terminal
from .. import tool_images as completion_tool_images
from ..citation_text import (
    apply_url_citations,
    extract_completed_output_text,
    extract_url_citations,
    finalize_completion_text,
)
from ..history import instructions_with_summary_guardrail
from ..request_metadata import (
    completion_upstream_provider_event,
    merge_completion_upstream_metadata,
    normalize_reasoning_effort_for_upstream,
)
from ..stream import (
    extract_reasoning_delta,
    extract_reasoning_text_from_response,
    raise_for_terminal_response_event,
)
from ..tool_images import (
    CompletionUsageAccumulator,
    completion_event_payload,
    estimate_completion_request_input_tokens,
    estimate_completion_tool_output_tokens,
    extract_image_events_from_response,
    settle_cancelled_completion_billing,
    tool_image_dedupe_key,
)
from ..tool_state import CompletionToolTracker, summarize_tool_error
from ....upstream_parts.responses import (
    extract_response_image_b64,
    extract_response_revised_prompt,
)

from .compat import (
    select,
    update,
)


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
            _instructions_with_summary_guardrail=instructions_with_summary_guardrail,
            _pack_recent_history=callbacks.pack_recent_history,
            _record_completion_context_metadata=callbacks.record_context_metadata,
            runtime_settings=runtime_settings,
        ),
        tools=CompletionToolBindings(
            _CompletionToolTracker=CompletionToolTracker,
            _CompletionUsageAccumulator=CompletionUsageAccumulator,
            _chat_tools_from_content=callbacks.chat_tools_from_content,
            _completion_tool_images=completion_tool_images,
            _configure_chat_tools=callbacks.configure_chat_tools,
            _estimate_completion_request_input_tokens=(
                estimate_completion_request_input_tokens
            ),
            _estimate_completion_tool_output_tokens=(
                estimate_completion_tool_output_tokens
            ),
            _extract_image_events_from_response=extract_image_events_from_response,
            _extract_response_image_b64=extract_response_image_b64,
            _extract_response_revised_prompt=extract_response_revised_prompt,
            _publish_completion_tool_progress=callbacks.publish_tool_progress,
            _publish_completion_tool_updates=callbacks.publish_tool_updates,
            tool_image_service=callbacks.build_tool_image_service(storage_writes),
            _summarize_tool_error=summarize_tool_error,
            _tool_image_dedupe_key=tool_image_dedupe_key,
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
            _apply_url_citations=apply_url_citations,
            _completion_upstream_provider_event=completion_upstream_provider_event,
            _extract_completed_output_text=extract_completed_output_text,
            _extract_reasoning_delta=extract_reasoning_delta,
            _extract_reasoning_text_from_response=extract_reasoning_text_from_response,
            _extract_url_citations=extract_url_citations,
            _finalize_completion_text=finalize_completion_text,
            _merge_completion_upstream_metadata=merge_completion_upstream_metadata,
            _normalize_reasoning_effort_for_upstream=(
                normalize_reasoning_effort_for_upstream
            ),
            _raise_for_terminal_response_event=raise_for_terminal_response_event,
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
            _settle_cancelled_completion_billing=(settle_cancelled_completion_billing),
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
            _completion_event_payload=completion_event_payload,
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
