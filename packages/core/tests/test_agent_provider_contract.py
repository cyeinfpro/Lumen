from __future__ import annotations

import pytest

from lumen_core.agent_provider_contract import (
    agent_endpoint_contract,
    build_agent_probe_request,
    parse_agent_probe_sse,
)
from lumen_core.providers_parts.config import parse_provider_json


@pytest.mark.parametrize(
    ("api", "base", "agent_base", "sdk_base", "request_url", "models_url"),
    [
        (
            "openai-responses",
            "https://api.example.com",
            None,
            "https://api.example.com/v1",
            "https://api.example.com/v1/responses",
            "https://api.example.com/v1/models",
        ),
        (
            "openai-completions",
            "https://api.example.com/v1/",
            None,
            "https://api.example.com/v1",
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/models",
        ),
        (
            "openai-responses",
            "https://images.example.com/v1",
            "https://gateway.example.com/team/openai/v1/",
            "https://gateway.example.com/team/openai/v1",
            "https://gateway.example.com/team/openai/v1/responses",
            "https://gateway.example.com/team/openai/v1/models",
        ),
        (
            "anthropic-messages",
            "https://api.anthropic.com",
            None,
            "https://api.anthropic.com",
            "https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com/v1/models",
        ),
        (
            "anthropic-messages",
            "https://images.example.com/v1",
            "https://gateway.example.com/team/anthropic",
            "https://gateway.example.com/team/anthropic",
            "https://gateway.example.com/team/anthropic/v1/messages",
            "https://gateway.example.com/team/anthropic/v1/models",
        ),
    ],
)
def test_agent_endpoint_contract_matches_sdk_append_semantics(
    api: str,
    base: str,
    agent_base: str | None,
    sdk_base: str,
    request_url: str,
    models_url: str,
) -> None:
    contract = agent_endpoint_contract(base, api, agent_base_url=agent_base)
    assert contract.sdk_base_url == sdk_base
    assert contract.request_url == request_url
    assert contract.models_url == models_url


@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@example.com/v1",
        "https://example.com/v1?tenant=one",
        "https://example.com/v1#fragment",
        "https://example.com/v1/responses",
        "https://example.com/v1/chat/completions",
        "https://example.com/v1/messages",
        "https://example.com/\ncontrol",
    ],
)
def test_agent_endpoint_contract_rejects_ambiguous_or_complete_urls(value: str) -> None:
    with pytest.raises(ValueError):
        agent_endpoint_contract(value, "openai-responses")


def test_anthropic_legacy_v1_base_requires_explicit_migration() -> None:
    with pytest.raises(ValueError, match="must not end in /v1"):
        agent_endpoint_contract(
            "https://api.anthropic.com/v1",
            "anthropic-messages",
        )


@pytest.mark.parametrize(
    ("api", "raw", "stop_reason"),
    [
        (
            "openai-responses",
            'data: {"type":"response.output_text.delta","delta":"9801"}\n\n'
            'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n\n',
            "completed",
        ),
        (
            "openai-completions",
            'data: {"choices":[{"delta":{"content":"9801"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{}}\n\n'
            "data: [DONE]\n\n",
            "stop",
        ),
        (
            "anthropic-messages",
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"9801"}}\n\n'
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n'
            'data: {"type":"message_stop"}\n\n',
            "end_turn",
        ),
    ],
)
def test_agent_probe_contract_requires_adapter_terminal_sse(
    api: str,
    raw: str,
    stop_reason: str,
) -> None:
    parsed = parse_agent_probe_sse(api, raw)
    assert parsed.text == "9801"
    assert parsed.terminal is True
    assert parsed.usage_present is True
    assert parsed.stop_reason == stop_reason
    request = build_agent_probe_request(api, "configured-model")
    assert request["model"] == "configured-model"
    assert request["stream"] is True
    if api == "openai-completions":
        assert "max_tokens" not in request
        assert "max_completion_tokens" not in request
        assert request["messages"] == [
            {
                "role": "user",
                "content": (
                    "Return only the final integer. No words or punctuation."
                    "\n\nCalculate 99 * 99."
                ),
            }
        ]


def test_provider_config_keeps_generic_base_and_derives_agent_base_separately() -> None:
    providers, errors = parse_provider_json(
        '[{"name":"shared","base_url":"https://images.example.com/custom",'
        '"agent_base_url":"https://gateway.example.com/openai/v1",'
        '"agent_api":"openai-completions","api_key":"secret"}]'
    )
    assert errors == []
    assert providers[0].base_url == "https://images.example.com/custom"
    assert providers[0].agent_base_url == "https://gateway.example.com/openai/v1"


@pytest.mark.parametrize("model", ["o3", "gpt-4.1-mini"])
def test_chat_completion_probe_is_portable_across_reasoning_and_legacy_models(
    model: str,
) -> None:
    request = build_agent_probe_request("openai-completions", model)
    assert request == {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Return only the final integer. No words or punctuation."
                    "\n\nCalculate 99 * 99."
                ),
            }
        ],
        "stream": True,
    }


@pytest.mark.parametrize(
    ("api", "raw"),
    [
        (
            "openai-responses",
            'data: {"type":"response.output_text.delta","delta":"9801"}\n\n'
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
        ),
        (
            "openai-completions",
            'data: {"choices":[{"delta":{"content":"9801"},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n",
        ),
    ],
)
def test_agent_probe_terminal_without_usage_is_not_admissible(
    api: str,
    raw: str,
) -> None:
    parsed = parse_agent_probe_sse(api, raw)
    assert parsed.text == "9801"
    assert parsed.terminal is True
    assert parsed.usage_present is False


def test_agent_probe_contract_rejects_truncated_or_nonterminal_stream() -> None:
    parsed = parse_agent_probe_sse(
        "openai-responses",
        'data: {"type":"response.output_text.delta","delta":"9801"}\n\n',
    )
    assert parsed.text == "9801"
    assert parsed.terminal is False
