"""Redis persistence and queue admission services for Volcano asset operations."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.video_asset_schemas import (
    VideoAssetOperationAction,
    VideoAssetOperationOut,
)
from lumen_core.video_providers import (
    VideoProviderDefinition,
    video_provider_binding_fingerprint,
)
from lumen_core.volcano_assets import (
    VOLCANO_ASSET_CREATE_QPM,
    VOLCANO_ASSET_CREATE_WINDOW_SECONDS,
    VOLCANO_ASSET_OPERATION_TTL_SECONDS,
    VolcanoAssetCreateRateLimited,
    VolcanoAssetQuotaKey,
    VolcanoAssetRedisUnavailable,
    volcano_asset_operation_key,
)


REDIS_RETRY_ATTEMPTS = 3
REDIS_RETRY_BASE_DELAY_SECONDS = 0.02
OPERATION_JOB_NAME = "process_volcano_asset_operation"

HttpError = Callable[..., HTTPException]


@dataclass(frozen=True)
class QueueOperationDependencies:
    new_id: Callable[[], str]
    now_iso: Callable[[], str]
    get_redis: Callable[[], Any]
    hash_email: Callable[[str], str]
    request_ip_hash: Callable[[Request], str]
    acquire_rate_limit: Callable[..., Awaitable[None]]
    quota_key: Callable[[VideoProviderDefinition], VolcanoAssetQuotaKey]
    redis_set_operation: Callable[[Any, dict[str, Any]], Awaitable[None]]
    redis_get_operation: Callable[[Any, str], Awaitable[dict[str, Any] | None]]
    same_operation_intent: Callable[
        [dict[str, Any], dict[str, Any]],
        bool,
    ]
    release_admission_slot: Callable[
        [Any, VolcanoAssetQuotaKey, str],
        Awaitable[None],
    ]
    enqueue_operation: Callable[[dict[str, Any]], Awaitable[None]]
    mark_enqueue_failed: Callable[
        [Any, dict[str, Any]],
        Awaitable[dict[str, Any]],
    ]
    audit_write_best_effort: Callable[..., Awaitable[None]]
    operation_out: Callable[[dict[str, Any]], VideoAssetOperationOut]
    rate_limit_http: Callable[[VolcanoAssetCreateRateLimited], HTTPException]
    http_error: HttpError
    logger: logging.Logger


async def retry_redis_call(call: Callable[[], Awaitable[Any]]) -> Any:
    last_error: Exception | None = None
    for attempt in range(REDIS_RETRY_ATTEMPTS):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 >= REDIS_RETRY_ATTEMPTS:
                break
            delay = REDIS_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            await asyncio.sleep(delay + random.uniform(0, delay))
    if last_error is None:  # pragma: no cover - defensive invariant
        raise RuntimeError("Redis operation failed")
    raise VolcanoAssetRedisUnavailable(
        f"Volcano asset Redis operation failed ({type(last_error).__name__})"
    ) from None


async def redis_get_operation(
    redis: Any,
    operation_id: str,
    *,
    retry_call: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
    logger: logging.Logger,
) -> dict[str, Any] | None:
    raw = await retry_call(lambda: redis.get(volcano_asset_operation_key(operation_id)))
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.error("video_asset.operation_invalid")
        return None
    return payload if isinstance(payload, dict) else None


async def redis_set_operation(
    redis: Any,
    operation: dict[str, Any],
    *,
    retry_call: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
) -> None:
    await retry_call(
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


async def owned_operation(
    *,
    operation_id: str,
    user_id: str,
    redis: Any,
    redis_get_operation: Callable[
        [Any, str],
        Awaitable[dict[str, Any] | None],
    ],
    http_error: HttpError,
) -> dict[str, Any]:
    try:
        operation = await redis_get_operation(redis, operation_id)
    except Exception as exc:  # noqa: BLE001
        raise http_error(
            "video_asset_queue_unavailable",
            "video asset operation queue is unavailable",
            503,
        ) from exc
    if operation is None or str(operation.get("user_id") or "") != str(user_id):
        raise http_error(
            "video_asset_operation_not_found",
            "video asset operation was not found",
            404,
        )
    return operation


async def enqueue_operation(
    operation: dict[str, Any],
    *,
    get_arq_pool: Callable[[], Awaitable[Any]],
    retry_call: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]],
) -> None:
    attempt = max(1, int(operation.get("attempt") or 1))
    delivery_generation = max(
        0,
        int(operation.get("delivery_generation") or 0),
    )

    async def enqueue() -> Any:
        pool = await get_arq_pool()
        return await pool.enqueue_job(
            OPERATION_JOB_NAME,
            str(operation["id"]),
            attempt,
            delivery_generation,
            _job_id=(
                f"volcano-asset:{operation['id']}:{attempt}:{delivery_generation}"
            ),
        )

    await retry_call(enqueue)


async def release_admission_slot(
    redis: Any,
    quota_key: VolcanoAssetQuotaKey,
    member: str,
    *,
    release_rate_limit: Callable[..., Awaitable[None]],
    logger: logging.Logger,
) -> None:
    try:
        await release_rate_limit(
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


async def mark_enqueue_failed(
    redis: Any,
    operation: dict[str, Any],
    *,
    compare_and_set: Callable[
        ...,
        Awaitable[tuple[bool, dict[str, Any] | None]],
    ],
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    failed = {
        **operation,
        "status": "failed",
        "progress_stage": "enqueue_failed",
        "retryable": True,
        "retry_after_seconds": 1,
        "updated_at": now_iso(),
        "completed_at": now_iso(),
        "result": None,
        "error": {
            "code": "video_asset_queue_unavailable",
            "message": "video asset operation queue is unavailable",
            "retryable": True,
            "retry_after_seconds": 1,
        },
    }
    swapped, current = await compare_and_set(
        redis,
        str(operation["id"]),
        owner_user_id=str(operation.get("user_id") or ""),
        expected_status=str(operation.get("status") or ""),
        expected_attempt=max(1, int(operation.get("attempt") or 1)),
        replacement=failed,
        expected_progress_stage=str(operation.get("progress_stage") or ""),
    )
    return failed if swapped else current or operation


async def queue_operation(
    *,
    action: VideoAssetOperationAction,
    request: Request,
    user: Any,
    db: AsyncSession,
    model: str,
    provider: VideoProviderDefinition,
    operation_fields: dict[str, Any],
    audit_details: dict[str, Any],
    deps: QueueOperationDependencies,
    operation_id: str | None = None,
) -> VideoAssetOperationOut:
    operation_id = operation_id or deps.new_id()
    now = deps.now_iso()
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
        "actor_email_hash": deps.hash_email(user.email),
        "actor_ip_hash": deps.request_ip_hash(request),
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
    redis = deps.get_redis()
    quota_key: VolcanoAssetQuotaKey | None = None
    admission_member: str | None = None
    if action == "create_asset":
        quota_key = deps.quota_key(provider)
        admission_member = operation_id
        try:
            await deps.acquire_rate_limit(
                redis,
                quota_key,
                bucket="admission",
                operation_id=admission_member,
                now_ms=int(time.time() * 1000),
            )
        except VolcanoAssetCreateRateLimited as exc:
            raise deps.rate_limit_http(exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise deps.http_error(
                "video_asset_queue_unavailable",
                "video asset operation queue is unavailable",
                503,
            ) from exc

    try:
        await deps.redis_set_operation(redis, operation)
    except Exception as exc:  # noqa: BLE001
        try:
            stored = await deps.redis_get_operation(redis, operation_id)
        except Exception:  # noqa: BLE001
            stored = None
        if (
            stored is None
            or not deps.same_operation_intent(stored, operation)
            or stored.get("status") != "queued"
            or max(1, int(stored.get("attempt") or 1)) != 1
        ):
            if quota_key is not None and admission_member is not None:
                await deps.release_admission_slot(
                    redis,
                    quota_key,
                    admission_member,
                )
            raise deps.http_error(
                "video_asset_queue_unavailable",
                "video asset operation queue is unavailable",
                503,
            ) from exc
        operation = stored

    try:
        await deps.enqueue_operation(operation)
    except Exception:  # noqa: BLE001
        deps.logger.warning(
            "video_asset.enqueue_failed operation_id=%s action=%s",
            operation_id,
            action,
            exc_info=True,
        )
        try:
            operation = await deps.mark_enqueue_failed(redis, operation)
        except Exception:  # noqa: BLE001
            deps.logger.error(
                "video_asset.enqueue_state_failed operation_id=%s action=%s",
                operation_id,
                action,
                exc_info=True,
            )
        if quota_key is not None and admission_member is not None:
            await deps.release_admission_slot(
                redis,
                quota_key,
                admission_member,
            )

    await deps.audit_write_best_effort(
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
    return deps.operation_out(operation)


def rate_limit_http(
    exc: VolcanoAssetCreateRateLimited,
    *,
    http_error: HttpError,
) -> HTTPException:
    retry_after_seconds = max(1, math.ceil(exc.retry_after_ms / 1000))
    return http_error(
        "volcano_asset_create_rate_limited",
        "CreateAsset is limited to 3 requests per 60 seconds",
        429,
        headers={"Retry-After": str(retry_after_seconds)},
        retry_after_ms=exc.retry_after_ms,
        retry_after_seconds=retry_after_seconds,
        limit=VOLCANO_ASSET_CREATE_QPM,
        window_seconds=VOLCANO_ASSET_CREATE_WINDOW_SECONDS,
    )
