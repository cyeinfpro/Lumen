from __future__ import annotations

import pytest

from lumen_core.text_model_capabilities import (
    agent_model_api_supported,
    gpt6_model_family,
    normalize_model_reasoning,
    normalize_responses_model_body,
)


@pytest.mark.parametrize("prefix", ["", "openai/", "gateway:openai/"])
@pytest.mark.parametrize("family", ["astra", "sol", "luna"])
def test_gpt6_ids_and_dated_snapshots(prefix: str, family: str) -> None:
    assert gpt6_model_family(f"{prefix}gpt-6-{family}") == family
    assert gpt6_model_family(f"{prefix}gpt-6-{family}-2026-09-29") == family


@pytest.mark.parametrize("model", [None, "gpt-6", "gpt-6-astra-pro", "gpt-6-astra-fake", "gpt-5.6-sol"])
def test_unverified_ids_are_not_guessed(model: str | None) -> None:
    assert gpt6_model_family(model) is None


@pytest.mark.parametrize("effort", ["none", "off", "minimal"])
def test_astra_legacy_off_uses_low(effort: str) -> None:
    assert normalize_model_reasoning("openai/gpt-6-astra", effort) == "low"


@pytest.mark.parametrize("effort", [None, "low", "medium", "high", "xhigh", "max"])
def test_astra_keeps_auto_and_valid_efforts(effort: str | None) -> None:
    assert normalize_model_reasoning("gpt-6-astra", effort) == effort


def test_sol_supports_explicit_none_and_migrates_minimal() -> None:
    assert normalize_model_reasoning("gpt-6-sol", "none") == "none"
    assert normalize_model_reasoning("gpt-6-sol", "minimal") == "low"
    assert normalize_model_reasoning("custom-model", "minimal") == "minimal"


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna"])
def test_agent_requires_responses_for_reasoning_with_tools(model: str) -> None:
    assert agent_model_api_supported(model, "openai-responses")
    assert not agent_model_api_supported(model, "openai-completions")
    assert agent_model_api_supported("custom-model", "openai-completions")


def test_responses_body_migrates_without_mutating_or_changing_model() -> None:
    body = {
        "model": "gateway/gpt-6-astra", "reasoning": {"effort": "none", "summary": "auto"},
        "temperature": 0.7, "top_p": 1, "top_logprobs": 5,
        "include": ["reasoning.encrypted_content", "message.output_text.logprobs"],
    }
    result = normalize_responses_model_body(body)
    assert result["model"] == body["model"]
    assert result["reasoning"] == {"effort": "low", "summary": "auto"}
    assert result["include"] == ["reasoning.encrypted_content"]
    assert not {"temperature", "top_p", "top_logprobs"} & result.keys()
    assert body["reasoning"]["effort"] == "none"
    assert "temperature" in body


def test_auto_reasoning_omits_sampling_without_forcing_an_effort() -> None:
    result = normalize_responses_model_body({"model": "gpt-6-luna", "temperature": 0.5})
    assert result == {"model": "gpt-6-luna"}
    body = {"model": "gpt-6-sol", "reasoning": {"effort": "none"}, "temperature": 0.5}
    assert normalize_responses_model_body(body) == body
    unknown = {"model": "custom", "temperature": 0.5}
    assert normalize_responses_model_body(unknown) is unknown
