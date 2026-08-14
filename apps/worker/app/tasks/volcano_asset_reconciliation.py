"""Paginated inventory and ambiguous-submit reconciliation."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta
from typing import Any

from .volcano_asset_runtime import (
    VolcanoAssetRuntimeContext,
    VolcanoAssetRuntimeSlot,
    VolcanoAssetRuntimeView,
)

_AIGC_GROUP_TYPE = "AIGC"
_AMBIGUOUS_RECONCILE_ATTEMPTS = 3
_GROUP_RECONCILE_BEFORE_SECONDS = 2 * 60
_GROUP_RECONCILE_AFTER_SECONDS = 30 * 60
_ASSET_SCAN_PAGE_SIZE = 100
_ASSET_SCAN_MAX_ITEMS = 3000
_RUNTIME = VolcanoAssetRuntimeSlot(
    owner=__name__,
    dependencies=frozenset(
        {
            "_OperationFailure",
            "_asset_matches_operation",
            "_explicit_asset_total",
            "_parse_operation_time",
            "VolcanoAssetServiceError",
            "normalize_asset_list",
        }
    ),
)


def install_runtime(context: VolcanoAssetRuntimeContext) -> None:
    _RUNTIME.install(context)


def _runtime() -> VolcanoAssetRuntimeView:
    return _RUNTIME.get()


async def _scan_operation_assets(
    client: Any,
    provider: Any,
    operation: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    runtime = _runtime()
    group_id = str(operation.get("group_id") or "")
    name = str(operation.get("name") or "")
    if not group_id or not name:
        return [], True
    seen_ids: set[str] = set()
    matches: list[dict[str, Any]] = []
    page_number = 1
    scanned_items = 0
    known_total: int | None = None
    while scanned_items < _ASSET_SCAN_MAX_ITEMS:
        raw = await client.request(
            "ListAssets",
            {
                "ProjectName": provider.project_name,
                "Filter": {
                    "GroupType": _AIGC_GROUP_TYPE,
                    "GroupIds": [group_id],
                    "Name": name,
                },
                "PageNumber": page_number,
                "PageSize": _ASSET_SCAN_PAGE_SIZE,
            },
        )
        listed = runtime.normalize_asset_list(
            raw,
            project_name=provider.project_name,
            page_number=page_number,
            page_size=_ASSET_SCAN_PAGE_SIZE,
        )
        items = listed["items"]
        explicit_total = runtime._explicit_asset_total(raw)
        if explicit_total is not None:
            known_total = max(known_total or 0, explicit_total)
        if not items:
            return matches, True
        remaining = _ASSET_SCAN_MAX_ITEMS - scanned_items
        scanned_items += min(len(items), remaining)
        items = items[:remaining]
        page_ids = {str(asset.get("id") or "") for asset in items if asset.get("id")}
        new_ids = page_ids - seen_ids
        if not new_ids:
            return matches, False
        for asset in items:
            asset_id = str(asset.get("id") or "")
            if asset_id in new_ids and runtime._asset_matches_operation(
                asset,
                provider,
                operation,
            ):
                matches.append(asset)
        seen_ids.update(new_ids)
        if known_total is not None and len(seen_ids) >= known_total:
            return matches, True
        if scanned_items >= _ASSET_SCAN_MAX_ITEMS:
            return matches, False
        page_number += 1
    return matches, False


async def _find_existing_submitted_asset(
    client: Any,
    provider: Any,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _runtime()
    assets, complete = await _scan_operation_assets(client, provider, operation)
    if not complete:
        return None
    baseline_asset_ids = {
        str(asset_id)
        for asset_id in (
            operation.get("baseline_asset_ids")
            if isinstance(operation.get("baseline_asset_ids"), list)
            else []
        )
        if asset_id is not None and str(asset_id)
    }
    submit_started_at = runtime._parse_operation_time(
        operation.get("submit_started_at")
    )
    lower_bound = (
        submit_started_at - timedelta(seconds=_GROUP_RECONCILE_BEFORE_SECONDS)
        if submit_started_at is not None
        else None
    )
    upper_bound = (
        submit_started_at + timedelta(seconds=_GROUP_RECONCILE_AFTER_SECONDS)
        if submit_started_at is not None
        else None
    )
    matches: list[dict[str, Any]] = []
    for asset in assets:
        asset_id = str(asset.get("id") or "")
        created_at = runtime._parse_operation_time(asset.get("create_time"))
        if (
            asset_id
            and asset_id not in baseline_asset_ids
            and (
                created_at is None
                or lower_bound is None
                or upper_bound is None
                or lower_bound <= created_at <= upper_bound
            )
        ):
            matches.append(asset)
    return matches[0] if len(matches) == 1 else None


async def _snapshot_group_asset_ids(
    client: Any,
    provider: Any,
    operation: dict[str, Any],
) -> list[str]:
    assets, complete = await _scan_operation_assets(client, provider, operation)
    if not complete:
        raise _runtime()._OperationFailure(
            "volcano_asset_inventory_incomplete",
            "could not safely inventory existing Volcano assets",
            retryable=True,
            retry_after_seconds=10,
        )
    return sorted(str(asset["id"]) for asset in assets)


async def _reconcile_ambiguous_submit(
    client: Any,
    provider: Any,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    for attempt in range(_AMBIGUOUS_RECONCILE_ATTEMPTS):
        if attempt:
            delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
            await asyncio.sleep(delay + random.uniform(0, delay))
        try:
            asset = await _find_existing_submitted_asset(
                client,
                provider,
                operation,
            )
        except _runtime().VolcanoAssetServiceError:
            continue
        if asset is not None:
            return asset
    return None


__all__ = [
    "_find_existing_submitted_asset",
    "_reconcile_ambiguous_submit",
    "_scan_operation_assets",
    "_snapshot_group_asset_ids",
    "install_runtime",
]
