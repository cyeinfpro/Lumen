from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from app import runtime_settings


def _db_resolutions(
    *resolutions: runtime_settings.SettingResolution,
) -> Iterator[runtime_settings.SettingResolution]:
    yield from resolutions


@pytest.mark.asyncio
async def test_recent_database_value_survives_temporary_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    reads = _db_resolutions(
        runtime_settings.SettingResolution("value", "16", "database"),
        runtime_settings.SettingResolution("unavailable"),
    )

    async def read_db(_spec_key: str) -> runtime_settings.SettingResolution:
        return next(reads)

    monkeypatch.setattr(
        runtime_settings,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(runtime_settings, "_read_db_state", read_db)
    monkeypatch.setenv("IMAGE_GENERATION_CONCURRENCY", "2")
    cache = runtime_settings.RuntimeSettingsCache()

    with runtime_settings._RUNTIME_CACHE_SLOT.use(cache):
        assert await runtime_settings.resolve("image.generation_concurrency") == "16"
        clock[0] += runtime_settings._TTL_S + 0.1
        assert await runtime_settings.resolve("image.generation_concurrency") == "16"


@pytest.mark.asyncio
async def test_expired_database_value_does_not_fall_back_to_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    reads = _db_resolutions(
        runtime_settings.SettingResolution("value", "16", "database"),
        runtime_settings.SettingResolution("unavailable"),
    )

    async def read_db(_spec_key: str) -> runtime_settings.SettingResolution:
        return next(reads)

    monkeypatch.setattr(
        runtime_settings,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(runtime_settings, "_read_db_state", read_db)
    monkeypatch.setenv("IMAGE_GENERATION_CONCURRENCY", "2")
    cache = runtime_settings.RuntimeSettingsCache()

    with runtime_settings._RUNTIME_CACHE_SLOT.use(cache):
        assert await runtime_settings.resolve("image.generation_concurrency") == "16"
        clock[0] += runtime_settings._LAST_KNOWN_GOOD_MAX_STALE_S + 0.1
        with pytest.raises(runtime_settings.SettingUnavailable):
            await runtime_settings.resolve("image.generation_concurrency")


@pytest.mark.asyncio
async def test_confirmed_database_missing_allows_environment_fallback_during_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    reads = _db_resolutions(
        runtime_settings.SettingResolution("missing"),
        runtime_settings.SettingResolution("unavailable"),
    )

    async def read_db(_spec_key: str) -> runtime_settings.SettingResolution:
        return next(reads)

    monkeypatch.setattr(
        runtime_settings,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0]),
    )
    monkeypatch.setattr(runtime_settings, "_read_db_state", read_db)
    monkeypatch.setenv("IMAGE_GENERATION_CONCURRENCY", "16")
    cache = runtime_settings.RuntimeSettingsCache()

    with runtime_settings._RUNTIME_CACHE_SLOT.use(cache):
        assert await runtime_settings.resolve("image.generation_concurrency") == "16"
        clock[0] += runtime_settings._TTL_S + 0.1
        assert await runtime_settings.resolve("image.generation_concurrency") == "16"
