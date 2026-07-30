"""User-facing Volcano AIGC asset management routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import new_uuid7
from lumen_core.schemas import (
    VideoAssetCapabilitiesOut,
    VideoAssetCreateIn,
    VideoAssetGroupCreateIn,
    VideoAssetGroupListOut,
    VideoAssetGroupUpdateIn,
    VideoAssetListOut,
    VideoAssetOperationAction,
    VideoAssetOperationOut,
    VideoAssetOut,
    VideoAssetQuotaUsageOut,
    VideoAssetUpdateIn,
)
from lumen_core.video_providers import VideoProviderDefinition
from lumen_core.volcano_assets import (
    VolcanoAssetCreateRateLimited,
    VolcanoAssetQuotaKey,
    acquire_volcano_create_rate_limit,
    compare_and_set_volcano_asset_operation,
    normalize_asset_group_list,
    normalize_asset_list,
    normalize_volcano_asset_name,
    release_volcano_create_rate_limit,
    volcano_asset_quota_key,
)

from ..arq_pool import get_arq_pool
from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..public_urls import resolve_public_base_url
from ..redis_client import get_redis
from ..services.volcano_asset_client import VolcanoAssetClient
from ._volcano_asset_listing import (
    AssetTypeFilter,
    admin_asset_listing as _admin_asset_listing,
    asset_list_payload as _asset_list_payload,  # noqa: F401
    clean_multi_values as _clean_multi_values,  # noqa: F401
    group_list_payload as _group_list_payload,
    member_asset_listing as _member_asset_listing,
    member_group_ids as _member_group_ids,
    member_visible_page as _member_visible_page,
    project_quota_usage as _project_quota_usage,
    sort_fields as _sort_fields,  # noqa: F401
)
from ._volcano_asset_ownership import (
    OwnedResourceReceipts,
    operation_matches_provider_snapshot,
    owned_resource_receipts,
    resource_owner_user_id,
)
from .videos import video_provider_state
from .volcano_assets_parts import mutations as _mutation_routes
from .volcano_assets_parts import operation_routes as _operation_routes
from .volcano_assets_parts import operations as _operations
from .volcano_assets_parts import routes as _route_queries
from .volcano_assets_parts import serialization as _serialization
from .volcano_assets_parts import services as _services
from .volcano_assets_parts import validation as _validation


router = APIRouter(prefix="/video-assets", tags=["video-assets"])
logger = logging.getLogger(__name__)

_AIGC_GROUP_TYPE = _validation.AIGC_GROUP_TYPE
_OPERATION_JOB_NAME = _operations.OPERATION_JOB_NAME
_OPERATION_ACTIONS = _serialization.OPERATION_ACTIONS
_REDIS_RETRY_ATTEMPTS = _operations.REDIS_RETRY_ATTEMPTS
_REDIS_RETRY_BASE_DELAY_SECONDS = _operations.REDIS_RETRY_BASE_DELAY_SECONDS
_MEMBER_LIST_PAGE_SIZE = 100
VOLCANO_ASSET_CREATE_QPM = _operations.VOLCANO_ASSET_CREATE_QPM
VOLCANO_ASSET_CREATE_WINDOW_SECONDS = _operations.VOLCANO_ASSET_CREATE_WINDOW_SECONDS
_capability = _validation.capability
_is_admin = _validation.is_admin
_utc_iso = _serialization.utc_iso
_retry_redis_call = _operations.retry_redis_call
_operation_quota_key = _serialization.operation_quota_key
_operation_asset_response = _serialization.operation_asset_response
_same_operation_scope = _serialization.same_operation_scope
_http_error_code = _validation.http_error_code


def _mutation_route_dependencies() -> _mutation_routes.MutationRouteDependencies:
    return _mutation_routes.MutationRouteDependencies(
        require_provider=_require_provider,
        require_resource_owner=_require_resource_owner,
        resolve_local_asset_source=_resolve_local_asset_source,
        public_base_url=_public_base_url,
        queue_operation=_queue_operation,
        new_id=new_uuid7,
        normalize_asset_name=normalize_volcano_asset_name,
        aigc_group_type=_AIGC_GROUP_TYPE,
    )


def _operation_route_dependencies() -> _operation_routes.OperationRouteDependencies:
    return _operation_routes.OperationRouteDependencies(
        get_redis=get_redis,
        redis_get_operation=_redis_get_operation,
        is_admin=_is_admin,
        http_error=_http,
        require_provider=_require_provider,
        provider_snapshot_matches=operation_matches_provider_snapshot,
        client_factory=VolcanoAssetClient,
        get_asset=_get_asset,
        http_error_code=_http_error_code,
        require_resource_owner=_require_resource_owner,
        operation_asset_response=_operation_asset_response,
        owned_operation=_owned_operation,
        operation_out=_operation_out,
        allowed_actions=_OPERATION_ACTIONS,
        rate_limit_http=_rate_limit_http,
        operation_quota_key=_operation_quota_key,
        acquire_rate_limit=acquire_volcano_create_rate_limit,
        compare_and_set=compare_and_set_volcano_asset_operation,
        release_admission_slot=_release_admission_slot,
        same_operation_scope=_same_operation_scope,
        enqueue_operation=_enqueue_operation,
        mark_enqueue_failed=_mark_enqueue_failed,
        utc_iso=_utc_iso,
        audit_write_best_effort=_audit_write_best_effort,
        logger=logger,
    )


def _query_route_dependencies() -> _route_queries.QueryRouteDependencies:
    return _route_queries.QueryRouteDependencies(
        provider_state=_provider_state,
        public_base_url=_public_base_url,
        require_provider=_require_provider,
        project_quota_usage=_project_quota_usage,
        client_factory=VolcanoAssetClient,
        normalize_assets=normalize_asset_list,
        normalize_groups=normalize_asset_group_list,
        is_admin=_is_admin,
        owned_resource_receipts=_owned_resource_receipts,
        member_group_ids=_member_group_ids,
        group_list_payload=_group_list_payload,
        member_visible_page=_member_visible_page,
        require_group_shape=_require_group_shape,
        member_asset_listing=_member_asset_listing,
        admin_asset_listing=_admin_asset_listing,
        require_asset_shape=_require_asset_shape,
        member_list_page_size=_MEMBER_LIST_PAGE_SIZE,
    )


def _http(
    code: str,
    message: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    **details: Any,
) -> HTTPException:
    return _validation.http_error(
        code,
        message,
        status_code,
        headers=headers,
        **details,
    )


async def _provider_state(
    db: AsyncSession,
    *,
    model: str,
) -> tuple[VideoProviderDefinition | None, str | None]:
    return await _services.provider_state(
        db,
        model=model,
        load_provider_state=video_provider_state,
        capability=_capability,
    )


async def _require_provider(
    db: AsyncSession,
    *,
    model: str,
) -> VideoProviderDefinition:
    return await _services.require_provider(
        db,
        model=model,
        get_provider_state=_provider_state,
        http_error=_http,
    )


async def _resource_owner_user_id(
    db: AsyncSession,
    *,
    provider: VideoProviderDefinition,
    resource_type: str,
    resource_id: str,
) -> str | None:
    return await resource_owner_user_id(
        db,
        provider=provider,
        resource_type=resource_type,
        resource_id=resource_id,
    )


async def _owned_resource_receipts(
    db: AsyncSession,
    *,
    user: Any,
    provider: VideoProviderDefinition,
    resource_type: str,
) -> OwnedResourceReceipts:
    return await owned_resource_receipts(
        db,
        provider=provider,
        resource_type=resource_type,
        user_id=str(user.id),
    )


async def _require_resource_owner(
    db: AsyncSession,
    *,
    user: Any,
    provider: VideoProviderDefinition,
    resource_type: str,
    resource_id: str,
) -> None:
    await _services.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type=resource_type,
        resource_id=resource_id,
        is_admin=_is_admin,
        resource_owner_user_id=_resource_owner_user_id,
        http_error=_http,
    )


def _require_group_shape(
    group: dict[str, Any],
    provider: VideoProviderDefinition,
) -> None:
    _validation.require_group_shape(group, provider, http_error=_http)


def _require_asset_shape(
    asset: dict[str, Any],
    provider: VideoProviderDefinition,
) -> None:
    _validation.require_asset_shape(asset, provider, http_error=_http)


async def _get_group(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    group_id: str,
) -> dict[str, Any]:
    return await _services.get_group(
        client,
        provider,
        group_id,
        require_group_shape=_require_group_shape,
    )


async def _get_asset(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    asset_id: str,
) -> dict[str, Any]:
    return await _services.get_asset(
        client,
        provider,
        asset_id,
        require_asset_shape=_require_asset_shape,
        get_group=_get_group,
    )


def _validate_public_reference_url(url: str) -> str:
    return _validation.validate_public_reference_url(url, http_error=_http)


async def _public_base_url(request: Request, db: AsyncSession) -> str:
    return await _services.public_base_url(
        request,
        db,
        resolve_public_base_url=resolve_public_base_url,
        validate_public_reference_url=_validate_public_reference_url,
        http_error=_http,
    )


_LocalAssetSource = _services.LocalAssetSource


async def _resolve_local_asset_source(
    *,
    body: VideoAssetCreateIn,
    request: Request,
    user_id: str,
    db: AsyncSession,
) -> _LocalAssetSource:
    del request
    return await _services.resolve_local_asset_source(
        body=body,
        user_id=user_id,
        db=db,
        http_error=_http,
    )


async def _audit_write(
    *,
    db: AsyncSession,
    request: Request,
    user: Any,
    event_type: str,
    details: dict[str, Any],
) -> None:
    await _services.audit_write(
        db=db,
        request=request,
        user=user,
        event_type=event_type,
        details=details,
        write_audit=write_audit,
        hash_email=hash_email,
        request_ip_hash=request_ip_hash,
    )


async def _audit_write_best_effort(
    *,
    db: AsyncSession,
    request: Request,
    user: Any,
    event_type: str,
    details: dict[str, Any],
) -> None:
    await _services.audit_write_best_effort(
        db=db,
        request=request,
        user=user,
        event_type=event_type,
        details=details,
        audit_write=_audit_write,
        logger=logger,
    )


async def _redis_get_operation(
    redis: Any,
    operation_id: str,
) -> dict[str, Any] | None:
    return await _operations.redis_get_operation(
        redis,
        operation_id,
        retry_call=_retry_redis_call,
        logger=logger,
    )


async def _redis_set_operation(redis: Any, operation: dict[str, Any]) -> None:
    await _operations.redis_set_operation(
        redis,
        operation,
        retry_call=_retry_redis_call,
    )


def _operation_out(operation: dict[str, Any]) -> VideoAssetOperationOut:
    return _serialization.operation_out(
        operation,
        http_error=_http,
        now_iso=_utc_iso,
    )


async def _owned_operation(
    *,
    operation_id: str,
    user_id: str,
    redis: Any,
) -> dict[str, Any]:
    return await _operations.owned_operation(
        operation_id=operation_id,
        user_id=user_id,
        redis=redis,
        redis_get_operation=_redis_get_operation,
        http_error=_http,
    )


async def _enqueue_operation(operation: dict[str, Any]) -> None:
    await _operations.enqueue_operation(
        operation,
        get_arq_pool=get_arq_pool,
        retry_call=_retry_redis_call,
    )


async def _release_admission_slot(
    redis: Any,
    quota_key: VolcanoAssetQuotaKey,
    member: str,
) -> None:
    await _operations.release_admission_slot(
        redis,
        quota_key,
        member,
        release_rate_limit=release_volcano_create_rate_limit,
        logger=logger,
    )


async def _mark_enqueue_failed(
    redis: Any,
    operation: dict[str, Any],
) -> dict[str, Any]:
    return await _operations.mark_enqueue_failed(
        redis,
        operation,
        compare_and_set=compare_and_set_volcano_asset_operation,
        now_iso=_utc_iso,
    )


def _same_operation_intent(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    return _serialization.same_operation_intent(
        left,
        right,
        same_scope=_same_operation_scope,
    )


async def _queue_operation(
    *,
    action: VideoAssetOperationAction,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    provider: VideoProviderDefinition,
    operation_fields: dict[str, Any],
    audit_details: dict[str, Any],
    operation_id: str | None = None,
) -> VideoAssetOperationOut:
    return await _operations.queue_operation(
        action=action,
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields=operation_fields,
        audit_details=audit_details,
        operation_id=operation_id,
        deps=_operations.QueueOperationDependencies(
            new_id=new_uuid7,
            now_iso=_utc_iso,
            get_redis=get_redis,
            hash_email=hash_email,
            request_ip_hash=request_ip_hash,
            acquire_rate_limit=acquire_volcano_create_rate_limit,
            quota_key=volcano_asset_quota_key,
            redis_set_operation=_redis_set_operation,
            redis_get_operation=_redis_get_operation,
            same_operation_intent=_same_operation_intent,
            release_admission_slot=_release_admission_slot,
            enqueue_operation=_enqueue_operation,
            mark_enqueue_failed=_mark_enqueue_failed,
            audit_write_best_effort=_audit_write_best_effort,
            operation_out=_operation_out,
            rate_limit_http=_rate_limit_http,
            http_error=_http,
            logger=logger,
        ),
    )


def _rate_limit_http(exc: VolcanoAssetCreateRateLimited) -> HTTPException:
    return _operations.rate_limit_http(exc, http_error=_http)


@router.get("/capabilities", response_model=VideoAssetCapabilitiesOut)
async def get_capabilities(
    model: Annotated[str, Query(min_length=1, max_length=128)],
    request: Request,
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetCapabilitiesOut:
    return await _route_queries.get_capabilities(
        model=model,
        request=request,
        db=db,
        deps=_query_route_dependencies(),
    )


@router.get("/usage", response_model=VideoAssetQuotaUsageOut)
async def get_usage(
    model: Annotated[str, Query(min_length=1, max_length=128)],
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetQuotaUsageOut:
    return await _route_queries.get_usage(
        model=model,
        db=db,
        deps=_query_route_dependencies(),
    )


@router.get("/groups", response_model=VideoAssetGroupListOut)
async def list_groups(
    model: Annotated[str, Query(min_length=1, max_length=128)],
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str | None, Query(max_length=64)] = None,
    group_ids: Annotated[list[str] | None, Query()] = None,
    page_number: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort_by: Annotated[str | None, Query(max_length=64)] = None,
    sort_order: Annotated[str | None, Query(max_length=4)] = None,
) -> VideoAssetGroupListOut:
    return await _route_queries.list_groups(
        model=model,
        user=user,
        db=db,
        name=name,
        group_ids=group_ids,
        page_number=page_number,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        deps=_query_route_dependencies(),
    )


@router.post(
    "/groups",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def create_group(
    body: VideoAssetGroupCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[str, Query(min_length=1, max_length=128)],
) -> VideoAssetOperationOut:
    return await _mutation_routes.create_group(
        body=body,
        request=request,
        user=user,
        db=db,
        model=model,
        deps=_mutation_route_dependencies(),
    )


@router.patch(
    "/groups/{group_id}",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def update_group(
    group_id: str,
    body: VideoAssetGroupUpdateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[str, Query(min_length=1, max_length=128)],
) -> VideoAssetOperationOut:
    return await _mutation_routes.update_group(
        group_id=group_id,
        body=body,
        request=request,
        user=user,
        db=db,
        model=model,
        deps=_mutation_route_dependencies(),
    )


@router.delete(
    "/groups/{group_id}",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def delete_group(
    group_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[str, Query(min_length=1, max_length=128)],
) -> VideoAssetOperationOut:
    return await _mutation_routes.delete_group(
        group_id=group_id,
        request=request,
        user=user,
        db=db,
        model=model,
        deps=_mutation_route_dependencies(),
    )


@router.get("/assets", response_model=VideoAssetListOut)
async def list_assets(
    model: Annotated[str, Query(min_length=1, max_length=128)],
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    name: Annotated[str | None, Query(max_length=64)] = None,
    group_ids: Annotated[list[str] | None, Query()] = None,
    statuses: Annotated[list[str] | None, Query()] = None,
    page_number: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort_by: Annotated[str | None, Query(max_length=64)] = None,
    sort_order: Annotated[str | None, Query(max_length=4)] = None,
    asset_types: Annotated[list[AssetTypeFilter] | None, Query()] = None,
) -> VideoAssetListOut:
    return await _route_queries.list_assets(
        model=model,
        user=user,
        db=db,
        name=name,
        group_ids=group_ids,
        statuses=statuses,
        page_number=page_number,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        asset_types=asset_types,
        deps=_query_route_dependencies(),
    )


@router.get("/assets/{asset_id}", response_model=VideoAssetOut)
async def get_asset(
    asset_id: str,
    model: Annotated[str, Query(min_length=1, max_length=128)],
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetOut:
    return await _operation_routes.get_asset(
        asset_id=asset_id,
        model=model,
        user=user,
        db=db,
        deps=_operation_route_dependencies(),
    )


@router.get(
    "/operations/{operation_id}",
    response_model=VideoAssetOperationOut,
)
async def get_operation(
    operation_id: str,
    user: CurrentUser,
) -> VideoAssetOperationOut:
    operation = await _owned_operation(
        operation_id=operation_id,
        user_id=user.id,
        redis=get_redis(),
    )
    return _operation_out(operation)


@router.post(
    "/operations/{operation_id}/retry",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def retry_operation(
    operation_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetOperationOut:
    return await _operation_routes.retry_operation(
        operation_id=operation_id,
        request=request,
        user=user,
        db=db,
        deps=_operation_route_dependencies(),
    )


@router.post(
    "/assets",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def create_asset(
    body: VideoAssetCreateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[str, Query(min_length=1, max_length=128)],
) -> VideoAssetOperationOut:
    return await _mutation_routes.create_asset(
        body=body,
        request=request,
        user=user,
        db=db,
        model=model,
        deps=_mutation_route_dependencies(),
    )


@router.patch(
    "/assets/{asset_id}",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def update_asset(
    asset_id: str,
    body: VideoAssetUpdateIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[str, Query(min_length=1, max_length=128)],
) -> VideoAssetOperationOut:
    return await _mutation_routes.update_asset(
        asset_id=asset_id,
        body=body,
        request=request,
        user=user,
        db=db,
        model=model,
        deps=_mutation_route_dependencies(),
    )


@router.delete(
    "/assets/{asset_id}",
    response_model=VideoAssetOperationOut,
    status_code=202,
    dependencies=[Depends(verify_csrf)],
)
async def delete_asset(
    asset_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    model: Annotated[str, Query(min_length=1, max_length=128)],
) -> VideoAssetOperationOut:
    return await _mutation_routes.delete_asset(
        asset_id=asset_id,
        request=request,
        user=user,
        db=db,
        model=model,
        deps=_mutation_route_dependencies(),
    )


__all__ = ["router"]
