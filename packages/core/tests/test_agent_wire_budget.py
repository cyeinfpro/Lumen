from __future__ import annotations

from lumen_core.agent_wire_budget import (
    AGENT_RUNTIME_CREDENTIAL_HEADROOM_BYTES,
    AGENT_RUNTIME_REFERENCE_PREVIEW_MAX_BYTES,
    AGENT_RUNTIME_REQUEST_SAFETY_MARGIN_BYTES,
    encoded_json_bytes,
    estimate_agent_runtime_request_bytes,
)


def test_wire_budget_counts_json_escaping_base64_and_headroom() -> None:
    budget = estimate_agent_runtime_request_bytes(
        system_prompt='system "\\\x00" 中文',
        current_prompt="😀" * 100,
        history_texts=['history "\\"', "more 中文"],
        history_structured_bytes=encoded_json_bytes({"tool": {"prompt": "x" * 1_000}}),
        current_reference_count=2,
        historical_reference_count=3,
        maximum_bytes=10_000_000,
        preview_max_bytes=128 * 1024,
    )
    assert budget.admitted is True
    assert budget.estimated_bytes > (
        5 * 128 * 1024
        + AGENT_RUNTIME_CREDENTIAL_HEADROOM_BYTES
        + AGENT_RUNTIME_REQUEST_SAFETY_MARGIN_BYTES
    )


def test_wire_budget_rejects_before_dispatch_when_mandatory_input_exceeds_limit() -> (
    None
):
    budget = estimate_agent_runtime_request_bytes(
        system_prompt="system",
        current_prompt="x" * 10_000,
        history_texts=(),
        history_structured_bytes=0,
        current_reference_count=16,
        historical_reference_count=0,
        maximum_bytes=64 * 1024,
        preview_max_bytes=128 * 1024,
    )
    assert budget.admitted is False
    assert budget.estimated_bytes > budget.maximum_bytes


def test_wire_budget_uses_worker_maximum_for_sixteen_mandatory_previews() -> None:
    assert AGENT_RUNTIME_REFERENCE_PREVIEW_MAX_BYTES == 512 * 1024
    budget = estimate_agent_runtime_request_bytes(
        system_prompt="system",
        current_prompt="current",
        history_texts=(),
        history_structured_bytes=0,
        current_reference_count=16,
        historical_reference_count=0,
        maximum_bytes=8 * 1024 * 1024,
    )
    assert budget.admitted is False
    assert budget.estimated_bytes > 10 * 1024 * 1024
