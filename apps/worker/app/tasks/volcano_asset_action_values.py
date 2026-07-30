"""Pure value parsing helpers for Volcano asset management actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from lumen_core.volcano_assets import VolcanoAssetServiceError


def is_not_found(exc: VolcanoAssetServiceError) -> bool:
    return exc.code == "volcano_asset_not_found" or exc.status_code in {404, 410}


def parse_operation_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None:
        return parse_operation_time(numeric)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
