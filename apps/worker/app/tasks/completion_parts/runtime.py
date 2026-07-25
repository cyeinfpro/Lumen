from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...task_runtime import RuntimeSlot


@dataclass(frozen=True, slots=True)
class CompletionPorts:
    Completion: Any
    DEFAULT_CHAT_MODEL: Any
    Message: Any
    RETRY_BACKOFF_SECONDS: Any
    RetryDecision: Any
    SessionLocal: Any
    UpstreamError: Any
    User: Any
    _CANCEL_CHECK_EVERY_DELTAS: Any
    _CANCEL_POLL_INTERVAL_S: Any
    _COMPLETION_EVENT_HOOKS: Any
    _CompletionEpochSuperseded: Any
    _CompletionToolTracker: Any
    _CompletionUsageAccumulator: Any
    _LeaseLost: Any
    _MAX_ATTEMPTS: Any
    _MAX_TOOL_INVOCATIONS_DEFAULT: Any
    _PG_FLUSH_EVERY_CHARS: Any
    _RUNNING_COMPLETION_STATUSES: Any
    _TOOL_IDLE_TIMEOUT_S_DEFAULT: Any
    _TaskCancelled: Any
    _ToolIdleTimeout: Any
    _acquire_completion_xact_lock: Any
    _acquire_lease: Any
    _apply_url_citations: Any
    _chat_tools_from_content: Any
    _classify_exception: Any
    _cleanup_completion_runtime: Any
    _completion_event_payload: Any
    _completion_preflight_failure: Any
    _completion_tool_images: Any
    _completion_upstream_provider_event: Any
    _configure_chat_tools: Any
    _deliver_completion_event: Any
    _estimate_completion_request_input_tokens: Any
    _estimate_completion_tool_output_tokens: Any
    _extract_completed_output_text: Any
    _extract_image_events_from_response: Any
    _extract_reasoning_delta: Any
    _extract_reasoning_text_from_response: Any
    _extract_response_image_b64: Any
    _extract_response_revised_prompt: Any
    _extract_url_citations: Any
    _fallback_completion_tool_image_tokens: Any
    _finalize_completion_text: Any
    _flush_completion_text: Any
    _inject_user_memory_context: Any
    _instructions_with_summary_guardrail: Any
    _is_cancelled: Any
    _iter_completion_stream_with_abort: Any
    _lease_renewer: Any
    _merge_completion_upstream_metadata: Any
    _normalize_reasoning_effort_for_upstream: Any
    _pack_recent_history: Any
    _publish_completion_tool_progress: Any
    _publish_completion_tool_updates: Any
    _raise_for_terminal_response_event: Any
    _raise_if_completion_cancelled: Any
    _record_completion_context_metadata: Any
    _record_completion_upstream_metadata: Any
    _settle_cancelled_completion_billing: Any
    _settle_failed_completion_billing: Any
    _stage_completion_event: Any
    _store_and_publish_completion_tool_image: Any
    _summarize_tool_error: Any
    _tool_image_dedupe_key: Any
    _tool_limited_completion_body: Any
    _tracer: Any
    _watch_completion_cancel: Any
    affected_rows: Any
    byok_error_message: Any
    byok_error_to_generation_code: Any
    classify_user_credential_error: Any
    completion_queue_metadata: Any
    is_completion_terminal: Any
    logger: Any
    memory_extraction: Any
    merge_queue_metadata: Any
    new_uuid7: Any
    parse_usage: Any
    publish_event: Any
    record_user_credential_runtime_error: Any
    resolve_user_credential_runtime: Any
    runtime_settings: Any
    select: Any
    stream_completion: Any
    update: Any
    upstream_calls_total: Any
    worker_billing: Any


_COMPLETION_PORTS: RuntimeSlot[CompletionPorts] = RuntimeSlot("completion-ports")


def install_completion_ports(ports: CompletionPorts) -> None:
    _COMPLETION_PORTS.install_default(ports)


def completion_ports() -> CompletionPorts:
    return _COMPLETION_PORTS.current()


@dataclass(frozen=True, slots=True)
class CompletionRuntime:
    ports: CompletionPorts
    runner: Any

    async def run(self, ctx: dict[str, Any], task_id: str) -> None:
        with _COMPLETION_PORTS.use(self.ports):
            await self.runner(ctx, task_id)
