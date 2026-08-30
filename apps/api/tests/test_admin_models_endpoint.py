from __future__ import annotations

import json

import pytest

from app.routes import admin_models
from app.services.admin_model_cache import AdminModelCache
from lumen_core.schema_models.providers import AdminProviderModelsDiscoverIn


PROVIDERS_RAW = json.dumps(
    {
        "providers": [
            {
                "name": "main",
                "base_url": "https://main.example/v1",
                "api_key": "sk-main",
                "enabled": True,
            },
            {
                "name": "backup",
                "base_url": "https://backup.example/v1",
                "api_key": "sk-backup",
                "enabled": True,
            },
            {
                "name": "off",
                "base_url": "https://off.example/v1",
                "api_key": "sk-off",
                "enabled": False,
            },
        ]
    }
)


PROVIDERS_WITH_LOCKED_RAW = json.dumps(
    {
        "providers": [
            {
                "name": "image2-only",
                "base_url": "https://image2.example/v1",
                "api_key": "sk-image2",
                "enabled": True,
                "image_jobs_endpoint": "generations",
                "image_jobs_endpoint_lock": True,
            },
            {
                "name": "responses-only",
                "base_url": "https://responses.example/v1",
                "api_key": "sk-responses",
                "enabled": True,
                "image_jobs_endpoint": "responses",
                "image_jobs_endpoint_lock": True,
            },
            {
                "name": "unlocked",
                "base_url": "https://unlocked.example/v1",
                "api_key": "sk-unlocked",
                "enabled": True,
            },
        ]
    }
)


@pytest.mark.asyncio
async def test_build_models_response_dedupes_and_keeps_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read_providers(_db: object) -> tuple[str, str]:
        return PROVIDERS_RAW, "db"

    async def fake_fetch(provider: object) -> tuple[str, list[str], str | None]:
        name = provider.name
        if name == "main":
            return name, ["gpt-5.5", "gpt-5.4-mini"], None
        if name == "backup":
            return name, ["gpt-5.5", "gpt-4.1"], None
        raise AssertionError("disabled provider should not be fetched")

    monkeypatch.setattr(admin_models, "_read_providers", fake_read_providers)
    monkeypatch.setattr(admin_models, "_fetch_provider_models", fake_fetch)

    out = await admin_models._build_models_response(object())  # type: ignore[arg-type]

    assert [(model.id, model.providers) for model in out.models] == [
        ("gpt-4.1", ["backup"]),
        ("gpt-5.4-mini", ["main"]),
        ("gpt-5.5", ["backup", "main"]),
    ]
    assert out.errors == []


@pytest.mark.asyncio
async def test_build_models_response_ignores_image_endpoint_locks_for_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read_providers(_db: object) -> tuple[str, str]:
        return PROVIDERS_WITH_LOCKED_RAW, "db"

    fetched: list[str] = []

    async def fake_fetch(provider: object) -> tuple[str, list[str], str | None]:
        fetched.append(provider.name)
        return provider.name, ["gpt-5.5"], None

    monkeypatch.setattr(admin_models, "_read_providers", fake_read_providers)
    monkeypatch.setattr(admin_models, "_fetch_provider_models", fake_fetch)

    out = await admin_models._build_models_response(object())  # type: ignore[arg-type]

    assert fetched == ["image2-only", "responses-only", "unlocked"]
    assert [(model.id, model.providers) for model in out.models] == [
        ("gpt-5.5", ["image2-only", "responses-only", "unlocked"]),
    ]


@pytest.mark.asyncio
async def test_admin_models_cache_avoids_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    cache = AdminModelCache()

    async def fake_build(_db: object):
        nonlocal calls
        calls += 1
        return admin_models.AdminModelsOut(
            models=[],
            fetched_at=admin_models.datetime.now(admin_models.timezone.utc),
            errors=[],
        )

    monkeypatch.setattr(admin_models, "_build_models_response", fake_build)

    first = await cache.get(object(), fake_build)  # type: ignore[arg-type]
    second = await cache.get(object(), fake_build)  # type: ignore[arg-type]

    assert first is second
    assert calls == 1


