from __future__ import annotations

import pytest
from pydantic import ValidationError

from lumen_core.schema_models import AgentEventEnvelope


_BASE = {
    "agent_session_id": "session-1",
    "agent_run_id": "run-1",
    "assistant_message_id": "message-1",
    "execution_epoch": 1,
    "event_seq": 1,
}


def test_replacement_events_and_snapshot_marker_are_independently_typed() -> None:
    delta = AgentEventEnvelope(
        **_BASE,
        event_name="agent.output.delta",
        text_delta="replacement",
        text_operation="replace",
        output_revision=1,
        output_runtime_seq=7,
        blocks=[{"kind": "text", "turn": 1, "text": "replacement"}],
    )
    marker = AgentEventEnvelope(
        **_BASE,
        event_name="agent.output.reset",
        snapshot_required=True,
        output_revision=1,
        output_runtime_seq=7,
    )
    assert delta.text_operation == "replace"
    assert marker.snapshot_required is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            **_BASE,
            "event_name": "agent.output.delta",
            "text_delta": "missing operation",
        },
        {
            **_BASE,
            "event_name": "agent.output.reset",
            "snapshot_required": True,
            "replacement_text": "must not accompany marker",
        },
        {
            **_BASE,
            "event_name": "agent.output.reset",
            "text_operation": "replace",
            "replacement_text": "x" * 20_001,
        },
        {
            **_BASE,
            "event_name": "agent.output.reset",
            "text_operation": "replace",
            "replacement_text": "replacement",
            "blocks": [
                {"kind": "text", "turn": index + 1, "text": "x"} for index in range(33)
            ],
        },
        {
            **_BASE,
            "event_name": "agent.run.failed",
            "error_code": "agent_error",
            "unsupported_internal": "leak",
        },
    ],
)
def test_invalid_or_undeclared_event_payloads_fail_closed(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AgentEventEnvelope.model_validate(payload)
