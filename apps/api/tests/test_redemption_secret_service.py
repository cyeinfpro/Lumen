from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.services import redemption_secret


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


@pytest.mark.asyncio
async def test_lock_redemption_secret_rotation_uses_transaction_and_row_locks() -> None:
    statements: list[Any] = []

    class Db:
        async def connection(self) -> Any:
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement: Any) -> _ScalarResult:
            statements.append(statement)
            value = "current-secret" if len(statements) == 2 else None
            return _ScalarResult(value)

    current = await redemption_secret.lock_redemption_secret_rotation(
        Db(),  # type: ignore[arg-type]
    )

    rendered = [
        str(statement.compile(dialect=postgresql.dialect())).upper()
        for statement in statements
    ]
    assert current == "current-secret"
    assert "PG_ADVISORY_XACT_LOCK" in rendered[0]
    assert "HASHTEXT" in rendered[0]
    assert "FROM SYSTEM_SETTINGS" in rendered[1]
    assert "FOR UPDATE" in rendered[1]
    assert (
        statements[1].compile(dialect=postgresql.dialect()).params["key_1"]
        == redemption_secret.CURRENT_REDEMPTION_SECRET_KEY
    )


@pytest.mark.asyncio
async def test_rotation_lock_missing_current_stays_serialized() -> None:
    statements: list[Any] = []

    class Db:
        async def connection(self) -> Any:
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement: Any) -> _ScalarResult:
            statements.append(statement)
            return _ScalarResult(None)

    current = await redemption_secret.lock_redemption_secret_rotation(
        Db(),  # type: ignore[arg-type]
    )

    assert current is None
    assert len(statements) == 2
    advisory_sql = str(statements[0].compile(dialect=postgresql.dialect())).lower()
    row_lock_sql = str(statements[1].compile(dialect=postgresql.dialect())).lower()
    assert "pg_advisory_xact_lock" in advisory_sql
    assert "for update" in row_lock_sql


@pytest.mark.asyncio
async def test_lock_redemption_secret_rotation_fails_closed_without_postgres() -> None:
    class Db:
        async def connection(self) -> Any:
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def execute(self, _statement: Any) -> None:
            raise AssertionError(
                "unsupported dialect must not attempt an unlocked read"
            )

    with pytest.raises(redemption_secret.RedemptionSecretRotationLockUnavailable):
        await redemption_secret.lock_redemption_secret_rotation(
            Db(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_remember_previous_redemption_secret_rejects_second_active_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 20, tzinfo=timezone.utc)
    payload, _expires_at = redemption_secret.previous_redemption_secret_payload(
        "first-old-secret",
        now=now,
    )

    async def fake_raw(_db: Any, key: str) -> str:
        assert key == redemption_secret.PREVIOUS_REDEMPTION_SECRET_KEY
        return payload

    async def fail_upsert(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("locked rotation must not overwrite previous secret")

    monkeypatch.setattr(redemption_secret, "_system_setting_raw", fake_raw)
    monkeypatch.setattr(redemption_secret, "_upsert_system_setting", fail_upsert)

    with pytest.raises(redemption_secret.PreviousRedemptionSecretLocked):
        await redemption_secret.remember_previous_redemption_secret(
            object(),  # type: ignore[arg-type]
            "second-old-secret",
            now=now,
        )


@pytest.mark.asyncio
async def test_remember_previous_redemption_secret_allows_same_active_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 5, 20, tzinfo=timezone.utc)
    payload, _expires_at = redemption_secret.previous_redemption_secret_payload(
        "same-old-secret",
        now=now,
    )
    writes: list[tuple[str, str]] = []

    async def fake_raw(_db: Any, _key: str) -> str:
        return payload

    async def fake_upsert(_db: Any, key: str, value: str) -> None:
        writes.append((key, value))

    monkeypatch.setattr(redemption_secret, "_system_setting_raw", fake_raw)
    monkeypatch.setattr(redemption_secret, "_upsert_system_setting", fake_upsert)

    expires_at = await redemption_secret.remember_previous_redemption_secret(
        object(),  # type: ignore[arg-type]
        "same-old-secret",
        now=now,
    )

    assert expires_at is not None
    assert writes and writes[0][0] == redemption_secret.PREVIOUS_REDEMPTION_SECRET_KEY
