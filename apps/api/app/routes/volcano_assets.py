"""User-facing Volcano AIGC asset management routes."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, Video, new_uuid7
from lumen_core.schemas import (
    VideoAssetCapabilitiesOut,
    VideoAssetCreateAcceptedOut,
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
from lumen_core.url_security import is_private_host
from lumen_core.video_providers import (
    VideoProviderDefinition,
    select_video_provider,
    video_provider_binding_fingerprint,
)

from ..arq_pool import get_arq_pool
from ..audit import hash_email, request_ip_hash, write_audit
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..public_urls import resolve_public_base_url
from ..redis_client import get_redis
from ..volcano_assets import (
    VOLCANO_ASSET_CREATE_QPM,
    VOLCANO_ASSET_CREATE_WINDOW_SECONDS,
    VOLCANO_ASSET_OPERATION_TTL_SECONDS,
    VolcanoAssetClient,
    VolcanoAssetCreateRateLimited,
    VolcanoAssetQuotaKey,
    VolcanoAssetRedisUnavailable,
    acquire_volcano_create_rate_limit,
    compare_and_set_volcano_asset_operation,
    normalize_asset,
    normalize_asset_group,
    normalize_asset_group_list,
    normalize_asset_list,
    normalize_volcano_asset_name,
    release_volcano_create_rate_limit,
    volcano_asset_operation_key,
    volcano_asset_quota_key,
)
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
from ._volcano_asset_retry import (
    RetryDependencies,
    retry_failed_operation,
)
from .videos import _video_provider_state


router = APIRouter(prefix="/video-assets", tags=["video-assets"])
logger = logging.getLogger(__name__)

_AIGC_GROUP_TYPE = "AIGC"
_OPERATION_JOB_NAME = "process_volcano_asset_operation"
_OPERATION_ACTIONS = frozenset(
    {
        "create_group",
        "update_group",
        "delete_group",
        "create_asset",
        "update_asset",
        "delete_asset",
    }
)
_REDIS_RETRY_ATTEMPTS = 3
_REDIS_RETRY_BASE_DELAY_SECONDS = 0.02
_MEMBER_LIST_PAGE_SIZE = 100


def _http(
    code: str,
    message: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    **details: Any,
) -> HTTPException:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return HTTPException(
        status_code=status_code,
        detail={"error": error},
        headers=headers,
    )


def _capability(
    providers: list[VideoProviderDefinition],
    *,
    model: str,
    errors: list[str] | None = None,
) -> tuple[VideoProviderDefinition | None, str | None]:
    if errors:
        return None, "video_provider_config_invalid"
    provider = select_video_provider(
        providers,
        model=model,
        action="reference",
    )
    if provider is None:
        return None, "reference_provider_missing"
    if provider.kind != "volcano":
        return provider, "reference_provider_not_official_volcano"
    if not provider.asset_management_ready:
        return provider, "volcano_asset_credentials_missing"
    return provider, None


async def _provider_state(
    db: AsyncSession,
    *,
    model: str,
) -> tuple[VideoProviderDefinition | None, str | None]:
    providers, errors = await _video_provider_state(db)
    return _capability(providers, model=model, errors=errors)


async def _require_provider(
    db: AsyncSession,
    *,
    model: str,
) -> VideoProviderDefinition:
    provider, reason = await _provider_state(db, model=model)
    if reason == "video_provider_config_invalid":
        raise _http(
            "video_provider_config_invalid",
            "video provider configuration is invalid",
            503,
        )
    if reason == "reference_provider_missing":
        raise _http(
            "video_asset_provider_missing",
            "no enabled video provider supports reference assets for this model",
            503,
        )
    if reason == "reference_provider_not_official_volcano":
        raise _http(
            "video_asset_provider_unsupported",
            "the selected reference provider does not support Volcano assets",
            409,
        )
    if reason == "volcano_asset_credentials_missing":
        raise _http(
            "volcano_asset_credentials_missing",
            "the selected Volcano provider is missing asset credentials",
            503,
        )
    if provider is None:
        raise _http(
            "video_asset_provider_missing",
            "no enabled video provider supports reference assets for this model",
            503,
        )
    return provider


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


def _is_admin(user: Any) -> bool:
    return getattr(user, "role", "") == "admin"


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
    if _is_admin(user):
        return
    owner_user_id = await _resource_owner_user_id(
        db,
        provider=provider,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    if owner_user_id != str(user.id):
        raise _http(
            "video_asset_forbidden",
            "only the resource owner or an administrator may access this resource",
            403,
            resource_type=resource_type,
            resource_id=resource_id,
        )


def _require_group_shape(
    group: dict[str, Any],
    provider: VideoProviderDefinition,
) -> None:
    if not group.get("id"):
        raise _http(
            "volcano_asset_invalid_response",
            "Volcano asset service returned a group without an id",
            502,
        )
    if str(group.get("group_type") or "").upper() != _AIGC_GROUP_TYPE:
        raise _http(
            "volcano_asset_scope_mismatch",
            "the asset group is outside the AIGC scope",
            403,
        )
    if group.get("project_name") != provider.project_name:
        raise _http(
            "volcano_asset_scope_mismatch",
            "the asset group is outside the configured project",
            403,
        )


def _require_asset_shape(
    asset: dict[str, Any],
    provider: VideoProviderDefinition,
) -> None:
    if not asset.get("id") or not asset.get("group_id"):
        raise _http(
            "volcano_asset_invalid_response",
            "Volcano asset service returned an incomplete asset",
            502,
        )
    if asset.get("project_name") != provider.project_name:
        raise _http(
            "volcano_asset_scope_mismatch",
            "the asset is outside the configured project",
            403,
        )


async def _get_group(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    group_id: str,
) -> dict[str, Any]:
    raw = await client.request(
        "GetAssetGroup",
        {
            "Id": group_id,
            "ProjectName": provider.project_name,
        },
    )
    group = normalize_asset_group(
        raw,
        project_name=provider.project_name,
        fallback={
            "id": group_id,
            "project_name": provider.project_name,
        },
    )
    _require_group_shape(group, provider)
    return group


async def _get_asset(
    client: VolcanoAssetClient,
    provider: VideoProviderDefinition,
    asset_id: str,
) -> dict[str, Any]:
    raw = await client.request(
        "GetAsset",
        {
            "Id": asset_id,
            "ProjectName": provider.project_name,
        },
    )
    asset = normalize_asset(
        raw,
        project_name=provider.project_name,
        fallback={
            "id": asset_id,
            "project_name": provider.project_name,
        },
    )
    _require_asset_shape(asset, provider)
    await _get_group(client, provider, str(asset["group_id"]))
    return asset


def _validate_public_reference_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or is_private_host(parts.hostname)
    ):
        raise _http(
            "video_asset_public_url_invalid",
            "a public HTTPS URL is required for Volcano asset ingestion",
            503,
        )
    return url


async def _public_base_url(request: Request, db: AsyncSession) -> str:
    try:
        public_base_url = await resolve_public_base_url(request, db)
    except Exception as exc:  # noqa: BLE001
        raise _http(
            "video_asset_public_url_missing",
            "PUBLIC_BASE_URL or site.public_base_url is required for asset ingestion",
            503,
        ) from exc
    return _validate_public_reference_url(public_base_url)


@dataclass(frozen=True)
class _LocalAssetSource:
    asset_type: str
    local_id: str


async def _resolve_local_asset_source(
    *,
    body: VideoAssetCreateIn,
    request: Request,
    user_id: str,
    db: AsyncSession,
) -> _LocalAssetSource:
    if body.image_id is not None:
        image = (
            await db.execute(
                select(Image).where(
                    Image.id == body.image_id,
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if image is None:
            raise _http(
                "video_asset_image_not_found",
                "asset image was not found",
                404,
            )
        return _LocalAssetSource(
            asset_type="Image",
            local_id=image.id,
        )

    video = (
        await db.execute(
            select(Video).where(
                Video.id == body.video_id,
                Video.user_id == user_id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise _http(
            "video_asset_video_not_found",
            "asset video was not found",
            404,
        )
    return _LocalAssetSource(
        asset_type="Video",
        local_id=video.id,
    )


async def _audit_write(
    *,
    db: AsyncSession,
    request: Request,
    user: Any,
    event_type: str,
    details: dict[str, Any],
) -> None:
    await write_audit(
        db,
        event_type=event_type,
        user_id=user.id,
        actor_email_hash=hash_email(user.email),
        actor_ip_hash=request_ip_hash(request),
        details=details,
        autocommit=False,
    )
    await db.commit()


async def _audit_write_best_effort(
    *,
    db: AsyncSession,
    request: Request,
    user: Any,
    event_type: str,
    details: dict[str, Any],
) -> None:
    try:
        await _audit_write(
            db=db,
            request=request,
            user=user,
            event_type=event_type,
            details=details,
        )
    except Exception:  # noqa: BLE001
        logger.error(
            "video_asset.audit_write_failed event_type=%s",
            event_type,
            exc_info=True,
        )


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _retry_redis_call(call: Callable[[], Awaitable[Any]]) -> Any:
    last_error: Exception | None = None
    for attempt in range(_REDIS_RETRY_ATTEMPTS):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 >= _REDIS_RETRY_ATTEMPTS:
                break
            delay = _REDIS_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            await asyncio.sleep(delay + random.uniform(0, delay))
    if last_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("Redis operation failed")
    raise VolcanoAssetRedisUnavailable(
        f"Volcano asset Redis operation failed ({type(last_error).__name__})"
    ) from None


async def _redis_get_operation(
    redis: Any,
    operation_id: str,
) -> dict[str, Any] | None:
    raw = await _retry_redis_call(
        lambda: redis.get(volcano_asset_operation_key(operation_id))
    )
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.error(
            "video_asset.operation_invalid",
        )
        return None
    return payload if isinstance(payload, dict) else None


async def _redis_set_operation(redis: Any, operation: dict[str, Any]) -> None:
    await _retry_redis_call(
        lambda: redis.set(
            volcano_asset_operation_key(str(operation["id"])),
            json.dumps(
                operation,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            ex=VOLCANO_ASSET_OPERATION_TTL_SECONDS,
        )
    )


def _operation_quota_key(operation: dict[str, Any]) -> VolcanoAssetQuotaKey:
    return VolcanoAssetQuotaKey(
        provider_name=str(operation.get("provider_name") or ""),
        project_name=str(operation.get("project_name") or ""),
        region=str(operation.get("region") or ""),
    )


def _operation_out(operation: dict[str, Any]) -> VideoAssetOperationOut:
    action = str(operation.get("action") or "")
    if action not in _OPERATION_ACTIONS:
        raise _http(
            "video_asset_operation_state_invalid",
            "video asset operation state is invalid",
            503,
        )
    return VideoAssetOperationOut(
        id=str(operation["id"]),
        action=action,
        status=str(operation.get("status") or "queued"),
        progress_stage=str(operation.get("progress_stage") or "queued"),
        attempt=max(1, int(operation.get("attempt") or 1)),
        delivery_generation=max(
            0,
            int(operation.get("delivery_generation") or 0),
        ),
        retryable=bool(operation.get("retryable")),
        retry_after_seconds=operation.get("retry_after_seconds"),
        result=operation.get("result"),
        error=operation.get("error"),
        created_at=str(operation.get("created_at") or _utc_iso()),
        updated_at=str(operation.get("updated_at") or _utc_iso()),
        completed_at=operation.get("completed_at"),
    )


def _operation_asset_response(
    operation: dict[str, Any],
) -> VideoAssetCreateAcceptedOut:
    result = operation.get("result")
    if isinstance(result, dict):
        asset = VideoAssetOut(**result)
    else:
        failed = str(operation.get("status") or "") == "failed"
        error = operation.get("error")
        error = error if isinstance(error, dict) else {}
        asset = VideoAssetOut(
            id=str(operation["id"]),
            group_id=str(operation.get("group_id") or ""),
            name=str(operation.get("name") or ""),
            asset_type=str(operation.get("asset_type") or ""),
            status="Failed" if failed else "Processing",
            url=None,
            project_name=str(operation.get("project_name") or ""),
            error_code=str(error.get("code") or "") or None,
            error_message=str(error.get("message") or "") or None,
        )
    return VideoAssetCreateAcceptedOut(
        **asset.model_dump(),
        operation_id=str(operation["id"]),
        operation_status=str(operation.get("status") or "queued"),
        progress_stage=str(operation.get("progress_stage") or "queued"),
        retryable=bool(operation.get("retryable")),
        retry_after_seconds=operation.get("retry_after_seconds"),
    )


async def _owned_operation(
    *,
    operation_id: str,
    user_id: str,
    redis: Any,
) -> dict[str, Any]:
    try:
        operation = await _redis_get_operation(redis, operation_id)
    except Exception as exc:  # noqa: BLE001
        raise _http(
            "video_asset_queue_unavailable",
            "video asset operation queue is unavailable",
            503,
        ) from exc
    if operation is None or str(operation.get("user_id") or "") != str(user_id):
        raise _http(
            "video_asset_operation_not_found",
            "video asset operation was not found",
            404,
        )
    return operation


async def _enqueue_operation(operation: dict[str, Any]) -> None:
    attempt = max(1, int(operation.get("attempt") or 1))
    delivery_generation = max(
        0,
        int(operation.get("delivery_generation") or 0),
    )

    async def enqueue() -> Any:
        pool = await get_arq_pool()
        return await pool.enqueue_job(
            _OPERATION_JOB_NAME,
            str(operation["id"]),
            attempt,
            delivery_generation,
            _job_id=(
                f"volcano-asset:{operation['id']}:{attempt}:{delivery_generation}"
            ),
        )

    await _retry_redis_call(enqueue)


async def _release_admission_slot(
    redis: Any,
    quota_key: VolcanoAssetQuotaKey,
    member: str,
) -> None:
    try:
        await release_volcano_create_rate_limit(
            redis,
            quota_key,
            bucket="admission",
            operation_id=member,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "video_asset.admission_release_failed",
            exc_info=True,
        )


async def _mark_enqueue_failed(
    redis: Any,
    operation: dict[str, Any],
) -> dict[str, Any]:
    failed = {
        **operation,
        "status": "failed",
        "progress_stage": "enqueue_failed",
        "retryable": True,
        "retry_after_seconds": 1,
        "updated_at": _utc_iso(),
        "completed_at": _utc_iso(),
        "result": None,
        "error": {
            "code": "video_asset_queue_unavailable",
            "message": "video asset operation queue is unavailable",
            "retryable": True,
            "retry_after_seconds": 1,
        },
    }
    swapped, current = await compare_and_set_volcano_asset_operation(
        redis,
        str(operation["id"]),
        owner_user_id=str(operation.get("user_id") or ""),
        expected_status=str(operation.get("status") or ""),
        expected_attempt=max(1, int(operation.get("attempt") or 1)),
        replacement=failed,
        expected_progress_stage=str(operation.get("progress_stage") or ""),
    )
    return failed if swapped else current or operation


def _same_operation_scope(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    return all(
        str(left.get(key) or "") == str(right.get(key) or "")
        for key in (
            "id",
            "action",
            "user_id",
            "model",
            "provider_name",
            "provider_binding",
            "project_name",
            "region",
        )
    )


def _same_operation_intent(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    return (
        _same_operation_scope(left, right)
        and left.get("target_id") == right.get("target_id")
        and left.get("fields") == right.get("fields")
        and str(left.get("public_base_url") or "")
        == str(right.get("public_base_url") or "")
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
    operation_id = operation_id or new_uuid7()
    now = _utc_iso()
    operation: dict[str, Any] = {
        "id": operation_id,
        "action": action,
        "status": "queued",
        "progress_stage": "queued",
        "attempt": 1,
        "delivery_generation": 0,
        "retryable": False,
        "retry_after_seconds": None,
        "user_id": str(user.id),
        "actor_email_hash": hash_email(user.email),
        "actor_ip_hash": request_ip_hash(request),
        "model": model,
        "provider_name": provider.name,
        "provider_binding": video_provider_binding_fingerprint(provider),
        "project_name": provider.project_name,
        "region": provider.region,
        **operation_fields,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "result": None,
        "error": None,
    }
    redis = get_redis()
    quota_key: VolcanoAssetQuotaKey | None = None
    admission_member: str | None = None
    if action == "create_asset":
        quota_key = volcano_asset_quota_key(provider)
        admission_member = operation_id
        try:
            await acquire_volcano_create_rate_limit(
                redis,
                quota_key,
                bucket="admission",
                operation_id=admission_member,
                now_ms=int(time.time() * 1000),
            )
        except VolcanoAssetCreateRateLimited as exc:
            raise _rate_limit_http(exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise _http(
                "video_asset_queue_unavailable",
                "video asset operation queue is unavailable",
                503,
            ) from exc

    try:
        await _redis_set_operation(redis, operation)
    except Exception as exc:  # noqa: BLE001
        try:
            stored = await _redis_get_operation(redis, operation_id)
        except Exception:  # noqa: BLE001
            stored = None
        if (
            stored is None
            or not _same_operation_intent(stored, operation)
            or stored.get("status") != "queued"
            or max(1, int(stored.get("attempt") or 1)) != 1
        ):
            if quota_key is not None and admission_member is not None:
                await _release_admission_slot(
                    redis,
                    quota_key,
                    admission_member,
                )
            raise _http(
                "video_asset_queue_unavailable",
                "video asset operation queue is unavailable",
                503,
            ) from exc
        operation = stored

    try:
        await _enqueue_operation(operation)
    except Exception:  # noqa: BLE001
        logger.warning(
            "video_asset.enqueue_failed operation_id=%s action=%s",
            operation_id,
            action,
            exc_info=True,
        )
        try:
            operation = await _mark_enqueue_failed(redis, operation)
        except Exception:  # noqa: BLE001
            logger.error(
                "video_asset.enqueue_state_failed operation_id=%s action=%s",
                operation_id,
                action,
                exc_info=True,
            )
        if quota_key is not None and admission_member is not None:
            await _release_admission_slot(
                redis,
                quota_key,
                admission_member,
            )

    await _audit_write_best_effort(
        db=db,
        request=request,
        user=user,
        event_type=(
            f"video_asset_operation.{action}.queued"
            if operation.get("status") != "failed"
            else f"video_asset_operation.{action}.enqueue_failed"
        ),
        details={
            "operation_id": operation_id,
            "action": action,
            "target_id": operation.get("target_id"),
            "field_names": sorted(
                str(key)
                for key in (
                    operation.get("fields")
                    if isinstance(operation.get("fields"), dict)
                    else {}
                )
            ),
            "model": model,
            "provider_name": provider.name,
            "project_name": provider.project_name,
            **audit_details,
        },
    )
    return _operation_out(operation)


def _rate_limit_http(exc: VolcanoAssetCreateRateLimited) -> HTTPException:
    retry_after_seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
    return _http(
        "volcano_asset_create_rate_limited",
        "CreateAsset is limited to 3 requests per 60 seconds",
        429,
        headers={"Retry-After": str(retry_after_seconds)},
        retry_after_ms=exc.retry_after_ms,
        retry_after_seconds=retry_after_seconds,
        limit=VOLCANO_ASSET_CREATE_QPM,
        window_seconds=VOLCANO_ASSET_CREATE_WINDOW_SECONDS,
    )


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    error = detail.get("error") if isinstance(detail, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return str(code) if code else None


@router.get("/capabilities", response_model=VideoAssetCapabilitiesOut)
async def get_capabilities(
    model: Annotated[str, Query(min_length=1, max_length=128)],
    request: Request,
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetCapabilitiesOut:
    provider, reason = await _provider_state(db, model=model)
    public_base_url: str | None = None
    if reason is None:
        try:
            public_base_url = await _public_base_url(request, db)
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


@router.get("/usage", response_model=VideoAssetQuotaUsageOut)
async def get_usage(
    model: Annotated[str, Query(min_length=1, max_length=128)],
    _user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetQuotaUsageOut:
    provider = await _require_provider(db, model=model)
    return await _project_quota_usage(
        request_page=VolcanoAssetClient(provider).request,
        normalize_assets=normalize_asset_list,
        normalize_groups=normalize_asset_group_list,
        provider=provider,
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
    provider = await _require_provider(db, model=model)
    member_receipts: OwnedResourceReceipts | None = None
    upstream_group_ids = group_ids
    upstream_page_number = page_number
    upstream_page_size = page_size
    if not _is_admin(user):
        member_receipts = await _owned_resource_receipts(
            db,
            user=user,
            provider=provider,
            resource_type="group",
        )
        upstream_group_ids = _member_group_ids(member_receipts, group_ids)
        if not upstream_group_ids:
            return VideoAssetGroupListOut(
                page_number=page_number,
                page_size=page_size,
            )
        upstream_page_number = 1
        upstream_page_size = _MEMBER_LIST_PAGE_SIZE
    raw = await VolcanoAssetClient(provider).request(
        "ListAssetGroups",
        _group_list_payload(
            provider,
            name=name,
            group_ids=upstream_group_ids,
            page_number=upstream_page_number,
            page_size=upstream_page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        ),
    )
    normalized = normalize_asset_group_list(
        raw,
        project_name=provider.project_name,
        page_number=page_number,
        page_size=page_size,
    )
    visible = normalized["items"]
    if member_receipts is not None:
        normalized = _member_visible_page(
            visible,
            owned_ids=member_receipts.resource_ids,
            page_number=page_number,
            page_size=page_size,
        )
        visible = normalized["items"]
    for group in visible:
        _require_group_shape(group, provider)
    return VideoAssetGroupListOut(**normalized)


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
    provider = await _require_provider(db, model=model)
    fields = {
        "name": body.name,
        "description": body.description,
        "group_type": _AIGC_GROUP_TYPE,
    }
    return await _queue_operation(
        action="create_group",
        request=request,
        user=user,
        db=db,
        model=model,
        provider=provider,
        operation_fields={
            "target_id": None,
            "fields": fields,
            "name": body.name,
            "description": body.description,
            "group_type": _AIGC_GROUP_TYPE,
        },
        audit_details={
            "resource": "asset_group",
        },
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
    provider = await _require_provider(db, model=model)
    await _require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="group",
        resource_id=group_id,
    )
    fields = body.model_dump(exclude_none=True)
    return await _queue_operation(
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
        audit_details={
            "resource": "asset_group",
            "group_id": group_id,
        },
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
    provider = await _require_provider(db, model=model)
    await _require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="group",
        resource_id=group_id,
    )
    return await _queue_operation(
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
    provider = await _require_provider(db, model=model)
    if not _is_admin(user):
        member_receipts = await _owned_resource_receipts(
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
        normalized = await _member_asset_listing(
            request_page=VolcanoAssetClient(provider).request,
            normalize_page=normalize_asset_list,
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
        visible = normalized["items"]
        for asset in visible:
            _require_asset_shape(asset, provider)
        return VideoAssetListOut(**normalized)

    normalized = await _admin_asset_listing(
        request_page=VolcanoAssetClient(provider).request,
        normalize_page=normalize_asset_list,
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
        _require_asset_shape(asset, provider)
    return VideoAssetListOut(**normalized)


@router.get("/assets/{asset_id}", response_model=VideoAssetOut)
async def get_asset(
    asset_id: str,
    model: Annotated[str, Query(min_length=1, max_length=128)],
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> VideoAssetOut:
    try:
        operation = await _redis_get_operation(get_redis(), asset_id)
    except Exception:  # noqa: BLE001
        operation = None
        logger.warning(
            "video_asset.operation_lookup_failed",
            exc_info=True,
        )
    if operation is not None:
        owned = str(operation.get("user_id") or "") == str(user.id)
        same_model = not operation.get("model") or operation.get("model") == model
        if (
            not (_is_admin(user) or owned)
            or not same_model
            or operation.get("action") != "create_asset"
        ):
            raise _http(
                "video_asset_operation_not_found",
                "video asset operation was not found",
                404,
            )
        result = operation.get("result")
        if operation.get("status") == "succeeded" and isinstance(result, dict):
            real_asset_id = str(result.get("id") or "")
            if real_asset_id:
                provider = await _require_provider(db, model=model)
                if operation_matches_provider_snapshot(operation, provider):
                    try:
                        current = await _get_asset(
                            VolcanoAssetClient(provider),
                            provider,
                            real_asset_id,
                        )
                    except HTTPException as exc:
                        if _http_error_code(exc) != "volcano_asset_not_found":
                            raise
                    else:
                        return VideoAssetOut(**current)
        return VideoAssetOut(
            **_operation_asset_response(operation).model_dump(
                include=set(VideoAssetOut.model_fields)
            )
        )
    provider = await _require_provider(db, model=model)
    await _require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="asset",
        resource_id=asset_id,
    )
    asset = await _get_asset(VolcanoAssetClient(provider), provider, asset_id)
    return VideoAssetOut(**asset)


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
    redis = get_redis()
    operation = await _owned_operation(
        operation_id=operation_id,
        user_id=user.id,
        redis=redis,
    )
    result = await retry_failed_operation(
        redis,
        operation_id,
        operation,
        user_id=str(user.id),
        allowed_actions=_OPERATION_ACTIONS,
        deps=RetryDependencies(
            http_error=_http,
            rate_limit_error=_rate_limit_http,
            operation_quota_key=_operation_quota_key,
            acquire_rate_limit=acquire_volcano_create_rate_limit,
            compare_and_set=compare_and_set_volcano_asset_operation,
            release_admission_slot=_release_admission_slot,
            same_operation_scope=_same_operation_scope,
            enqueue_operation=_enqueue_operation,
            mark_enqueue_failed=_mark_enqueue_failed,
            utc_iso=_utc_iso,
            logger=logger,
        ),
    )
    operation = result.operation
    if result.audit_required:
        await _audit_write_best_effort(
            db=db,
            request=request,
            user=user,
            event_type=(
                f"video_asset_operation.{result.action}.retry"
                if operation.get("status") != "failed"
                else (f"video_asset_operation.{result.action}.retry_enqueue_failed")
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
    return _operation_out(operation)


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
    provider = await _require_provider(db, model=model)
    await _require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="group",
        resource_id=body.group_id,
    )
    source = await _resolve_local_asset_source(
        body=body,
        request=request,
        user_id=user.id,
        db=db,
    )
    public_base_url = await _public_base_url(request, db)
    operation_id = new_uuid7()
    asset_name = normalize_volcano_asset_name(
        body.name,
        fallback_id=operation_id,
    )
    fields = {
        "group_id": body.group_id,
        "name": asset_name,
        "asset_type": source.asset_type,
        "local_source_id": source.local_id,
    }
    return await _queue_operation(
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
            "group_id": body.group_id,
            "name": asset_name,
            "asset_type": source.asset_type,
            "local_source_id": source.local_id,
            "public_base_url": public_base_url,
        },
        audit_details={
            "resource": "asset",
            "group_id": body.group_id,
            "asset_type": source.asset_type,
            "local_source_id": source.local_id,
        },
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
    provider = await _require_provider(db, model=model)
    await _require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="asset",
        resource_id=asset_id,
    )
    fields = {"name": body.name}
    return await _queue_operation(
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
        audit_details={
            "resource": "asset",
            "asset_id": asset_id,
        },
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
    provider = await _require_provider(db, model=model)
    await _require_resource_owner(
        db,
        user=user,
        provider=provider,
        resource_type="asset",
        resource_id=asset_id,
    )
    return await _queue_operation(
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
        audit_details={
            "resource": "asset",
            "asset_id": asset_id,
        },
    )


__all__ = ["router"]
