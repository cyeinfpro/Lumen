"""Single-asset lookup and retry endpoint orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.schemas import VideoAssetOperationOut, VideoAssetOut
from lumen_core.volcano_assets import VolcanoAssetQuotaKey

from .._volcano_asset_retry import RetryDependencies, retry_failed_operation


@dataclass(frozen=True)
class OperationRouteDependencies:
    get_redis: Callable[[], Any]
    redis_get_operation: Callable[..., Awaitable[dict[str, Any] | None]]
    is_admin: Callable[[Any], bool]
    http_error: Callable[..., HTTPException]
    require_provider: Callable[..., Awaitable[Any]]
    provider_snapshot_matches: Callable[[dict[str, Any], Any], bool]
    client_factory: Callable[[Any], Any]
    get_asset: Callable[..., Awaitable[dict[str, Any]]]
    http_error_code: Callable[[HTTPException], str | None]
    require_resource_owner: Callable[..., Awaitable[None]]
    operation_asset_response: Callable[[dict[str, Any]], Any]
    owned_operation: Callable[..., Awaitable[dict[str, Any]]]
    operation_out: Callable[[dict[str, Any]], VideoAssetOperationOut]
    allowed_actions: Any
    rate_limit_http: Callable[..., HTTPException]
    operation_quota_key: Callable[[dict[str, Any]], VolcanoAssetQuotaKey]
    acquire_rate_limit: Callable[..., Awaitable[Any]]
    compare_and_set: Callable[..., Awaitable[Any]]
    release_admission_slot: Callable[..., Awaitable[None]]
    same_operation_scope: Callable[[dict[str, Any], dict[str, Any]], bool]
    enqueue_operation: Callable[..., Awaitable[None]]
    mark_enqueue_failed: Callable[..., Awaitable[dict[str, Any]]]
    utc_iso: Callable[[], str]
    audit_write_best_effort: Callable[..., Awaitable[None]]
    logger: Any


async def get_asset(
    *,
    asset_id: str,
    model: str,
    user: Any,
    db: AsyncSession,
    deps: OperationRouteDependencies,
) -> VideoAssetOut:
    try:
        operation = await deps.redis_get_operation(deps.get_redis(), asset_id)
    except Exception:  # noqa: BLE001
        operation = None
        deps.logger.warning("video_asset.operation_lookup_failed", exc_info=True)
    if operation is not None:
        owned = str(operation.get("user_id") or "") == str(user.id)
        same_model = not operation.get("model") or operation.get("model") == model
        if (
            not (deps.is_admin(user) or owned)
            or not same_model
            or operation.get("action") != "create_asset"
        ):
            raise deps.http_error(
                "video_asset_operation_not_found",
                "video asset operation was not found",
                404,
            )
        result = operation.get("result")
        if operation.get("status") == "succeeded" and isinstance(result, dict):
            real_asset_id = str(result.get("id") or "")
            if real_asset_id:
                provider = await deps.require_provider(db, model=model)
                if deps.provider_snapshot_matches(operation, provider):
                    try:
                        current = await deps.get_asset(
                            deps.client_factory(provider),
                            provider,
                            real_asset_id,
                        )
                    except HTTPException as exc:
                        if deps.http_error_code(exc) != "volcano_asset_not_found":
                            raise
                    else:
                        return VideoAssetOut(**current)
        return VideoAssetOut(
            **deps.operation_asset_response(operation).model_dump(
                include=set(VideoAssetOut.model_fields)
            )
        )
    provider = await deps.require_provider(db, model=model)
    await deps.require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="asset",
        resource_id=asset_id,
    )
    asset = await deps.get_asset(
        deps.client_factory(provider),
        provider,
        asset_id,
    )
    return VideoAssetOut(**asset)


async def retry_operation(
    *,
    operation_id: str,
    request: Request,
    user: Any,
    db: AsyncSession,
    deps: OperationRouteDependencies,
) -> VideoAssetOperationOut:
    redis = deps.get_redis()
    operation = await deps.owned_operation(
        operation_id=operation_id,
        user_id=user.id,
        redis=redis,
    )
    result = await retry_failed_operation(
        redis,
        operation_id,
        operation,
        user_id=str(user.id),
        allowed_actions=deps.allowed_actions,
        deps=RetryDependencies(
            http_error=deps.http_error,
            rate_limit_error=deps.rate_limit_http,
            operation_quota_key=deps.operation_quota_key,
            acquire_rate_limit=deps.acquire_rate_limit,
            compare_and_set=deps.compare_and_set,
            release_admission_slot=deps.release_admission_slot,
            same_operation_scope=deps.same_operation_scope,
            enqueue_operation=deps.enqueue_operation,
            mark_enqueue_failed=deps.mark_enqueue_failed,
            utc_iso=deps.utc_iso,
            logger=deps.logger,
        ),
    )
    operation = result.operation
    if result.audit_required:
        await deps.audit_write_best_effort(
            db=db,
            request=request,
            user=user,
            event_type=(
                f"video_asset_operation.{result.action}.retry"
                if operation.get("status") != "failed"
                else f"video_asset_operation.{result.action}.retry_enqueue_failed"
            ),
            details={
                "operation_id": operation_id,
                "action": result.action,
                "attempt": operation["attempt"],
                "target_id": operation.get("target_id"),
                "model": operation.get("model"),
                "provider_name": operation.get("provider_name"),
                "project_name": operation.get("project_name"),
            },
        )
    return deps.operation_out(operation)
