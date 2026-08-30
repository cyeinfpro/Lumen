from __future__ import annotations

import pytest

from lumen_core.agent_errors import (
    agent_error_allows_continuation,
    normalize_agent_error_code,
    public_agent_error_code,
    public_agent_error_message,
)


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("agent_output_limit_reached", "agent_output_truncated"),
        ("agent_continuation_unavailable", "agent_run_not_continuable"),
        ("agent_runtime_invalid_event", "agent_runtime_protocol_error"),
        ("agent_runtime_terminal_missing", "agent_runtime_protocol_error"),
    ],
)
def test_legacy_agent_errors_normalize_without_rewriting_rows(
    legacy: str,
    canonical: str,
) -> None:
    assert normalize_agent_error_code(legacy) == canonical
    assert public_agent_error_code(legacy) == canonical
    assert public_agent_error_message(legacy)


@pytest.mark.parametrize(
    "code",
    [
        "agent_output_truncated",
        "agent_output_limit_reached",
        "agent_run_timeout",
        "agent_runtime_shutdown",
        "agent_runtime_error",
        "agent_runtime_disconnected",
        "agent_runtime_invalid_event",
    ],
)
def test_partial_transport_and_legacy_errors_can_use_server_side_continuation(
    code: str,
) -> None:
    assert agent_error_allows_continuation(code)


def test_unknown_internal_error_does_not_leak_publicly() -> None:
    assert public_agent_error_code("upstream-secret-error") == "agent_error"
    assert public_agent_error_message("upstream-secret-error") == (
        "Agent run could not be completed"
    )
