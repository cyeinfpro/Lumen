"""Canonical public Agent error aliases and continuation policy."""

from __future__ import annotations

from types import MappingProxyType


AGENT_ERROR_ALIASES = MappingProxyType(
    {
        "agent_output_limit_reached": "agent_output_truncated",
        "agent_continuation_unavailable": "agent_run_not_continuable",
        "agent_runtime_invalid_event": "agent_runtime_protocol_error",
        "agent_runtime_invalid_framing": "agent_runtime_protocol_error",
        "agent_runtime_line_too_large": "agent_runtime_protocol_error",
        "agent_runtime_truncated_line": "agent_runtime_protocol_error",
        "agent_runtime_terminal_missing": "agent_runtime_protocol_error",
        "agent_runtime_event_scope_mismatch": "agent_runtime_protocol_error",
        "agent_runtime_event_after_terminal": "agent_runtime_protocol_error",
        "agent_runtime_usage_out_of_bounds": "agent_runtime_protocol_error",
        "agent_runtime_invalid_response": "agent_runtime_protocol_error",
    }
)

AGENT_PUBLIC_ERROR_MESSAGES = MappingProxyType(
    {
        "agent_cancelled": "Agent run was cancelled",
        "agent_provider_unavailable": "Agent provider is unavailable",
        "agent_provider_protocol_error": "The provider returned an incompatible response",
        "agent_provider_empty_response": "The provider returned no usable response",
        "agent_runtime_unavailable": "Agent runtime is unavailable",
        "agent_runtime_error": "Agent runtime could not complete the run",
        "agent_runtime_protocol_error": "Agent runtime returned an invalid stream",
        "agent_runtime_disconnected": "Agent runtime stream disconnected",
        "agent_runtime_event_timeout": "Agent runtime stream timed out",
        "agent_runtime_shutdown": "Agent runtime stopped for maintenance",
        "agent_run_timeout": "Agent run reached its time limit",
        "agent_runtime_request_too_large": "Agent request exceeds the Runtime transport limit",
        "agent_provider_configuration_invalid": "Agent provider configuration is invalid",
        "agent_output_truncated": "Model output reached its length limit",
        "agent_safety_budget_reached": "Agent safety budget reached",
        "content_policy_violation": "The request cannot be processed under the content policy",
        "agent_tool_result_unknown": "Image submission result is unknown",
        "agent_tool_failed": "Image submission failed",
        "agent_tool_limit_reached": "Agent image tool limit reached",
        "agent_image_limit_reached": "Agent image count limit reached",
        "agent_reference_not_found": "A referenced image is unavailable",
        "agent_session_reference_limit_reached": "Agent session image limit reached",
        "agent_vision_model_unavailable": "Image input is unavailable for this model",
        "agent_reasoning_model_unavailable": "Reasoning is unavailable for this model",
        "agent_context_window_exceeded": "The model context window is insufficient",
        "agent_run_not_continuable": "This Agent run cannot be continued safely",
        "INSUFFICIENT_BALANCE": "Insufficient wallet balance",
        "NO_ACTIVE_API_KEY": "No active API key is available",
    }
)

AGENT_CONTINUATION_CODES = frozenset(
    {
        "agent_output_truncated",
        "agent_safety_budget_reached",
        "agent_runtime_disconnected",
        "agent_runtime_event_timeout",
        "agent_runtime_shutdown",
        "agent_run_timeout",
        "agent_runtime_error",
        "agent_runtime_protocol_error",
    }
)


def normalize_agent_error_code(code: str | None) -> str | None:
    if not code:
        return None
    return AGENT_ERROR_ALIASES.get(code, code)


def public_agent_error_code(code: str | None) -> str | None:
    normalized = normalize_agent_error_code(code)
    if normalized is None:
        return None
    if normalized in AGENT_PUBLIC_ERROR_MESSAGES:
        return normalized
    if normalized.startswith("agent_runtime_"):
        return "agent_runtime_protocol_error"
    return "agent_error"


def public_agent_error_message(code: str | None) -> str | None:
    public_code = public_agent_error_code(code)
    if public_code is None:
        return None
    return AGENT_PUBLIC_ERROR_MESSAGES.get(
        public_code,
        "Agent run could not be completed",
    )


def agent_error_allows_continuation(code: str | None) -> bool:
    return normalize_agent_error_code(code) in AGENT_CONTINUATION_CODES


__all__ = [
    "AGENT_CONTINUATION_CODES",
    "AGENT_ERROR_ALIASES",
    "AGENT_PUBLIC_ERROR_MESSAGES",
    "agent_error_allows_continuation",
    "normalize_agent_error_code",
    "public_agent_error_code",
    "public_agent_error_message",
]