def test_gpt_56_profile_uses_conservative_known_family_defaults() -> None:
    profile = admin_models._model_profile(  # noqa: SLF001
        "gpt-5.6-sol",
        {"id": "gpt-5.6-sol"},
        agent_api="openai-responses",
    )

    assert profile.source == "known_family"
    assert profile.responses_supported is True
    assert profile.vision_supported is True
    assert profile.reasoning_supported is True
    assert profile.context_window == 272_000
    assert profile.max_output_tokens == 16_384

    namespaced = admin_models._model_profile(  # noqa: SLF001
        "openai/gpt-5.6-mini",
        {"id": "openai/gpt-5.6-mini"},
        agent_api="openai-responses",
    )
    assert namespaced.source == "known_family"
    assert namespaced.vision_supported is True
    assert namespaced.reasoning_supported is True


def test_model_catalog_auth_headers_follow_provider_api() -> None:
    assert admin_models._models_headers("openai-responses", "secret") == {  # noqa: SLF001
        "authorization": "Bearer secret"
    }
    assert admin_models._models_headers("anthropic-messages", "secret") == {  # noqa: SLF001
        "x-api-key": "secret",
        "anthropic-version": "2023-06-01",
    }


def test_provider_model_metadata_overrides_family_defaults() -> None:
    profile = admin_models._model_profile(  # noqa: SLF001
        "gpt-5.6-custom",
        {
            "id": "gpt-5.6-custom",
            "context_length": 400_000,
            "max_output_tokens": 32_000,
            "architecture": {"input_modalities": ["text"]},
            "supported_parameters": ["temperature"],
        },
        agent_api="openai-responses",
    )

    assert profile.source == "provider"
    assert profile.context_window == 400_000
    assert profile.max_output_tokens == 32_000
    assert profile.vision_supported is False
    assert profile.reasoning_supported is False


@pytest.mark.asyncio
async def test_discover_models_reuses_saved_key_only_for_unchanged_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_read_providers(_db: object) -> tuple[str, str]:
        return PROVIDERS_RAW, "db"

    captured: dict[str, object] = {}

    async def fake_fetch(**kwargs: object) -> tuple[object, None]:
        captured.update(kwargs)
        return {
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "context_length": 256_000,
                    "input_modalities": ["text", "image"],
                }
            ]
        }, None

    monkeypatch.setattr(admin_models, "_read_providers", fake_read_providers)
    monkeypatch.setattr(admin_models, "_fetch_models_payload", fake_fetch)

    out = await admin_models.discover_provider_models(
        AdminProviderModelsDiscoverIn(
            provider_name="main",
            base_url="https://main.example/v1",
            api_key="",
            agent_api="openai-responses",
        ),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert out.error is None
    assert [model.id for model in out.models] == ["gpt-5.6-sol"]
    assert out.models[0].profile.context_window == 256_000
    assert captured["api_key"] == "sk-main"

    changed = await admin_models.discover_provider_models(
        AdminProviderModelsDiscoverIn(
            provider_name="main",
            base_url="https://changed.example/v1",
            api_key="",
            agent_api="openai-responses",
        ),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert changed.models == []
    assert changed.error == "API key is required when the Agent connection changes"

    changed_agent_base = await admin_models.discover_provider_models(
        AdminProviderModelsDiscoverIn(
            provider_name="main",
            base_url="https://main.example/v1",
            agent_base_url="https://attacker.example/v1",
            api_key="",
            agent_api="openai-responses",
        ),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )
    assert changed_agent_base.models == []
    assert changed_agent_base.error == (
        "API key is required when the Agent connection changes"
    )
    assert captured["api_key"] == "sk-main"
