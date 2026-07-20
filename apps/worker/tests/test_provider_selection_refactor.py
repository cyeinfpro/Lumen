from __future__ import annotations

from typing import Any

import pytest

from app import account_limiter, upstream
from app.provider_pool import (
    ProviderConfig,
    ProviderHealth,
    ProviderPool,
    ResolvedProvider,
)
from app.upstream_parts import responses


def _pool_with(*providers: ProviderConfig) -> ProviderPool:
    pool = ProviderPool()
    pool._providers = list(providers)
    pool._health = {provider.name: ProviderHealth() for provider in providers}
    pool._config_loaded_at = float("inf")
    return pool


def _provider(
    name: str,
    *,
    transport: str = "url",
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name.removeprefix('user:')}.example",
        api_key=f"sk-{name}",
        image_edit_input_transport=transport,
    )


@pytest.mark.asyncio
async def test_image_avoid_full_overlap_retries_byok_with_contract_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider("user:42")
    pool = _pool_with(provider)

    class FakeRedis:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def smembers(self, key: str) -> set[bytes]:
            self.keys.append(key)
            return {provider.name.encode()}

    redis = FakeRedis()
    pool.attach_redis(redis)
    quota_checks: list[str] = []

    async def allow_quota(
        _redis: Any,
        name: str,
        _rate_limit: str | None,
        _daily_quota: int | None,
        **_kwargs: Any,
    ) -> tuple[bool, float]:
        quota_checks.append(name)
        return True, 0.0

    monkeypatch.setattr(account_limiter, "check_quota", allow_quota)

    selected = await pool.select(
        route="image",
        task_id="task-42",
        acquire_inflight=False,
    )

    assert redis.keys == ["generation:image_queue:avoid:task-42"]
    assert quota_checks == [provider.name]
    assert len(selected) == 1
    assert type(selected[0]) is ResolvedProvider
    assert selected[0].name == provider.name
    assert selected[0].api_key == provider.api_key


@pytest.mark.asyncio
async def test_pool_select_compat_downgrades_then_applies_live_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = [
        _provider("url-ok"),
        _provider("file-ok", transport="file"),
        _provider("file-blocked", transport="file"),
    ]
    seen_kwargs: list[set[str]] = []

    class LegacyPool:
        async def select(self, **kwargs: Any) -> list[ProviderConfig]:
            seen_kwargs.append(set(kwargs))
            for unsupported in (
                "acquire_inflight",
                "endpoint_kind",
                "requires_mask",
            ):
                if unsupported in kwargs:
                    raise TypeError(
                        f"select() got an unexpected keyword argument '{unsupported}'"
                    )
            return providers

    filter_calls: list[tuple[str, str]] = []

    def allows_endpoint(provider: ProviderConfig, endpoint_kind: str) -> bool:
        filter_calls.append((provider.name, endpoint_kind))
        return provider.name != "file-blocked"

    monkeypatch.setattr(
        upstream,
        "_provider_allows_image_endpoint",
        allows_endpoint,
    )

    selected = await upstream._pool_select_compat(
        LegacyPool(),
        route="image",
        endpoint_kind="responses",
        requires_mask=True,
    )

    assert [provider.name for provider in selected] == ["file-ok"]
    assert [
        keys & {"acquire_inflight", "endpoint_kind", "requires_mask"}
        for keys in seen_kwargs
    ] == [
        {"acquire_inflight", "endpoint_kind", "requires_mask"},
        {"endpoint_kind", "requires_mask"},
        {"requires_mask"},
        set(),
    ]
    assert filter_calls == [
        ("url-ok", "responses"),
        ("file-ok", "responses"),
        ("file-blocked", "responses"),
    ]


@pytest.mark.asyncio
async def test_pool_select_compat_does_not_swallow_internal_type_error() -> None:
    class BrokenPool:
        async def select(self, **_kwargs: Any) -> list[Any]:
            raise TypeError("provider transformation failed")

    with pytest.raises(TypeError, match="provider transformation failed"):
        await upstream._pool_select_compat(BrokenPool(), route="image")


def test_response_candidate_order_preserves_injected_short_circuit() -> None:
    seen: list[Any] = []

    def select_data_result(value: Any) -> str | None:
        seen.append(value)
        return "chosen" if value == "data-result" else None

    payload = {
        "result": "root-result",
        "b64_json": "root-b64",
        "item": {
            "result": "item-result",
            "b64_json": "item-b64",
        },
        "data": [
            {
                "b64_json": "data-b64",
                "result": "data-result",
            }
        ],
        "output": [{"result": "output-result"}],
        "response": {"data": [{"b64_json": "response-data"}]},
    }

    assert (
        responses._extract_image_b64_from_payload(
            payload,
            b64_value_if_str=select_data_result,
        )
        == "chosen"
    )
    assert seen == [
        "root-result",
        "root-b64",
        "item-result",
        "item-b64",
        "data-b64",
        "data-result",
    ]
