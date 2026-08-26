from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.agent import common


@pytest.mark.asyncio
async def test_wallet_agent_preflight_filters_providers_by_discovered_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(
        {
            "providers": [
                {
                    "name": "old-model",
                    "base_url": "https://old.example/v1",
                    "api_key": "sk-old",
                    "purposes": ["chat"],
                    "responses_supported": True,
                    "agent_models": ["gpt-4.1"],
                },
                {
                    "name": "gpt-56",
                    "base_url": "https://new.example/v1",
                    "api_key": "sk-new",
                    "purposes": ["chat"],
                    "responses_supported": True,
                    "agent_models": ["gpt-5.6-sol"],
                    "agent_context_window": 256000,
                },
                {
                    "name": "legacy-wildcard",
                    "base_url": "https://legacy.example/v1",
                    "api_key": "sk-legacy",
                    "purposes": ["chat"],
                    "responses_supported": True,
                },
            ]
        }
    )

    monkeypatch.setattr(
        common,
        "get_spec",
        lambda key: SimpleNamespace(key=key),
    )

    async def fake_get_setting(_db: object, spec: object) -> str:
        return raw if spec.key == "providers" else "gpt-5.6-sol"

    monkeypatch.setattr(common, "get_setting", fake_get_setting)

    result = await common.wallet_chat_provider_preflight(
        object(),  # type: ignore[arg-type]
        require_vision=False,
    )

    assert result.model == "gpt-5.6-sol"
    assert result.eligible_provider_names == ("gpt-56", "legacy-wildcard")
    assert result.context_window == 128000

    large_context = await common.wallet_chat_provider_preflight(
        object(),  # type: ignore[arg-type]
        require_vision=False,
        minimum_context_window=200000,
    )
    assert large_context.eligible_provider_names == ("gpt-56",)
    assert large_context.context_window == 256000


@pytest.mark.asyncio
async def test_wallet_agent_preflight_uses_each_provider_output_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = json.dumps(
        {
            "providers": [
                {
                    "name": "small-context",
                    "base_url": "https://small.example/v1",
                    "api_key": "sk-small",
                    "purposes": ["chat"],
                    "responses_supported": True,
                    "agent_context_window": 32768,
                    "agent_max_output_tokens": 32768,
                },
                {
                    "name": "usable-context",
                    "base_url": "https://usable.example/v1",
                    "api_key": "sk-usable",
                    "purposes": ["chat"],
                    "responses_supported": True,
                    "agent_context_window": 65536,
                    "agent_max_output_tokens": 8192,
                },
            ]
        }
    )
    monkeypatch.setattr(common, "get_spec", lambda key: SimpleNamespace(key=key))

    async def fake_get_setting(_db: object, spec: object) -> str:
        return raw if spec.key == "providers" else "gpt-agent"

    monkeypatch.setattr(common, "get_setting", fake_get_setting)

    result = await common.wallet_chat_provider_preflight(
        object(),  # type: ignore[arg-type]
        require_vision=False,
        input_context_tokens=20_000,
    )

    assert result.eligible_provider_names == ("usable-context",)
    assert result.context_window == 65_536
    assert result.max_output_tokens == 8_192
