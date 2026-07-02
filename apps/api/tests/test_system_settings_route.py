from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request

from app.routes import byok as byok_routes
from app.routes import system_settings
from lumen_core.schemas import ByokSettingsPatchIn, SystemSettingsUpdateIn


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/settings",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_byok_fallback_setting_is_forced_off_for_compat_clients() -> None:
    pairs = byok_routes._setting_pairs(  # noqa: SLF001
        ByokSettingsPatchIn(fallback_to_admin_provider=True)
    )

    assert pairs == [("byok.fallback_to_admin_provider", "0")]


@pytest.mark.asyncio
async def test_put_settings_rejects_empty_string_for_typed_setting() -> None:
    with pytest.raises(Exception) as excinfo:
        await system_settings.put_settings_endpoint(
            SystemSettingsUpdateIn(
                items=[{"key": "context.summary_target_tokens", "value": ""}]
            ),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.com"),
            object(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["details"]["errors"][0]["key"] == (
        "context.summary_target_tokens"
    )


@pytest.mark.asyncio
async def test_put_settings_rejects_enabled_provider_with_disabled_proxy() -> None:
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "ssh-cn",
                    "type": "ssh",
                    "host": "203.0.113.10",
                    "port": 22,
                    "enabled": False,
                }
            ],
            "providers": [
                {
                    "name": "primary",
                    "base_url": "https://upstream.example",
                    "api_key": "sk-test",
                    "enabled": True,
                    "proxy": "ssh-cn",
                }
            ],
        }
    )

    with pytest.raises(Exception) as excinfo:
        await system_settings.put_settings_endpoint(
            SystemSettingsUpdateIn(items=[{"key": "providers", "value": raw}]),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.com"),
            object(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    errors = excinfo.value.detail["error"]["details"]["errors"]
    assert errors[0]["key"] == "providers"
    assert "disabled proxy" in errors[0]["message"]


@pytest.mark.asyncio
async def test_threshold_pricing_alignment_rejects_invalid_json() -> None:
    with pytest.raises(Exception) as excinfo:
        await system_settings._validate_threshold_pricing_alignment(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            "{not-json",
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["code"] == "INVALID_THRESHOLDS_JSON"


@pytest.mark.asyncio
async def test_put_settings_secret_rotation_keeps_previous_secret_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remembered: list[str | None] = []
    updated: list[list[tuple[str, str]]] = []
    audits: list[dict[str, Any]] = []

    class Db:
        committed = False

        async def commit(self) -> None:
            self.committed = True

    async def fake_get_setting(_db: Any, _spec: Any) -> str:
        return "old-secret-value-123456"

    async def fake_update_settings(_db: Any, pairs: list[tuple[str, str]]) -> None:
        updated.append(pairs)

    async def fake_remember(_db: Any, old_secret: str | None) -> str:
        remembered.append(old_secret)
        return "2026-05-17T00:00:00+00:00"

    async def fake_write_audit(_db: Any, **kwargs: Any) -> bool:
        audits.append(kwargs)
        return True

    async def fake_settings_view(_db: Any) -> list[Any]:
        return []

    monkeypatch.setattr(system_settings, "get_setting", fake_get_setting)
    monkeypatch.setattr(system_settings, "update_settings", fake_update_settings)
    monkeypatch.setattr(
        system_settings, "remember_previous_redemption_secret", fake_remember
    )
    monkeypatch.setattr(system_settings, "write_audit", fake_write_audit)
    monkeypatch.setattr(system_settings, "request_ip_hash", lambda _request: "ip-hash")
    monkeypatch.setattr(system_settings, "get_settings_view", fake_settings_view)

    db = Db()
    out = await system_settings.put_settings_endpoint(
        SystemSettingsUpdateIn(
            items=[
                {
                    "key": "billing.redemption_code_secret",
                    "value": "new-secret-value-123456",
                }
            ]
        ),
        _request(),
        SimpleNamespace(id="admin-1", email="admin@example.com"),
        db,  # type: ignore[arg-type]
    )

    assert out.items == []
    assert db.committed is True
    assert updated == [[("billing.redemption_code_secret", "new-secret-value-123456")]]
    assert remembered == ["old-secret-value-123456"]
    assert audits[-1]["details"]["revoked_unredeemed_count"] == 0
    assert audits[-1]["details"]["previous_secret_valid_until"] is not None
