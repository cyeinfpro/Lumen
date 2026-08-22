from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import agent_context
from app.provider_runtime.contracts import ResolvedProvider


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
        async def select(self, **_kwargs: object) -> list[ResolvedProvider]:
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
