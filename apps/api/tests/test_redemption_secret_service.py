from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.services import redemption_secret


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
