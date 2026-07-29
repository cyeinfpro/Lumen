"""Stable imports re-exported by the completion runtime facade."""

from __future__ import annotations

# ruff: noqa: F401

import asyncio
import logging
from contextlib import suppress
from functools import partial
from typing import Any

import httpx
from PIL import Image as PILImage
from sqlalchemy import select, text as sa_text, update

from lumen_core import billing as billing_core
from lumen_core.byok_retention import (
    BYOK_DEFAULT_DELETE_ENABLED,
    ByokRetentionPolicy,
    applies_to_account_mode as byok_retention_applies_to_account_mode,
    cutoffs as byok_retention_cutoffs,
)
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
from lumen_core.models import (
    Completion,
    Conversation,
    Image,
    ImageVariant,
    Message,
    User,
    new_uuid7,
)
from lumen_core.pricing import parse_usage
from lumen_core.queue_metadata import completion_queue_metadata, merge_queue_metadata

from .... import billing as worker_billing
from .... import completion_billing, runtime_settings
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
from ....upstream_parts.responses import (
    extract_response_image_b64 as _extract_response_image_b64,
    extract_response_revised_prompt as _extract_response_revised_prompt,
)
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
    write_completion_image_files as _write_generation_files,
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
from ..contracts import CompletionCommand, CompletionResult
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
from ..runtime import CompletionRuntime
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

PUBLIC_FACADE_EXPORTS = (
    "_CODE_INTERPRETER_TOOL_TYPE",
    "_CompletionToolTracker",
    "_FILE_SEARCH_TOOL_TYPE",
    "_IMAGE_GENERATION_TOOL_TYPE",
    "_LeaseLost",
    "_STICKY_TEXT_CHAR_LIMIT",
    "_SummaryBoundary",
    "_TaskCancelled",
    "_ToolCallState",
    "_ToolIdleTimeout",
    "_WEB_SEARCH_TOOL_TYPE",
    "_apply_url_citations",
    "_context_circuit_open",
    "_decode_upstream_image_b64",
    "_estimated_summary_source",
    "_extract_completed_output_text",
    "_extract_image_events_from_response",
    "_extract_reasoning_delta",
    "_extract_reasoning_text_from_item",
    "_extract_reasoning_text_from_response",
    "_extract_tool_call_update",
    "_extract_url_citations",
    "_fallback_completion_usage_tokens",
    "_fallback_pack",
    "_finalize_completion_text",
    "_first_str",
    "_instructions_with_summary_guardrail",
    "_make_quality_probes",
    "_markdown_link",
    "_merge_tool_call_state",
    "_message_after_summary",
    "_message_created_at",
    "_normalize_tool_status",
    "_normalize_tool_type",
    "_pack_with_existing_summary",
    "_packed_with_input",
    "_role_eq",
    "_sticky_text_from_message",
    "_summarize_tool_error",
    "_summary_age_seconds",
    "_summary_compressed_at",
    "_summary_covers_boundary",
    "_summary_created_at",
    "_tool_display_label",
    "_tool_image_dedupe_key",
    "_tool_status_rank",
    "_truncate_sticky_text",
    "_with_summary_guardrail",
    "PackedContext",
    "run_completion",
)


__all__ = [name for name in tuple(locals()) if not name.startswith("__")]
