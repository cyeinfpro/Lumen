"""Pure inventory matching helpers for Volcano asset operations."""

from __future__ import annotations

from typing import Any

from lumen_core.video_providers import VideoProviderDefinition


def explicit_asset_total(raw: Any) -> int | None:
    if not isinstance(raw, dict):
        return None
    for key in ("TotalCount", "Total", "total_count", "total"):
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def asset_matches_operation(
    asset: dict[str, Any],
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> bool:
    return (
        bool(asset.get("id"))
        and asset.get("group_id") == str(operation.get("group_id") or "")
        and asset.get("name") == str(operation.get("name") or "")
        and asset.get("asset_type") == str(operation.get("asset_type") or "")
        and asset.get("project_name") == provider.project_name
    )
