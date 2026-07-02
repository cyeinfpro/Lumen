from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routes import byok
from lumen_core.byok import ByokCryptoError


class _Result:
    def __init__(self, row: Any):
        self._row = row

    def one_or_none(self) -> Any:
        return self._row


class _Db:
    def __init__(self, *rows: Any):
        self.rows = list(rows)
        self.statements: list[Any] = []
        self.commits = 0
        self.refreshes: list[tuple[Any, list[str] | None]] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.rows.pop(0) if self.rows else None)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any, attrs: list[str] | None = None) -> None:
        self.refreshes.append((obj, attrs))


class _AllowLimiter:
    async def check(self, _redis: Any, _key: str) -> None:
        return None


def _request() -> Request:
    return Request({"type": "http", "headers": [], "client": ("203.0.113.9", 4567)})


def _credential() -> SimpleNamespace:
    now = datetime(2026, 5, 20, tzinfo=timezone.utc)
    return SimpleNamespace(
        id="cred-1",
        supplier_id="supplier-1",
        user_id="user-1",
        status="active",
        key_ciphertext="ciphertext",
        key_hint="test",
        last_verified_at=None,
        last_failed_at=None,
        last_error_code=None,
        rate_limited_until=None,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )


def _supplier() -> SimpleNamespace:
    return SimpleNamespace(
        id="supplier-1",
        name="OpenAI",
        enabled=True,
        deleted_at=None,
    )


def _for_update(statement: Any) -> bool:
    return getattr(statement, "_for_update_arg", None) is not None


@pytest.mark.asyncio
async def test_probe_credential_releases_row_lock_before_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_credential = _credential()
    second_credential = first_credential
    supplier = _supplier()
    db = _Db((first_credential, supplier), (second_credential, supplier))
    user = SimpleNamespace(id="user-1", email="user@example.test")
    validation_seen: dict[str, Any] = {}

    async def fake_validate(
        _db: Any,
        _supplier: Any,
        api_key: str,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        validation_seen["api_key"] = api_key
        validation_seen["commits_before_validate"] = db.commits
        return SimpleNamespace(
            ok=True,
            error_code=None,
            http_status=200,
            latency_ms=12,
        )

    async def fake_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_read_byok_settings(_db: Any) -> SimpleNamespace:
        return SimpleNamespace(
            validation_model="gpt-test",
            validation_timeout_ms=250,
        )

    monkeypatch.setattr(byok, "get_redis", lambda: object())
    monkeypatch.setattr(byok, "decrypt_api_key", lambda *_args: "sk-test")
    monkeypatch.setattr(byok, "api_key_rate_limit_hash", lambda _api_key: "hash")
    monkeypatch.setattr(byok, "byok_master_secret", lambda: "x" * 32)
    monkeypatch.setattr(byok, "read_byok_settings", fake_read_byok_settings)
    monkeypatch.setattr(byok, "validate_api_key_with_supplier", fake_validate)
    monkeypatch.setattr(byok, "write_audit", fake_audit)
    for name in (
        "_PROBE_IP_LIMITER",
        "_PROBE_USER_LIMITER",
        "_PROBE_CREDENTIAL_LIMITER",
        "_PROBE_SUPPLIER_LIMITER",
        "_PROBE_KEY_LIMITER",
    ):
        monkeypatch.setattr(byok, name, _AllowLimiter())

    out = await byok.probe_my_api_credential(
        "cred-1",
        _request(),
        user,  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
    )

    assert out.status == "active"
    assert second_credential.last_verified_at is not None
    assert validation_seen == {
        "api_key": "sk-test",
        "commits_before_validate": 1,
    }
    assert db.commits == 2
    assert len(db.statements) == 2
    assert all(_for_update(statement) for statement in db.statements)


@pytest.mark.asyncio
async def test_probe_credential_rate_limit_runs_before_db_and_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _Db((_credential(), _supplier()))
    user = SimpleNamespace(id="user-1", email="user@example.test")

    class _BlockingLimiter:
        async def check(self, _redis: Any, _key: str) -> None:
            raise byok._http("rate_limited", "rate limited", 429)  # noqa: SLF001

    async def fail_validate(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        raise AssertionError("upstream validation must not run when rate limited")

    monkeypatch.setattr(byok, "get_redis", lambda: object())
    monkeypatch.setattr(byok, "_PROBE_IP_LIMITER", _BlockingLimiter())
    monkeypatch.setattr(byok, "validate_api_key_with_supplier", fail_validate)

    with pytest.raises(HTTPException) as exc_info:
        await byok.probe_my_api_credential(
            "cred-1",
            _request(),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 429
    assert db.statements == []


@pytest.mark.asyncio
async def test_probe_credential_masks_decrypt_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = _credential()
    supplier = _supplier()
    db = _Db((credential, supplier))
    user = SimpleNamespace(id="user-1", email="user@example.test")
    audits: list[dict[str, Any]] = []

    async def fake_audit(**kwargs: Any) -> None:
        audits.append(kwargs)

    def fail_decrypt(*_args: Any) -> str:
        raise ByokCryptoError("bad master secret")

    monkeypatch.setattr(byok, "get_redis", lambda: object())
    monkeypatch.setattr(byok, "decrypt_api_key", fail_decrypt)
    monkeypatch.setattr(byok, "byok_master_secret", lambda: "x" * 32)
    monkeypatch.setattr(byok, "write_audit_isolated", fake_audit)
    for name in (
        "_PROBE_IP_LIMITER",
        "_PROBE_USER_LIMITER",
        "_PROBE_CREDENTIAL_LIMITER",
        "_PROBE_SUPPLIER_LIMITER",
    ):
        monkeypatch.setattr(byok, name, _AllowLimiter())

    with pytest.raises(HTTPException) as exc_info:
        await byok.probe_my_api_credential(
            "cred-1",
            _request(),
            user,  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"]["code"] == "credential_unavailable"
    assert "master" not in exc_info.value.detail["error"]["message"].lower()
    assert audits
    assert audits[0]["event_type"] == "me.api_credential.probe.decrypt_failed"
    assert db.commits == 1
