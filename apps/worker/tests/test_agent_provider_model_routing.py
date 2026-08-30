from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import agent_context
from app.provider_pool import ProviderConfig, ProviderPool
from app.provider_runtime.contracts import ProviderHealth, ResolvedProvider


@pytest.mark.asyncio
async def test_worker_selects_only_provider_supporting_pinned_agent_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsupported = ResolvedProvider(
        name="old-model",
        base_url="https://old.example/v1",
        api_key="sk-old",
        purposes=("chat",),
        agent_models=("gpt-4.1",),
    )
    supported = ResolvedProvider(
        name="gpt-56",
        base_url="https://new.example/v1",
        api_key="sk-new",
        purposes=("chat",),
        agent_models=("gpt-5.6-sol",),
    )

    class Pool:
        async def select_agent(self, **kwargs: object) -> list[ResolvedProvider]:
            assert kwargs == {
                "model": "gpt-5.6-sol",
                "purpose": "chat",
            }
            return [unsupported, supported]

    async def fake_pool() -> Pool:
        return Pool()

    monkeypatch.setattr(agent_context, "get_pool", fake_pool)
    run = SimpleNamespace(
        account_mode_snapshot="wallet",
        model="gpt-5.6-sol",
        request_snapshot_jsonb={
            "eligible_provider_names": ["old-model", "gpt-56"],
            "references": [],
        },
    )

    _pool, provider = await agent_context.resolve_agent_chat_provider(
        object(),  # type: ignore[arg-type]
        run,  # type: ignore[arg-type]
    )

    assert provider.name == "gpt-56"


@pytest.mark.asyncio
async def test_provider_pool_agent_selection_filters_the_pinned_model() -> None:
    pool = ProviderPool()
    pool._providers = [
        ProviderConfig(
            name="old-model",
            base_url="https://old.example/v1",
            api_key="sk-old",
            agent_models=("gpt-4.1",),
        ),
        ProviderConfig(
            name="pinned-model",
            base_url="https://new.example/v1",
            api_key="sk-new",
            agent_models=("gpt-5.6-sol",),
        ),
    ]
    pool._config_loaded_at = time.monotonic() + 60.0

    selected = await pool.select_agent(model="gpt-5.6-sol")

    assert [provider.name for provider in selected] == ["pinned-model"]


def test_agent_protocol_failure_isolated_to_api_model_lane() -> None:
    pool = ProviderPool()
    provider = ResolvedProvider(
        name="shared-provider",
        base_url="https://provider.example/v1",
        agent_base_url="https://provider.example/v1",
        api_key="secret",
        agent_api="openai-completions",
    )
    pool._health[provider.name] = ProviderHealth()

    for _ in range(3):
        pool.report_agent_failure(provider, "configured-model")

    lane = pool._agent_health[
        (
            provider.name,
            provider.agent_api,
            "configured-model",
        )
    ]
    assert lane.consecutive_failures == 3
    assert lane.cooldown_until is not None
    assert pool._health[provider.name].consecutive_failures == 0
    assert pool._health[provider.name].image_consecutive_failures == 0
