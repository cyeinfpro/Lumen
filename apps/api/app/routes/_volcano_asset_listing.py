"""List payload and member-visibility helpers for Volcano assets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from fastapi import HTTPException

from lumen_core.schemas import VideoAssetQuotaUsageOut
from lumen_core.video_providers import VideoProviderDefinition

from ._volcano_asset_ownership import OwnedResourceReceipts


_AIGC_GROUP_TYPE = "AIGC"
MEMBER_LIST_PAGE_SIZE = 100
MEMBER_ASSET_SCAN_LIMIT = 3000
_MEMBER_ASSET_MAX_PAGES = (
    MEMBER_ASSET_SCAN_LIMIT + MEMBER_LIST_PAGE_SIZE - 1
) // MEMBER_LIST_PAGE_SIZE

AssetPageRequest = Callable[[str, dict[str, Any]], Awaitable[Any]]
AssetPageNormalizer = Callable[..., dict[str, Any]]
AssetTypeFilter = Literal["Image", "Video"]


def _http(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message}},
    )


def clean_multi_values(values: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        for part in raw.split(","):
            value = part.strip()
            if value and value not in seen:
                seen.add(value)
                cleaned.append(value)
    return cleaned


def sort_fields(
    *,
    sort_by: str | None,
    sort_order: str | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    if sort_by is not None:
        clean_sort_by = sort_by.strip()
        if not clean_sort_by or len(clean_sort_by) > 64:
            raise _http("invalid_sort", "sort_by is invalid", 422)
        result["SortBy"] = clean_sort_by
    if sort_order is not None:
        clean_sort_order = sort_order.strip().lower()
        if clean_sort_order not in {"asc", "desc"}:
            raise _http(
                "invalid_sort",
                "sort_order must be Asc or Desc",
                422,
            )
        result["SortOrder"] = clean_sort_order.title()
    return result


def group_list_payload(
    provider: VideoProviderDefinition,
    *,
    name: str | None,
    group_ids: list[str] | None,
    page_number: int,
    page_size: int,
    sort_by: str | None,
    sort_order: str | None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {"GroupType": _AIGC_GROUP_TYPE}
    if name is not None and name.strip():
        filters["Name"] = name.strip()
    cleaned_group_ids = clean_multi_values(group_ids)
    if cleaned_group_ids:
        filters["GroupIds"] = cleaned_group_ids
    return {
        "ProjectName": provider.project_name,
        "Filter": filters,
        "PageNumber": page_number,
        "PageSize": page_size,
        **sort_fields(sort_by=sort_by, sort_order=sort_order),
    }


def asset_list_payload(
    provider: VideoProviderDefinition,
    *,
    name: str | None,
    group_ids: list[str] | None,
    statuses: list[str] | None,
    page_number: int,
    page_size: int,
    sort_by: str | None,
    sort_order: str | None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {"GroupType": _AIGC_GROUP_TYPE}
    if name is not None and name.strip():
        filters["Name"] = name.strip()
    cleaned_group_ids = clean_multi_values(group_ids)
    if cleaned_group_ids:
        filters["GroupIds"] = cleaned_group_ids
    cleaned_statuses = clean_multi_values(statuses)
    if cleaned_statuses:
        filters["Statuses"] = cleaned_statuses
    return {
        "ProjectName": provider.project_name,
        "Filter": filters,
        "PageNumber": page_number,
        "PageSize": page_size,
        **sort_fields(sort_by=sort_by, sort_order=sort_order),
    }


async def project_quota_usage(
    *,
    request_page: AssetPageRequest,
    normalize_assets: AssetPageNormalizer,
    normalize_groups: AssetPageNormalizer,
    provider: VideoProviderDefinition,
) -> VideoAssetQuotaUsageOut:
    assets_raw, groups_raw = await asyncio.gather(
        request_page(
            "ListAssets",
            asset_list_payload(
                provider,
                name=None,
                group_ids=None,
                statuses=None,
                page_number=1,
                page_size=1,
                sort_by=None,
                sort_order=None,
            ),
        ),
        request_page(
            "ListAssetGroups",
            group_list_payload(
                provider,
                name=None,
                group_ids=None,
                page_number=1,
                page_size=1,
                sort_by=None,
                sort_order=None,
            ),
        ),
    )
    assets = normalize_assets(
        assets_raw,
        project_name=provider.project_name,
        page_number=1,
        page_size=1,
    )
    groups = normalize_groups(
        groups_raw,
        project_name=provider.project_name,
        page_number=1,
        page_size=1,
    )
    return VideoAssetQuotaUsageOut(
        assets_used=max(0, int(assets.get("total_count") or 0)),
        asset_groups_used=max(0, int(groups.get("total_count") or 0)),
    )


def member_visible_page(
    items: list[dict[str, Any]],
    *,
    owned_ids: frozenset[str],
    page_number: int,
    page_size: int,
) -> dict[str, Any]:
    visible = [item for item in items if str(item.get("id") or "") in owned_ids]
    start = (page_number - 1) * page_size
    return {
        "items": visible[start : start + page_size],
        "total_count": len(visible),
        "page_number": page_number,
        "page_size": page_size,
    }


def asset_filtered_page(
    items: list[dict[str, Any]],
    *,
    owned_ids: frozenset[str] | None,
    asset_types: list[AssetTypeFilter] | None,
    page_number: int,
    page_size: int,
) -> dict[str, Any]:
    allowed_types = set(asset_types or [])
    visible: list[dict[str, Any]] = []
    for item in items:
        resource_id = str(item.get("id") or "")
        if owned_ids is not None and resource_id not in owned_ids:
            continue
        if allowed_types and item.get("asset_type") not in allowed_types:
            continue
        visible.append(item)
    start = (page_number - 1) * page_size
    return {
        "items": visible[start : start + page_size],
        "total_count": len(visible),
        "page_number": page_number,
        "page_size": page_size,
    }


def _new_remote_items(
    items: list[dict[str, Any]],
    *,
    seen_ids: set[str],
    remaining: int,
) -> list[dict[str, Any]]:
    new_items: list[dict[str, Any]] = []
    for item in items:
        resource_id = str(item.get("id") or "").strip()
        if not resource_id or resource_id in seen_ids:
            continue
        seen_ids.add(resource_id)
        new_items.append(item)
        if len(new_items) >= remaining:
            break
    return new_items


async def scan_member_asset_pages(
    *,
    request_page: AssetPageRequest,
    normalize_page: AssetPageNormalizer,
    provider: VideoProviderDefinition,
    name: str | None,
    group_ids: list[str] | None,
    statuses: list[str] | None,
    sort_by: str | None,
    sort_order: str | None,
) -> list[dict[str, Any]]:
    scanned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for remote_page in range(1, _MEMBER_ASSET_MAX_PAGES + 1):
        raw = await request_page(
            "ListAssets",
            asset_list_payload(
                provider,
                name=name,
                group_ids=group_ids,
                statuses=statuses,
                page_number=remote_page,
                page_size=MEMBER_LIST_PAGE_SIZE,
                sort_by=sort_by,
                sort_order=sort_order,
            ),
        )
        normalized = normalize_page(
            raw,
            project_name=provider.project_name,
            page_number=remote_page,
            page_size=MEMBER_LIST_PAGE_SIZE,
        )
        page_items = normalized.get("items")
        if not isinstance(page_items, list) or not page_items:
            break
        new_items = _new_remote_items(
            page_items,
            seen_ids=seen_ids,
            remaining=MEMBER_ASSET_SCAN_LIMIT - len(scanned),
        )
        if not new_items:
            break
        scanned.extend(new_items)
        if len(scanned) >= MEMBER_ASSET_SCAN_LIMIT:
            break
    return scanned


def member_group_ids(
    receipts: OwnedResourceReceipts,
    requested_group_ids: list[str] | None,
) -> list[str]:
    allowed = set(receipts.resource_ids)
    requested = set(clean_multi_values(requested_group_ids))
    if requested:
        allowed.intersection_update(requested)
    return sorted(allowed)


def member_asset_group_ids(
    receipts: OwnedResourceReceipts,
    requested_group_ids: list[str] | None,
) -> list[str] | None:
    requested = set(clean_multi_values(requested_group_ids))
    receipt_groups = set(receipts.group_ids)
    if requested and receipt_groups:
        return sorted(requested & receipt_groups)
    if requested:
        return sorted(requested)
    return sorted(receipt_groups) or None


async def member_asset_listing(
    *,
    request_page: AssetPageRequest,
    normalize_page: AssetPageNormalizer,
    provider: VideoProviderDefinition,
    receipts: OwnedResourceReceipts,
    name: str | None,
    requested_group_ids: list[str] | None,
    statuses: list[str] | None,
    asset_types: list[AssetTypeFilter] | None,
    page_number: int,
    page_size: int,
    sort_by: str | None,
    sort_order: str | None,
) -> dict[str, Any]:
    upstream_group_ids = member_asset_group_ids(receipts, requested_group_ids)
    if clean_multi_values(requested_group_ids) and not upstream_group_ids:
        return member_visible_page(
            [],
            owned_ids=receipts.resource_ids,
            page_number=page_number,
            page_size=page_size,
        )
    scanned = await scan_member_asset_pages(
        request_page=request_page,
        normalize_page=normalize_page,
        provider=provider,
        name=name,
        group_ids=upstream_group_ids,
        statuses=statuses,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return asset_filtered_page(
        scanned,
        owned_ids=receipts.resource_ids,
        asset_types=asset_types,
        page_number=page_number,
        page_size=page_size,
    )


async def admin_asset_listing(
    *,
    request_page: AssetPageRequest,
    normalize_page: AssetPageNormalizer,
    provider: VideoProviderDefinition,
    name: str | None,
    group_ids: list[str] | None,
    statuses: list[str] | None,
    asset_types: list[AssetTypeFilter] | None,
    page_number: int,
    page_size: int,
    sort_by: str | None,
    sort_order: str | None,
) -> dict[str, Any]:
    if not asset_types:
        raw = await request_page(
            "ListAssets",
            asset_list_payload(
                provider,
                name=name,
                group_ids=group_ids,
                statuses=statuses,
                page_number=page_number,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
            ),
        )
        return normalize_page(
            raw,
            project_name=provider.project_name,
            page_number=page_number,
            page_size=page_size,
        )
    scanned = await scan_member_asset_pages(
        request_page=request_page,
        normalize_page=normalize_page,
        provider=provider,
        name=name,
        group_ids=group_ids,
        statuses=statuses,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return asset_filtered_page(
        scanned,
        owned_ids=None,
        asset_types=asset_types,
        page_number=page_number,
        page_size=page_size,
    )
