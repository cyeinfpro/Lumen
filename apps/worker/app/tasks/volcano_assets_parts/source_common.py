"""Shared source-media helpers for Volcano asset imports."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from lumen_core.volcano_asset_media import VolcanoAssetMediaError


_REFERENCE_TOKEN_TTL = timedelta(hours=24)


def ensure_reference_token(
    metadata: dict[str, Any],
    *,
    token_key: str,
    expires_key: str,
) -> str:
    existing_token = str(metadata.get(token_key) or "")
    raw_expires_at = str(metadata.get(expires_key) or "")
    try:
        expires_at = datetime.fromisoformat(raw_expires_at)
    except ValueError:
        expires_at = None
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if existing_token and expires_at > datetime.now(timezone.utc):
            return existing_token
    token = secrets.token_urlsafe(32)
    metadata[token_key] = token
    metadata[expires_key] = (
        datetime.now(timezone.utc) + _REFERENCE_TOKEN_TTL
    ).isoformat()
    return token


def source_not_found(asset_type: str) -> VolcanoAssetMediaError:
    return VolcanoAssetMediaError(
        f"video_asset_{asset_type.lower()}_not_found",
        f"asset {asset_type.lower()} was not found",
        404,
    )


__all__ = ["ensure_reference_token", "source_not_found"]
