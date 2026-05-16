"""Shared helpers for redemption-code secret rotation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import SystemSetting


PREVIOUS_REDEMPTION_SECRET_KEY = "billing.redemption_code_previous_secret"
PREVIOUS_REDEMPTION_SECRET_TTL = timedelta(hours=24)


class PreviousRedemptionSecretLocked(RuntimeError):
    """Raised when another secret rotation is still inside the grace window."""


def previous_redemption_secret_payload(
    old_secret: str, *, now: datetime | None = None
) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    expires_at = current + PREVIOUS_REDEMPTION_SECRET_TTL
    return (
        json.dumps(
            {"secret": old_secret, "expires_at": expires_at.isoformat()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        expires_at.isoformat(),
    )


def parse_previous_redemption_secret(
    raw: str | None, *, now: datetime | None = None
) -> str | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    secret = str(data.get("secret") or "").strip()
    expires_raw = str(data.get("expires_at") or "").strip()
    if not secret or not expires_raw:
        return None
    try:
        expires_at = datetime.fromisoformat(expires_raw)
    except ValueError:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if expires_at <= current:
        return None
    return secret


async def _system_setting_raw(db: AsyncSession, key: str) -> str | None:
    return (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == key))
    ).scalar_one_or_none()


async def _upsert_system_setting(db: AsyncSession, key: str, value: str) -> None:
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if row is None:
        db.add(SystemSetting(key=key, value=value))
        await db.flush()
    else:
        row.value = value


async def previous_redemption_secret(
    db: AsyncSession, *, now: datetime | None = None
) -> str | None:
    return parse_previous_redemption_secret(
        await _system_setting_raw(db, PREVIOUS_REDEMPTION_SECRET_KEY),
        now=now,
    )


async def remember_previous_redemption_secret(
    db: AsyncSession, old_secret: str | None, *, now: datetime | None = None
) -> str | None:
    secret = (old_secret or "").strip()
    if not secret:
        return None
    current = await previous_redemption_secret(db, now=now)
    if current and current != secret:
        raise PreviousRedemptionSecretLocked(
            "another redemption secret rotation is still inside the 24h transition window"
        )
    payload, expires_at = previous_redemption_secret_payload(secret, now=now)
    await _upsert_system_setting(db, PREVIOUS_REDEMPTION_SECRET_KEY, payload)
    return expires_at
