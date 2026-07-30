"""Mutation endpoint orchestration for Volcano asset routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.video_asset_schemas import (
    VideoAssetCreateIn,
    VideoAssetGroupCreateIn,
    VideoAssetGroupUpdateIn,
    VideoAssetOperationOut,
    VideoAssetUpdateIn,
)
from lumen_core.video_providers import VideoProviderDefinition

from .services import LocalAssetSource


@dataclass(frozen=True)
class MutationRouteDependencies:
    require_provider: Callable[..., Awaitable[VideoProviderDefinition]]
    require_resource_owner: Callable[..., Awaitable[None]]
    resolve_local_asset_source: Callable[..., Awaitable[LocalAssetSource]]
    public_base_url: Callable[[Request, AsyncSession], Awaitable[str]]
    queue_operation: Callable[..., Awaitable[VideoAssetOperationOut]]
    new_id: Callable[[], str]
    normalize_asset_name: Callable[..., str]
    aigc_group_type: str


async def create_group(
    *,
    body: VideoAssetGroupCreateIn,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    deps: MutationRouteDependencies,
) -> VideoAssetOperationOut:
    provider = await deps.require_provider(db, model=model)
    fields = {
        "name": body.name,
        "description": body.description,
        "group_type": deps.aigc_group_type,
    }
    return await deps.queue_operation(
        action="create_group",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields={
            "target_id": None,
            "fields": fields,
            **fields,
        },
        audit_details={"resource": "asset_group"},
    )


async def update_group(
    *,
    group_id: str,
    body: VideoAssetGroupUpdateIn,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    deps: MutationRouteDependencies,
) -> VideoAssetOperationOut:
    provider = await deps.require_provider(db, model=model)
    await deps.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="group",
        resource_id=group_id,
    )
    fields = body.model_dump(exclude_none=True)
    return await deps.queue_operation(
        action="update_group",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields={
            "target_id": group_id,
            "fields": fields,
            "group_id": group_id,
            **fields,
        },
        audit_details={"resource": "asset_group", "group_id": group_id},
    )


async def delete_group(
    *,
    group_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    deps: MutationRouteDependencies,
) -> VideoAssetOperationOut:
    provider = await deps.require_provider(db, model=model)
    await deps.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="group",
        resource_id=group_id,
    )
    return await deps.queue_operation(
        action="delete_group",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields={
            "target_id": group_id,
            "fields": {"cascade_assets": True},
            "group_id": group_id,
            "cascade_assets": True,
        },
        audit_details={
            "resource": "asset_group",
            "group_id": group_id,
            "cascade_assets": True,
        },
    )


async def create_asset(
    *,
    body: VideoAssetCreateIn,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    deps: MutationRouteDependencies,
) -> VideoAssetOperationOut:
    provider = await deps.require_provider(db, model=model)
    await deps.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="group",
        resource_id=body.group_id,
    )
    source = await deps.resolve_local_asset_source(
        body=body,
        request=request,
        user_id=user.id,
        db=db,
    )
    public_base_url = await deps.public_base_url(request, db)
    operation_id = deps.new_id()
    asset_name = deps.normalize_asset_name(body.name, fallback_id=operation_id)
    fields = {
        "group_id": body.group_id,
        "name": asset_name,
        "asset_type": source.asset_type,
        "local_source_id": source.local_id,
    }
    return await deps.queue_operation(
        action="create_asset",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_id=operation_id,
        operation_fields={
            "target_id": None,
            "fields": fields,
            **fields,
            "public_base_url": public_base_url,
        },
        audit_details={
            "resource": "asset",
            "group_id": body.group_id,
            "asset_type": source.asset_type,
            "local_source_id": source.local_id,
        },
    )


async def update_asset(
    *,
    asset_id: str,
    body: VideoAssetUpdateIn,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    deps: MutationRouteDependencies,
) -> VideoAssetOperationOut:
    provider = await deps.require_provider(db, model=model)
    await deps.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="asset",
        resource_id=asset_id,
    )
    fields = {"name": body.name}
    return await deps.queue_operation(
        action="update_asset",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields={
            "target_id": asset_id,
            "fields": fields,
            "asset_id": asset_id,
            "name": body.name,
        },
        audit_details={"resource": "asset", "asset_id": asset_id},
    )


async def delete_asset(
    *,
    asset_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    deps: MutationRouteDependencies,
) -> VideoAssetOperationOut:
    provider = await deps.require_provider(db, model=model)
    await deps.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="asset",
        resource_id=asset_id,
    )
    return await deps.queue_operation(
        action="delete_asset",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields={
            "target_id": asset_id,
            "fields": {},
            "asset_id": asset_id,
        },
        audit_details={"resource": "asset", "asset_id": asset_id},
    )
