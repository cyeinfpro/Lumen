"""HTTP query implementations for the Volcano asset route facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.video_asset_schemas import (
    VideoAssetCapabilitiesOut,
    VideoAssetGroupListOut,
    VideoAssetListOut,
    VideoAssetQuotaUsageOut,
)
from lumen_core.video_providers import VideoProviderDefinition


@dataclass(frozen=True)
class QueryRouteDependencies:
    provider_state: Callable[
        ...,
        Awaitable[tuple[VideoProviderDefinition | None, str | None]],
    ]
    public_base_url: Callable[[Request, AsyncSession], Awaitable[str]]
    require_provider: Callable[..., Awaitable[VideoProviderDefinition]]
    project_quota_usage: Callable[..., Awaitable[VideoAssetQuotaUsageOut]]
    client_factory: Callable[[VideoProviderDefinition], Any]
    normalize_assets: Callable[..., dict[str, Any]]
    normalize_groups: Callable[..., dict[str, Any]]
    is_admin: Callable[[Any], bool]
    owned_resource_receipts: Callable[..., Awaitable[Any]]
    member_group_ids: Callable[..., list[str]]
    group_list_payload: Callable[..., dict[str, Any]]
    member_visible_page: Callable[..., dict[str, Any]]
    require_group_shape: Callable[
        [dict[str, Any], VideoProviderDefinition],
        None,
    ]
    member_asset_listing: Callable[..., Awaitable[dict[str, Any]]]
    admin_asset_listing: Callable[..., Awaitable[dict[str, Any]]]
    require_asset_shape: Callable[
        [dict[str, Any], VideoProviderDefinition],
        None,
    ]
    member_list_page_size: int


async def get_capabilities(
    *,
    model: str,
    request: Request,
    db: AsyncSession,
    deps: QueryRouteDependencies,
) -> VideoAssetCapabilitiesOut:
    provider, reason = await deps.provider_state(db, model=model)
    public_base_url: str | None = None
    if reason is None:
        try:
            public_base_url = await deps.public_base_url(request, db)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            error = detail.get("error") if isinstance(detail, dict) else None
            reason = (
                str(error.get("code"))
                if isinstance(error, dict) and error.get("code")
                else "video_asset_public_url_missing"
            )
    return VideoAssetCapabilitiesOut(
        enabled=reason is None,
        reason=reason,
        provider_name=provider.name if provider is not None else None,
        project_name=(
            provider.project_name
            if provider is not None and provider.kind == "volcano"
            else None
        ),
        region=(
            provider.region
            if provider is not None and provider.kind == "volcano"
            else None
        ),
        public_base_url=public_base_url,
    )


async def get_usage(
    *,
    model: str,
    db: AsyncSession,
    deps: QueryRouteDependencies,
) -> VideoAssetQuotaUsageOut:
    provider = await deps.require_provider(db, model=model)
    return await deps.project_quota_usage(
        request_page=deps.client_factory(provider).request,
        normalize_assets=deps.normalize_assets,
        normalize_groups=deps.normalize_groups,
        provider=provider,
    )


async def list_groups(
    *,
    model: str,
    user: Any,
    db: AsyncSession,
    name: str | None,
    group_ids: list[str] | None,
    page_number: int,
    page_size: int,
    sort_by: str | None,
    sort_order: str | None,
    deps: QueryRouteDependencies,
) -> VideoAssetGroupListOut:
    provider = await deps.require_provider(db, model=model)
    member_receipts: Any | None = None
    upstream_group_ids = group_ids
    upstream_page_number = page_number
    upstream_page_size = page_size
    if not deps.is_admin(user):
        member_receipts = await deps.owned_resource_receipts(
            db,
            user=user,
            provider=provider,
            resource_type="group",
        )
        upstream_group_ids = deps.member_group_ids(member_receipts, group_ids)
        if not upstream_group_ids:
            return VideoAssetGroupListOut(
                page_number=page_number,
                page_size=page_size,
            )
        upstream_page_number = 1
        upstream_page_size = deps.member_list_page_size
    raw = await deps.client_factory(provider).request(
        "ListAssetGroups",
        deps.group_list_payload(
            provider,
            name=name,
            group_ids=upstream_group_ids,
            page_number=upstream_page_number,
            page_size=upstream_page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        ),
    )
    normalized = deps.normalize_groups(
        raw,
        project_name=provider.project_name,
        page_number=page_number,
        page_size=page_size,
    )
    visible = normalized["items"]
    if member_receipts is not None:
        normalized = deps.member_visible_page(
            visible,
            owned_ids=member_receipts.resource_ids,
            page_number=page_number,
            page_size=page_size,
        )
        visible = normalized["items"]
    for group in visible:
        deps.require_group_shape(group, provider)
    return VideoAssetGroupListOut(**normalized)


async def list_assets(
    *,
    model: str,
    user: Any,
    db: AsyncSession,
    name: str | None,
    group_ids: list[str] | None,
    statuses: list[str] | None,
    page_number: int,
    page_size: int,
    sort_by: str | None,
    sort_order: str | None,
    asset_types: list[str] | None,
    deps: QueryRouteDependencies,
) -> VideoAssetListOut:
    provider = await deps.require_provider(db, model=model)
    if not deps.is_admin(user):
        member_receipts = await deps.owned_resource_receipts(
            db,
            user=user,
            provider=provider,
            resource_type="asset",
        )
        if not member_receipts.resource_ids:
            return VideoAssetListOut(
                page_number=page_number,
                page_size=page_size,
            )
        normalized = await deps.member_asset_listing(
            request_page=deps.client_factory(provider).request,
            normalize_page=deps.normalize_assets,
            provider=provider,
            receipts=member_receipts,
            name=name,
            requested_group_ids=group_ids,
            statuses=statuses,
            asset_types=asset_types,
            page_number=page_number,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        for asset in normalized["items"]:
            deps.require_asset_shape(asset, provider)
        return VideoAssetListOut(**normalized)

    normalized = await deps.admin_asset_listing(
        request_page=deps.client_factory(provider).request,
        normalize_page=deps.normalize_assets,
        provider=provider,
        name=name,
        group_ids=group_ids,
        statuses=statuses,
        asset_types=asset_types,
        page_number=page_number,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    for asset in normalized["items"]:
        deps.require_asset_shape(asset, provider)
    return VideoAssetListOut(**normalized)
