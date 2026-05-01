from __future__ import annotations

import json

import pytest

from app.routes import admin_models


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
async def test_build_models_response_skips_endpoint_locked_providers(
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

    assert fetched == ["unlocked"]
    assert [(model.id, model.providers) for model in out.models] == [
        ("gpt-5.5", ["unlocked"]),
    ]


@pytest.mark.asyncio
async def test_admin_models_cache_avoids_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    admin_models.invalidate_admin_models_cache()

    async def fake_build(_db: object):
        nonlocal calls
        calls += 1
        return admin_models.AdminModelsOut(
            models=[],
            fetched_at=admin_models.datetime.now(admin_models.timezone.utc),
            errors=[],
        )

    monkeypatch.setattr(admin_models, "_build_models_response", fake_build)

    first = await admin_models.list_admin_models(object(), object())  # type: ignore[arg-type]
    second = await admin_models.list_admin_models(object(), object())  # type: ignore[arg-type]

    assert first is second
    assert calls == 1
    admin_models.invalidate_admin_models_cache()
