from __future__ import annotations

import json

import pytest

from app.provider_pool import ProviderPool
from app.provider_pool import ProviderConfig, ResolvedProvider
from app.config import Settings


def test_worker_settings_ignores_shared_env_fields() -> None:
    settings = Settings(
        db_user="lumen",
        db_password="lumen",
        db_name="lumen",
        redis_password="secret",
        upstream_api_key="sk-legacy",
        cors_allow_origins="http://example.test",
    )

    assert settings.database_url
    assert not hasattr(settings, "db_user")


def test_provider_dataclass_repr_does_not_include_api_key() -> None:
    provider = ProviderConfig(
        name="primary",
        base_url="https://primary.example",
        api_key="sk-secret-value",
    )
    resolved = ResolvedProvider(
        name=provider.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
    )

    assert provider.api_key == "sk-secret-value"
    assert resolved.api_key == "sk-secret-value"
    assert "sk-secret-value" not in repr(provider)
    assert "sk-secret-value" not in repr(resolved)


@pytest.mark.asyncio
async def test_provider_pool_uses_configured_providers_without_legacy_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runtime_settings

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "primary",
                    "base_url": "https://primary.example",
                    "api_key": "sk-primary",
                    "priority": 10,
                    "weight": 2,
                    "enabled": True,
                }
            ]
        ),
    }

    async def fake_resolve(key: str) -> str | None:
        return values.get(key)

    monkeypatch.setattr(runtime_settings, "resolve", fake_resolve)

    providers = await ProviderPool()._load_config()

    assert [
        (p.name, p.base_url, p.api_key, p.priority, p.weight)
        for p in providers
    ] == [
        ("primary", "https://primary.example", "sk-primary", 10, 2),
    ]


@pytest.mark.asyncio
async def test_provider_pool_loads_provider_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runtime_settings

    values = {
        "providers": json.dumps(
            {
                "proxies": [
                    {
                        "name": "egress",
                        "type": "socks5",
                        "host": "127.0.0.1",
                        "port": 1080,
                    }
                ],
                "providers": [
                    {
                        "name": "primary",
                        "base_url": "https://primary.example",
                        "api_key": "sk-primary",
                        "proxy": "egress",
                    }
                ],
            }
        ),
    }

    async def fake_resolve(key: str) -> str | None:
        return values.get(key)

    monkeypatch.setattr(runtime_settings, "resolve", fake_resolve)

    providers = await ProviderPool()._load_config()

    assert providers[0].proxy_name == "egress"
    assert providers[0].proxy is not None
    assert providers[0].proxy.host == "127.0.0.1"


@pytest.mark.asyncio
async def test_provider_pool_reload_preserves_image_job_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool, runtime_settings

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "Flux",
                    "base_url": "https://flux.example",
                    "api_key": "sk-flux",
                    "enabled": True,
                    "image_jobs_enabled": True,
                    "image_jobs_endpoint": "responses",
                    "image_jobs_base_url": "https://jobs.example",
                    "image_concurrency": 20,
                }
            ]
        ),
    }

    async def fake_resolve(key: str) -> str | None:
        return values.get(key)

    async def fake_validate_provider_base_url(raw_base: str) -> str:
        return raw_base.rstrip("/")

    monkeypatch.setattr(runtime_settings, "resolve", fake_resolve)
    monkeypatch.setattr(
        provider_pool,
        "_validate_provider_base_url",
        fake_validate_provider_base_url,
    )

    pool = ProviderPool()
    await pool._maybe_reload()

    providers = await pool.select(route="image")
    assert [
        (
            provider.name,
            provider.image_concurrency,
            provider.image_jobs_endpoint,
            provider.image_jobs_base_url,
        )
        for provider in providers
    ] == [
        ("Flux", 20, "responses", "https://jobs.example"),
    ]


@pytest.mark.asyncio
async def test_provider_endpoint_lock_filters_text_and_image_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import provider_pool, runtime_settings

    values = {
        "providers": json.dumps(
            [
                {
                    "name": "image2-only",
                    "base_url": "https://image2.example",
                    "api_key": "sk-image2",
                    "enabled": True,
                    "image_jobs_endpoint": "generations",
                    "image_jobs_endpoint_lock": True,
                },
                {
                    "name": "responses-only",
                    "base_url": "https://responses.example",
                    "api_key": "sk-responses",
                    "enabled": True,
                    "image_jobs_endpoint": "responses",
                    "image_jobs_endpoint_lock": True,
                },
            ]
        ),
    }

    async def fake_resolve(key: str) -> str | None:
        return values.get(key)

    async def fake_validate_provider_base_url(raw_base: str) -> str:
        return raw_base.rstrip("/")

    monkeypatch.setattr(runtime_settings, "resolve", fake_resolve)
    monkeypatch.setattr(
        provider_pool,
        "_validate_provider_base_url",
        fake_validate_provider_base_url,
    )

    pool = ProviderPool()
    text = await pool.select(route="text")
    image2 = await pool.select(route="image", endpoint_kind="generations")
    responses = await pool.select(route="image", endpoint_kind="responses")

    assert [p.name for p in text] == ["responses-only"]
    assert [p.name for p in image2] == ["image2-only"]
    assert [p.name for p in responses] == ["responses-only"]


@pytest.mark.asyncio
async def test_provider_pool_uses_legacy_env_only_when_providers_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runtime_settings

    async def fake_resolve(key: str) -> str | None:
        assert key == "providers"
        return None

    monkeypatch.setattr(runtime_settings, "resolve", fake_resolve)
    monkeypatch.setenv("UPSTREAM_BASE_URL", "https://legacy.example")
    monkeypatch.setenv("UPSTREAM_API_KEY", "sk-legacy")

    providers = await ProviderPool()._load_config()

    assert [(p.name, p.base_url, p.api_key) for p in providers] == [
        ("default", "https://legacy.example", "sk-legacy")
    ]
