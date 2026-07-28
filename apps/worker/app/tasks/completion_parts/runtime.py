from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from .image_storage_runtime import CompletionToolImageService


class CompletionStream(Protocol):
    def __call__(
        self,
        body: dict[str, Any],
        *,
        runtime_override: Any | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...


class CompletionRunner(Protocol):
    async def __call__(
        self,
        ctx: dict[str, Any],
        task_id: str,
        ports: "CompletionPorts",
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CompletionContextPorts:
    DEFAULT_CHAT_MODEL: Any
    _inject_user_memory_context: Any
    _instructions_with_summary_guardrail: Any
    _pack_recent_history: Any
    _record_completion_context_metadata: Any
    runtime_settings: Any


@dataclass(frozen=True, slots=True)
class CompletionToolsPorts:
    _CompletionToolTracker: Any
    _CompletionUsageAccumulator: Any
    _chat_tools_from_content: Any
    _completion_tool_images: Any
    _configure_chat_tools: Any
    _estimate_completion_request_input_tokens: Any
    _estimate_completion_tool_output_tokens: Any
    _extract_image_events_from_response: Any
    _extract_response_image_b64: Any
    _extract_response_revised_prompt: Any
    _publish_completion_tool_progress: Any
    _publish_completion_tool_updates: Any
    tool_image_service: CompletionToolImageService
    _summarize_tool_error: Any
    _tool_image_dedupe_key: Any
    _tool_limited_completion_body: Any


@dataclass(frozen=True, slots=True)
class CompletionPersistencePorts:
    Completion: Any
    Message: Any
    SessionLocal: Any
    User: Any
    _acquire_completion_xact_lock: Any
    _cleanup_completion_runtime: Any
    _flush_completion_text: Any
    affected_rows: Any
    is_completion_terminal: Any
    new_uuid7: Any
    select: Any
    update: Any


@dataclass(frozen=True, slots=True)
class CompletionUpstreamPorts:
    UpstreamError: Any
    _apply_url_citations: Any
    _completion_upstream_provider_event: Any
    _extract_completed_output_text: Any
    _extract_reasoning_delta: Any
    _extract_reasoning_text_from_response: Any
    _extract_url_citations: Any
    _finalize_completion_text: Any
    _merge_completion_upstream_metadata: Any
    _normalize_reasoning_effort_for_upstream: Any
    _raise_for_terminal_response_event: Any
    _record_completion_upstream_metadata: Any
    stream_completion: CompletionStream


@dataclass(frozen=True, slots=True)
class CompletionBillingPorts:
    _fallback_completion_tool_image_tokens: Any
    _settle_cancelled_completion_billing: Any
    _settle_failed_completion_billing: Any
    byok_error_message: Any
    byok_error_to_generation_code: Any
    classify_user_credential_error: Any
    parse_usage: Any
    record_user_credential_runtime_error: Any
    resolve_user_credential_runtime: Any
    worker_billing: Any


@dataclass(frozen=True, slots=True)
class CompletionEventsPorts:
    _COMPLETION_EVENT_HOOKS: Any
    _completion_event_payload: Any
    _deliver_completion_event: Any
    _stage_completion_event: Any
    _tracer: Any
    logger: Any
    memory_extraction: Any
    publish_event: Any
    upstream_calls_total: Any


@dataclass(frozen=True, slots=True)
class CompletionRetryPorts:
    RETRY_BACKOFF_SECONDS: Any
    RetryDecision: Any
    _CANCEL_CHECK_EVERY_DELTAS: Any
    _CANCEL_POLL_INTERVAL_S: Any
    _CompletionEpochSuperseded: Any
    _LeaseLost: Any
    _MAX_ATTEMPTS: Any
    _MAX_TOOL_INVOCATIONS_DEFAULT: Any
    _PG_FLUSH_EVERY_CHARS: Any
    _RUNNING_COMPLETION_STATUSES: Any
    _TOOL_IDLE_TIMEOUT_S_DEFAULT: Any
    _TaskCancelled: Any
    _ToolIdleTimeout: Any
    _acquire_lease: Any
    _classify_exception: Any
    _completion_preflight_failure: Any
    _is_cancelled: Any
    _iter_completion_stream_with_abort: Any
    _lease_renewer: Any
    _raise_if_completion_cancelled: Any
    _watch_completion_cancel: Any
    completion_queue_metadata: Any
    merge_queue_metadata: Any


@dataclass(frozen=True, slots=True)
class CompletionPorts:
    context: CompletionContextPorts
    tools: CompletionToolsPorts
    persistence: CompletionPersistencePorts
    upstream: CompletionUpstreamPorts
    billing: CompletionBillingPorts
    events: CompletionEventsPorts
    retry: CompletionRetryPorts


@dataclass(frozen=True, slots=True)
class CompletionRuntime:
    ports: CompletionPorts
    runner: CompletionRunner
    image_upstream_runtime: ImageUpstreamRuntime

    async def run(self, ctx: dict[str, Any], task_id: str) -> None:
        await self.runner(ctx, task_id, self.ports)
