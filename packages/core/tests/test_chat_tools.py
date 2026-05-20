from __future__ import annotations

from lumen_core.chat_tools import (
    TOOL_TERMINAL_STATUSES,
    ToolStatus,
    is_terminal_tool_status,
    normalize_tool_idle_timeout_seconds,
    normalize_tool_status,
    tool_status_idle_timed_out,
    tool_status_idle_timeout_remaining_seconds,
)


def test_normalize_tool_status_maps_provider_variants() -> None:
    assert normalize_tool_status("in_progress") is ToolStatus.RUNNING
    assert normalize_tool_status("searching") is ToolStatus.RUNNING
    assert normalize_tool_status("completed") is ToolStatus.SUCCEEDED
    assert normalize_tool_status("incomplete") is ToolStatus.FAILED
    assert normalize_tool_status("interrupted") is ToolStatus.FAILED
    assert normalize_tool_status("requires_action") is ToolStatus.RUNNING
    assert normalize_tool_status("canceled") is ToolStatus.CANCELLED
    assert normalize_tool_status("timed_out") is ToolStatus.TIMED_OUT


def test_normalize_tool_status_infers_from_event_type_when_status_missing() -> None:
    assert (
        normalize_tool_status(None, event_type="response.web_search_call.searching")
        is ToolStatus.RUNNING
    )
    assert (
        normalize_tool_status(None, event_type="response.output_item.done")
        is ToolStatus.SUCCEEDED
    )
    assert (
        normalize_tool_status(None, event_type="response.code_interpreter_call.failed")
        is ToolStatus.FAILED
    )
    assert (
        normalize_tool_status(
            None, event_type="response.code_interpreter_call.interrupted"
        )
        is ToolStatus.FAILED
    )
    assert (
        normalize_tool_status(None, event_type="response.web_search_call.delta")
        is ToolStatus.RUNNING
    )
    assert (
        normalize_tool_status(None, event_type="response.requires_action")
        is ToolStatus.RUNNING
    )


def test_normalize_tool_status_uses_default_for_unknown_explicit_values() -> None:
    assert normalize_tool_status("provider_mystery") is ToolStatus.UNKNOWN
    assert (
        normalize_tool_status(
            "provider_mystery",
            event_type="response.output_item.done",
            default=ToolStatus.RUNNING,
        )
        is ToolStatus.RUNNING
    )


def test_normalize_tool_status_uses_default_only_when_status_is_missing() -> None:
    assert normalize_tool_status(None, default=ToolStatus.RUNNING) is ToolStatus.RUNNING


def test_terminal_status_helpers_accept_enum_and_string_values() -> None:
    assert is_terminal_tool_status(ToolStatus.SUCCEEDED) is True
    assert is_terminal_tool_status("failed") is True
    assert is_terminal_tool_status("running") is False
    assert TOOL_TERMINAL_STATUSES == {
        "succeeded",
        "failed",
        "cancelled",
        "timed_out",
    }


def test_normalize_tool_idle_timeout_seconds_preserves_zero_and_defaults() -> None:
    assert normalize_tool_idle_timeout_seconds("12.5", default=30.0) == 12.5
    assert normalize_tool_idle_timeout_seconds(0, default=30.0) == 0.0
    assert normalize_tool_idle_timeout_seconds(None, default=30.0) == 30.0
    assert normalize_tool_idle_timeout_seconds("bad", default=30.0) == 30.0
    assert normalize_tool_idle_timeout_seconds(-1, default=30.0) == 30.0


def test_tool_idle_timeout_helpers_use_last_tool_update_timestamp() -> None:
    assert tool_status_idle_timed_out(10.0, now=39.9, timeout_s=30.0) is False
    assert tool_status_idle_timed_out(10.0, now=40.0, timeout_s=30.0) is True
    assert tool_status_idle_timed_out(None, now=40.0, timeout_s=30.0) is False
    assert (
        tool_status_idle_timeout_remaining_seconds(
            10.0,
            now=25.0,
            timeout_s=30.0,
        )
        == 15.0
    )
    assert (
        tool_status_idle_timeout_remaining_seconds(
            10.0,
            now=40.0,
            timeout_s=30.0,
        )
        == 0.0
    )
