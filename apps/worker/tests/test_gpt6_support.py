from types import SimpleNamespace

import pytest

from app.agent_context import _runtime_reasoning_effort
from app.tasks.completion_parts.request_metadata import _normalize_reasoning_effort_for_upstream
from lumen_core.text_model_capabilities import normalize_model_reasoning


@pytest.mark.parametrize("effort,expected", [(None, None), ("none", "low"), ("minimal", "low"), ("low", "low"), ("max", "max")])
def test_agent_astra_normalizes_legacy_drafts_and_preserves_auto(effort, expected):
    run = SimpleNamespace(model="gpt-6-astra", reasoning_effort=effort)
    provider = SimpleNamespace(agent_reasoning_supported=True)
    assert _runtime_reasoning_effort(run, provider) == expected


def test_chat_astra_normalization_precedes_legacy_minimal_migration():
    assert _normalize_reasoning_effort_for_upstream(normalize_model_reasoning("gpt-6-astra", "minimal")) == "low"
    assert _normalize_reasoning_effort_for_upstream(normalize_model_reasoning("gpt-5.6-sol", "minimal")) == "none"
