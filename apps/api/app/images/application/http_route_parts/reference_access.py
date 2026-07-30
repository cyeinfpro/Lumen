from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


VIDEO_REFERENCE_ACCESS_TOKEN_TTL = timedelta(hours=24)


def parse_video_reference_token_expiry(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def video_reference_token_is_valid(
    metadata: dict[str, Any],
    *,
    token: str,
    updated_at: datetime | None,
) -> bool:
    expected = metadata.get("video_reference_access_token")
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        return False
    expires_at = parse_video_reference_token_expiry(
        metadata.get("video_reference_access_token_expires_at")
    )
    now = datetime.now(timezone.utc)
    if expires_at is not None:
        return expires_at > now
    if updated_at is None:
        return False
    fallback_updated_at = (
        updated_at.replace(tzinfo=timezone.utc)
        if updated_at.tzinfo is None
        else updated_at.astimezone(timezone.utc)
    )
    return fallback_updated_at + VIDEO_REFERENCE_ACCESS_TOKEN_TTL > now
