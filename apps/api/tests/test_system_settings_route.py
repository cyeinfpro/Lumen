from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from app.routes import byok as byok_routes
from app.routes import billing
from app.routes import system_settings
from app.routes.billing_parts import composition as billing_composition
from app.routes.billing_parts import overview as billing_overview_routes
from app.routes.billing_parts import services as billing_services
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
    events: list[str] = []
    remembered: list[str | None] = []
    updated: list[list[tuple[str, str]]] = []
    audits: list[dict[str, Any]] = []

    class Db:
        committed = False

        async def commit(self) -> None:
            events.append("commit")
            self.committed = True

    async def fake_lock(_db: Any) -> None:
        events.append("lock")
        return None

    async def fake_get_setting(_db: Any, _spec: Any) -> str:
        events.append("get")
        return "old-secret-value-123456"

    async def fake_update_settings(_db: Any, pairs: list[tuple[str, str]]) -> None:
        events.append("update")
        updated.append(pairs)

    async def fake_remember(_db: Any, old_secret: str | None) -> str:
        events.append("remember")
        remembered.append(old_secret)
        return "2026-05-17T00:00:00+00:00"

    async def fake_write_audit(_db: Any, **kwargs: Any) -> bool:
        events.append(f"audit:{kwargs['event_type']}")
        audits.append(kwargs)
        return True

    async def fake_settings_view(_db: Any) -> list[Any]:
        events.append("view")
        return []

    monkeypatch.setattr(system_settings, "lock_redemption_secret_rotation", fake_lock)
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
    assert events == [
        "lock",
        "get",
        "update",
        "audit:admin.settings.update",
        "remember",
        "audit:billing.secret.rotate",
        "commit",
        "view",
    ]


@pytest.mark.asyncio
async def test_rotation_entrypoints_share_transaction_lock_and_reject_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SharedState:
        def __init__(self) -> None:
            self.current = "initial-old-secret"
            self.previous: str | None = None
            self.transaction_lock = asyncio.Lock()
            self.first_lock_acquired = asyncio.Event()
            self.release_first = asyncio.Event()
            self.second_lock_attempted = asyncio.Event()
            self.lock_reads: list[str] = []
            self.committed_audits: list[str] = []

    shared = SharedState()

    class Db:
        def __init__(self, name: str) -> None:
            self.name = name
            self.holds_lock = False
            self.pending_current: str | None = None
            self.pending_previous: str | None = None
            self.pending_audits: list[str] = []
            self.rolled_back = False

        def release_lock(self) -> None:
            if self.holds_lock:
                self.holds_lock = False
                shared.transaction_lock.release()

        async def commit(self) -> None:
            assert self.holds_lock
            if self.pending_current is not None:
                shared.current = self.pending_current
            if self.pending_previous is not None:
                shared.previous = self.pending_previous
            shared.committed_audits.extend(self.pending_audits)
            self.release_lock()

        async def rollback(self) -> None:
            self.pending_current = None
            self.pending_previous = None
            self.pending_audits.clear()
            self.rolled_back = True
            self.release_lock()

    async def fake_lock(db: Db) -> str:
        if db.name == "settings":
            shared.second_lock_attempted.set()
        await shared.transaction_lock.acquire()
        db.holds_lock = True
        if db.name == "billing":
            shared.first_lock_acquired.set()
            await shared.release_first.wait()
        shared.lock_reads.append(shared.current)
        return shared.current

    async def fake_update_settings(db: Db, pairs: list[tuple[str, str]]) -> None:
        assert db.holds_lock
        db.pending_current = dict(pairs)["billing.redemption_code_secret"]

    async def fake_remember(db: Db, old_secret: str | None) -> str:
        assert db.holds_lock
        if shared.previous and shared.previous != old_secret:
            raise billing_overview_routes.PreviousRedemptionSecretLocked(
                "active previous secret"
            )
        db.pending_previous = old_secret
        return "2026-08-04T00:00:00+00:00"

    async def fake_write_audit(db: Db, **kwargs: Any) -> bool:
        assert db.holds_lock
        db.pending_audits.append(str(kwargs["event_type"]))
        return True

    async def fail_get_setting(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("locked persisted current secret must be reused")

    async def fake_overview(_admin: Any, _db: Any) -> str:
        return "overview"

    async def fail_settings_view(_db: Any) -> list[Any]:
        raise AssertionError("rejected settings rotation must not render a response")

    monkeypatch.setattr(
        billing_composition, "lock_redemption_secret_rotation", fake_lock
    )
    monkeypatch.setattr(system_settings, "lock_redemption_secret_rotation", fake_lock)
    monkeypatch.setattr(billing_composition, "get_setting", fail_get_setting)
    monkeypatch.setattr(system_settings, "get_setting", fail_get_setting)
    monkeypatch.setattr(billing_composition, "update_settings", fake_update_settings)
    monkeypatch.setattr(system_settings, "update_settings", fake_update_settings)
    monkeypatch.setattr(
        billing_composition, "remember_previous_redemption_secret", fake_remember
    )
    monkeypatch.setattr(
        system_settings, "remember_previous_redemption_secret", fake_remember
    )
    monkeypatch.setattr(billing_composition, "write_audit", fake_write_audit)
    monkeypatch.setattr(system_settings, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        billing_composition, "request_ip_hash", lambda _request: "ip-hash"
    )
    monkeypatch.setattr(system_settings, "request_ip_hash", lambda _request: "ip-hash")
    monkeypatch.setattr(
        billing_services,
        "_generate_redemption_secret",
        lambda: "billing-first-new-secret",
    )
    monkeypatch.setattr(
        billing_overview_routes, "admin_billing_overview", fake_overview
    )
    monkeypatch.setattr(system_settings, "get_settings_view", fail_settings_view)

    billing_db = Db("billing")
    settings_db = Db("settings")
    first = asyncio.create_task(
        billing.admin_rotate_redemption_secret(
            object(),  # type: ignore[arg-type]
            SimpleNamespace(id="admin-1", email="admin-1@example.test"),  # type: ignore[arg-type]
            billing_db,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(shared.first_lock_acquired.wait(), timeout=1)
    second = asyncio.create_task(
        system_settings.put_settings_endpoint(
            SystemSettingsUpdateIn(
                items=[
                    {
                        "key": "billing.redemption_code_secret",
                        "value": "settings-second-new-secret",
                    }
                ]
            ),
            _request(),
            SimpleNamespace(id="admin-2", email="admin-2@example.test"),
            settings_db,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(shared.second_lock_attempted.wait(), timeout=1)
    assert second.done() is False

    shared.release_first.set()
    assert await first == "overview"
    with pytest.raises(HTTPException) as excinfo:
        await second

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "previous_secret_locked"
    assert shared.lock_reads == [
        "initial-old-secret",
        "billing-first-new-secret",
    ]
    assert shared.current == "billing-first-new-secret"
    assert shared.previous == "initial-old-secret"
    assert shared.committed_audits == ["billing.secret.rotate"]
    assert settings_db.rolled_back is True
