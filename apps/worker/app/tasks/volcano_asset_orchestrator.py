"""Background Volcano AIGC asset operations."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time  # noqa: F401 - compatibility monkeypatch surface
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from lumen_core.models import AuditLog
from lumen_core.video_providers import (
    VideoProviderDefinition,
    parse_video_provider_config_json,
    video_provider_binding_fingerprint,
)
from lumen_core.volcano_asset_media import VolcanoAssetMediaError
from lumen_core.volcano_assets import (
    VOLCANO_ASSET_MAX_ASSETS,  # noqa: F401 - compatibility runtime export
    VOLCANO_ASSET_MAX_GROUPS,  # noqa: F401 - compatibility runtime export
    VOLCANO_ASSET_OPERATION_TTL_SECONDS,
    VolcanoAssetClient as _CoreVolcanoAssetClient,
    VolcanoAssetCreateRateLimited,
    VolcanoAssetQuotaExceeded,  # noqa: F401 - compatibility runtime export
    VolcanoAssetQuotaKey,
    VolcanoAssetRedisUnavailable,
    VolcanoAssetServiceError,
    acquire_volcano_create_rate_limit,  # noqa: F401 - compatibility runtime export
    normalize_asset,  # noqa: F401 - compatibility runtime export
    normalize_asset_group,
    normalize_asset_group_list,  # noqa: F401 - compatibility runtime export
    normalize_asset_list,
    normalize_volcano_asset_name,  # noqa: F401 - compatibility runtime export
    release_volcano_asset_quota,
    reserve_volcano_asset_quota,  # noqa: F401 - compatibility runtime export
    volcano_asset_operation_key,
    volcano_asset_quota_key,  # noqa: F401 - compatibility runtime export
    volcano_asset_quota_scope,
)
from .. import runtime_settings
from ..db import SessionLocal
from ..provider_pool import resolve_provider_proxy_url
from ..storage_writes import StorageWriteCoordinator
from . import (
    volcano_asset_actions as _action_parts,
)
from . import (
    volcano_asset_create as _create_parts,
)
from . import (
    volcano_asset_dispatch as _dispatch_parts,
)
from . import (
    volcano_asset_operation_lease as _lease_parts,
)
from . import (
    volcano_asset_reconciliation as _reconciliation_parts,
)
from .volcano_asset_runtime import VolcanoAssetRuntimeContext
from .volcano_asset_inventory import (
    asset_matches_operation as _asset_matches_operation,
    explicit_asset_total as _explicit_asset_total,
)
from .volcano_asset_source_media import (
    ensure_reference_token as _ensure_reference_token,  # noqa: F401
)
from .volcano_asset_source_media import (
    normalized_source_url as _normalized_source_url,
)
from .volcano_assets_parts.receipts import (
    AIGC_GROUP_TYPE as _AIGC_GROUP_TYPE,
    LEGACY_SUCCESS_RECEIPT_EVENT as _LEGACY_SUCCESS_RECEIPT_EVENT,  # noqa: F401
    RECEIPT_BINDING_FIELDS as _RECEIPT_BINDING_FIELDS,  # noqa: F401
    SUCCESS_RECEIPT_EVENT as _SUCCESS_RECEIPT_EVENT,  # noqa: F401
    operation_has_value as _operation_has_value,
    read_success_receipt as _read_success_receipt_impl,
    receipt_asset as _receipt_asset,  # noqa: F401
    receipt_binding_matches as _receipt_binding_matches,  # noqa: F401
    receipt_fence_matches as _receipt_fence_matches,  # noqa: F401
    receipt_group as _receipt_group,  # noqa: F401
    receipt_result as _receipt_result,  # noqa: F401
    success_receipt_details as _success_receipt_details,  # noqa: F401
    validated_receipt_result as _validated_receipt_result,  # noqa: F401
    write_success_receipt as _write_success_receipt_impl,
)

logger = logging.getLogger(__name__)


class VolcanoAssetClient(_CoreVolcanoAssetClient):
    def __init__(self, provider: Any) -> None:
        super().__init__(provider, proxy_resolver=resolve_provider_proxy_url)


_RUNTIME_DEPENDENCY_FACTORIES = MappingProxyType(
    {
        "VOLCANO_ASSET_MAX_ASSETS": lambda: VOLCANO_ASSET_MAX_ASSETS,
        "VOLCANO_ASSET_OPERATION_TTL_SECONDS": (
            lambda: VOLCANO_ASSET_OPERATION_TTL_SECONDS
        ),
        "VolcanoAssetClient": lambda: VolcanoAssetClient,
        "VolcanoAssetCreateRateLimited": lambda: VolcanoAssetCreateRateLimited,
        "VolcanoAssetMediaError": lambda: VolcanoAssetMediaError,
        "VolcanoAssetQuotaExceeded": lambda: VolcanoAssetQuotaExceeded,
        "VolcanoAssetRedisUnavailable": lambda: VolcanoAssetRedisUnavailable,
        "VolcanoAssetServiceError": lambda: VolcanoAssetServiceError,
        "_LeaseLostError": lambda: _LeaseLostError,
        "_ALLOCATE_OPERATION_FENCING_SCRIPT": (
            lambda: _ALLOCATE_OPERATION_FENCING_SCRIPT
        ),
        "_CONFIRM_OPERATION_FENCE_SCRIPT": lambda: _CONFIRM_OPERATION_FENCE_SCRIPT,
        "_JOB_NAME": lambda: _JOB_NAME,
        "_OPERATION_LOCK_RENEW_INTERVAL_SECONDS": (
            lambda: _OPERATION_LOCK_RENEW_INTERVAL_SECONDS
        ),
        "_OPERATION_LOCK_TTL_SECONDS": lambda: _OPERATION_LOCK_TTL_SECONDS,
        "_OperationFailure": lambda: _OperationFailure,
        "_RELEASE_OPERATION_LOCK_SCRIPT": lambda: _RELEASE_OPERATION_LOCK_SCRIPT,
        "_RENEW_OPERATION_LOCK_SCRIPT": lambda: _RENEW_OPERATION_LOCK_SCRIPT,
        "_SET_FENCED_OPERATION_SCRIPT": lambda: _SET_FENCED_OPERATION_SCRIPT,
        "_SUPPORTED_ACTIONS": lambda: _SUPPORTED_ACTIONS,
        "_SuccessPersistenceError": lambda: _SuccessPersistenceError,
        "_ambiguous_create_asset_failure": lambda: _ambiguous_create_asset_failure,
        "_ambiguous_create_group_failure": lambda: _ambiguous_create_group_failure,
        "_asset_matches_operation": lambda: _asset_matches_operation,
        "_asset_target_reached": lambda: _asset_target_reached,
        "_complete_operation": lambda: _complete_operation,
        "_confirm_operation_lock": lambda: _confirm_operation_lock,
        "_defer_for_rate_limit": lambda: _defer_for_rate_limit,
        "_defer_for_submit_slot": lambda: _defer_for_submit_slot,
        "_delete_asset_result": lambda: _delete_asset_result,
        "_delete_group_result": lambda: _delete_group_result,
        "_get_operation": lambda: _get_operation,
        "_get_scoped_asset": lambda: _get_scoped_asset,
        "_get_scoped_group": lambda: _get_scoped_group,
        "_group_target_reached": lambda: _group_target_reached,
        "_explicit_asset_total": lambda: _explicit_asset_total,
        "_is_not_found": lambda: _is_not_found,
        "_list_group_asset_ids_best_effort": lambda: _list_group_asset_ids_best_effort,
        "_media_failure": lambda: _media_failure,
        "_operation_contract_failure": lambda: _operation_contract_failure,
        "_operation_deleted_asset_ids": lambda: _operation_deleted_asset_ids,
        "_operation_has_value": lambda: _operation_has_value,
        "_parse_operation_time": lambda: _parse_operation_time,
        "_persist_terminal_operation": lambda: _persist_terminal_operation,
        "_process_create_asset": lambda: _process_create_asset,
        "_process_create_group": lambda: _process_create_group,
        "_process_delete_asset": lambda: _process_delete_asset,
        "_process_delete_group": lambda: _process_delete_group,
        "_process_management_action": lambda: _process_management_action,
        "_process_update_asset": lambda: _process_update_asset,
        "_process_update_group": lambda: _process_update_group,
        "_provider_for_operation": lambda: _provider_for_operation,
        "_process_locked": lambda: _process_locked,
        "_read_success_receipt": lambda: _read_success_receipt,
        "_reconcile_ambiguous_submit": lambda: _reconcile_ambiguous_submit,
        "_reconcile_update_asset": lambda: _reconcile_update_asset,
        "_reconcile_update_group": lambda: _reconcile_update_group,
        "_record_operation_failure": lambda: _record_operation_failure,
        "_recover_unconfirmed_delivery": lambda: _recover_unconfirmed_delivery,
        "_release_quota_best_effort": lambda: _release_quota_best_effort,
        "_require_asset_scope": lambda: _require_asset_scope,
        "_require_group_scope": lambda: _require_group_scope,
        "_resource_is_deleted": lambda: _resource_is_deleted,
        "_retry_redis_call": lambda: _retry_redis_call,
        "_service_failure": lambda: _service_failure,
        "_snapshot_group_asset_ids": lambda: _snapshot_group_asset_ids,
        "_source_url_for_submit": lambda: _source_url_for_submit,
        "_utc_iso": lambda: _utc_iso,
        "_write_audit": lambda: _write_audit,
        "acquire_volcano_create_rate_limit": lambda: acquire_volcano_create_rate_limit,
        "normalize_asset": lambda: normalize_asset,
        "normalize_asset_list": lambda: normalize_asset_list,
        "normalize_volcano_asset_name": lambda: normalize_volcano_asset_name,
        "reserve_volcano_asset_quota": lambda: reserve_volcano_asset_quota,
        "video_provider_binding_fingerprint": lambda: (
            video_provider_binding_fingerprint
        ),
        "volcano_asset_operation_key": lambda: volcano_asset_operation_key,
        "volcano_asset_quota_key": lambda: volcano_asset_quota_key,
        "volcano_asset_quota_scope": lambda: volcano_asset_quota_scope,
    }
)


def _resolve_runtime_dependency(name: str) -> Any:
    return _RUNTIME_DEPENDENCY_FACTORIES[name]()


_RUNTIME_CONTEXT = VolcanoAssetRuntimeContext(_resolve_runtime_dependency)
_action_parts.install_runtime(_RUNTIME_CONTEXT)
_create_parts.install_runtime(_RUNTIME_CONTEXT)
_dispatch_parts.install_runtime(_RUNTIME_CONTEXT)
_lease_parts.install_runtime(_RUNTIME_CONTEXT)
_reconciliation_parts.install_runtime(_RUNTIME_CONTEXT)

_JOB_NAME = _lease_parts._JOB_NAME
_OPERATION_LOCK_TTL_SECONDS = _lease_parts._OPERATION_LOCK_TTL_SECONDS
_OPERATION_LOCK_RENEW_INTERVAL_SECONDS = (
    _lease_parts._OPERATION_LOCK_RENEW_INTERVAL_SECONDS
)
_RELEASE_OPERATION_LOCK_SCRIPT = _lease_parts._RELEASE_OPERATION_LOCK_SCRIPT
_RENEW_OPERATION_LOCK_SCRIPT = _lease_parts._RENEW_OPERATION_LOCK_SCRIPT
_ALLOCATE_OPERATION_FENCING_SCRIPT = _lease_parts._ALLOCATE_OPERATION_FENCING_SCRIPT
_CONFIRM_OPERATION_FENCE_SCRIPT = _lease_parts._CONFIRM_OPERATION_FENCE_SCRIPT
_SET_FENCED_OPERATION_SCRIPT = _lease_parts._SET_FENCED_OPERATION_SCRIPT
_REDIS_RETRY_ATTEMPTS = 3
_REDIS_RETRY_BASE_DELAY_SECONDS = 0.02
_SUPPORTED_ACTIONS = frozenset(
    {
        "create_group",
        "update_group",
        "delete_group",
        "create_asset",
        "update_asset",
        "delete_asset",
    }
)


class _OperationFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class _SuccessPersistenceError(RuntimeError):
    """The upstream asset exists but durable local success state is unavailable."""


class _LeaseLostError(RuntimeError):
    """The operation lease is no longer owned by this worker."""


_OperationFence = _lease_parts._OperationFence
_OperationPersistence = _lease_parts._OperationPersistence


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _retry_redis_call(
    call: Callable[[], Awaitable[Any]],
) -> Any:
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


async def _get_operation(redis: Any, operation_id: str) -> dict[str, Any] | None:
    raw = await _retry_redis_call(
        lambda: redis.get(volcano_asset_operation_key(operation_id))
    )
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def _set_operation(redis: Any, operation: dict[str, Any]) -> None:
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


async def _update_operation(
    redis: Any,
    operation: dict[str, Any],
    **changes: Any,
) -> None:
    operation.update(changes)
    operation["updated_at"] = _utc_iso()
    await _set_operation(redis, operation)


_operation_fencing_key = _lease_parts._operation_fencing_key
_allocate_operation_fencing = _lease_parts._allocate_operation_fencing
_confirm_operation_fence = _lease_parts._confirm_operation_fence
_set_fenced_operation = _lease_parts._set_fenced_operation


async def _provider_for_operation(
    operation: dict[str, Any],
) -> VideoProviderDefinition:
    raw_video = await runtime_settings.resolve("video.providers")
    raw_shared = await runtime_settings.resolve("providers")
    providers, _proxies, errors = parse_video_provider_config_json(
        raw_video,
        shared_provider_raw=raw_shared,
    )
    if errors:
        raise _OperationFailure(
            "video_provider_config_invalid",
            "video provider configuration is invalid",
            retryable=True,
        )
    selected = next(
        (
            provider
            for provider in providers
            if provider.name == operation.get("provider_name")
        ),
        None,
    )
    if (
        selected is None
        or selected.kind != "volcano"
        or selected.project_name != operation.get("project_name")
        or selected.region != operation.get("region")
        or not selected.asset_management_ready
        or not selected.supports(str(operation.get("model") or ""), "reference")
    ):
        raise _OperationFailure(
            "video_asset_provider_snapshot_unavailable",
            "the queued Volcano provider configuration is no longer available",
            retryable=True,
        )
    expected_binding = str(operation.get("provider_binding") or "")
    if (
        expected_binding
        and video_provider_binding_fingerprint(selected) != expected_binding
    ):
        raise _OperationFailure(
            "video_asset_provider_snapshot_unavailable",
            "the queued Volcano provider credentials or route have changed",
            retryable=True,
        )
    return selected


def _require_group_scope(
    raw: Any,
    provider: VideoProviderDefinition,
    group_id: str,
) -> None:
    group = normalize_asset_group(
        raw,
        project_name=provider.project_name,
        fallback={
            "id": group_id,
            "project_name": provider.project_name,
        },
    )
    if (
        group.get("id") != group_id
        or str(group.get("group_type") or "").upper() != _AIGC_GROUP_TYPE
        or group.get("project_name") != provider.project_name
    ):
        raise _OperationFailure(
            "volcano_asset_scope_mismatch",
            "the asset group is outside the configured AIGC project",
            retryable=False,
        )


async def _write_audit(
    operation: dict[str, Any],
    *,
    event_type: str,
    details: dict[str, Any],
) -> None:
    async with SessionLocal() as session:
        session.add(
            AuditLog(
                user_id=str(operation.get("user_id") or "") or None,
                event_type=event_type,
                actor_email_hash=operation.get("actor_email_hash"),
                actor_ip_hash=operation.get("actor_ip_hash"),
                details=details,
            )
        )
        await session.commit()


async def _read_success_receipt(
    operation: dict[str, Any],
    *,
    fence: _OperationFence,
) -> dict[str, Any] | None:
    return await _read_success_receipt_impl(
        operation,
        fence=fence,
        session_factory=SessionLocal,
    )


async def _write_success_receipt(
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    fence: _OperationFence,
) -> None:
    await _write_success_receipt_impl(
        operation,
        result,
        fence=fence,
        session_factory=SessionLocal,
    )


def _service_failure(exc: VolcanoAssetServiceError) -> _OperationFailure:
    retryable = exc.status_code in {429, 502, 503, 504}
    retry_after_seconds = (
        max(1, math.ceil(exc.retry_after_ms / 1000))
        if exc.retry_after_ms is not None
        else None
    )
    return _OperationFailure(
        exc.code,
        exc.message,
        retryable=retryable,
        retry_after_seconds=retry_after_seconds,
    )


def _media_failure(exc: VolcanoAssetMediaError) -> _OperationFailure:
    return _OperationFailure(
        exc.code,
        exc.message,
        retryable=exc.status_code >= 500,
    )


def _source_url_is_fresh(operation: dict[str, Any]) -> bool:
    source_url = str(operation.get("source_url") or "")
    created_at = str(operation.get("source_url_created_at") or "")
    if not source_url or not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created < timedelta(hours=23)


async def _source_url_for_submit(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    storage_writes: StorageWriteCoordinator | None = None,
) -> str:
    if _source_url_is_fresh(operation):
        return str(operation["source_url"])
    if storage_writes is None:
        public_url, variant_kind = await _normalized_source_url(operation)
    else:
        public_url, variant_kind = await _normalized_source_url(
            operation,
            storage_writes=storage_writes,
        )
    await persistence.update(
        operation,
        source_url=public_url,
        source_variant=variant_kind,
        source_url_created_at=_utc_iso(),
        submit_started_at=None,
        preview_url=operation.get("preview_url"),
    )
    return public_url


_scan_operation_assets = _reconciliation_parts._scan_operation_assets
_find_existing_submitted_asset = _reconciliation_parts._find_existing_submitted_asset
_snapshot_group_asset_ids = _reconciliation_parts._snapshot_group_asset_ids
_reconcile_ambiguous_submit = _reconciliation_parts._reconcile_ambiguous_submit


async def _persist_terminal_operation(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    *,
    status: str,
    result: dict[str, Any] | None,
    error: dict[str, Any] | None,
    retryable: bool,
    retry_after_seconds: int | None,
    receipt_exists: bool = False,
) -> None:
    persistence.bind(operation)
    if status == "succeeded" and not receipt_exists:
        assert result is not None
        await persistence.confirm()
        try:
            await _write_success_receipt(
                operation,
                result,
                fence=persistence.fence,
            )
        except Exception as exc:  # noqa: BLE001
            raise _SuccessPersistenceError(
                "could not persist Volcano asset ownership receipt"
            ) from exc
    candidate = dict(operation)
    if status == "succeeded":
        candidate.pop("source_url", None)
        candidate.pop("source_url_created_at", None)
        candidate.pop("baseline_asset_ids", None)
        candidate.pop("submit_outcome_uncertain", None)
    candidate.update(
        {
            "status": status,
            "progress_stage": "completed" if status == "succeeded" else "failed",
            "retryable": retryable,
            "retry_after_seconds": retry_after_seconds,
            "result": result,
            "error": error,
            "completed_at": _utc_iso(),
            "updated_at": _utc_iso(),
            **persistence.fence.details(),
        }
    )
    try:
        await persistence.replace(
            operation,
            candidate,
            terminal=True,
        )
    except VolcanoAssetRedisUnavailable as exc:
        if status == "succeeded":
            raise _SuccessPersistenceError(
                "could not persist completed Volcano asset operation"
            ) from exc
        raise


async def _release_quota_best_effort(
    redis: Any,
    quota_key: VolcanoAssetQuotaKey,
    operation_id: str,
    *,
    resource: str = "assets",
) -> bool:
    try:
        await release_volcano_asset_quota(
            redis,
            quota_key,
            resource=resource,
            operation_id=operation_id,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "video_asset.quota_release_failed operation_id=%s resource=%s",
            operation_id,
            resource,
            exc_info=True,
        )
    return False


_renew_operation_lock = _lease_parts._renew_operation_lock
_operation_lock_heartbeat = _lease_parts._operation_lock_heartbeat
_confirm_operation_lock = _lease_parts._confirm_operation_lock


async def _defer_create_delivery(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    *,
    progress_stage: str,
    error_code: str,
    error_message: str,
    retry_after_seconds: int,
) -> None:
    redis = persistence.redis
    retry_after_seconds = max(1, int(retry_after_seconds))
    delivery_generation = max(0, int(operation.get("delivery_generation") or 0)) + 1
    retry_not_before = (
        datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
    ).isoformat()
    await persistence.update(
        operation,
        status="queued",
        progress_stage=progress_stage,
        delivery_generation=delivery_generation,
        delivery_enqueued=False,
        retry_not_before=retry_not_before,
        retryable=True,
        retry_after_seconds=retry_after_seconds,
        error={
            "code": error_code,
            "message": error_message,
            "retryable": True,
            "retry_after_seconds": retry_after_seconds,
        },
    )
    await _retry_redis_call(
        lambda: redis.enqueue_job(
            _JOB_NAME,
            str(operation["id"]),
            max(1, int(operation.get("attempt") or 1)),
            delivery_generation,
            _defer_by=timedelta(seconds=retry_after_seconds),
            _job_id=(
                f"volcano-asset:{operation['id']}:"
                f"{operation.get('attempt', 1)}:{delivery_generation}"
            ),
        )
    )
    await persistence.update(
        operation,
        delivery_enqueued=True,
    )


async def _defer_for_rate_limit(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    exc: VolcanoAssetCreateRateLimited,
) -> None:
    await _defer_create_delivery(
        persistence,
        operation,
        progress_stage="waiting_rate_limit",
        error_code="volcano_asset_create_rate_limited",
        error_message="CreateAsset is waiting for the 3 per 60 seconds limit",
        retry_after_seconds=max(1, math.ceil(exc.retry_after_ms / 1000)),
    )


async def _defer_for_submit_slot(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    *,
    retry_after_seconds: int,
) -> None:
    await _defer_create_delivery(
        persistence,
        operation,
        progress_stage="waiting_submit_slot",
        error_code="volcano_asset_create_queued",
        error_message="CreateAsset is queued behind another active submission",
        retry_after_seconds=retry_after_seconds,
    )


async def _recover_unconfirmed_delivery(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
) -> bool:
    redis = persistence.redis
    if (
        operation.get("status") != "queued"
        or operation.get("progress_stage")
        not in {"waiting_rate_limit", "waiting_submit_slot"}
        or operation.get("delivery_enqueued") is not False
    ):
        return False
    retry_after_seconds = 1
    raw_not_before = str(operation.get("retry_not_before") or "")
    try:
        not_before = datetime.fromisoformat(raw_not_before)
    except ValueError:
        not_before = None
    if not_before is not None:
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        retry_after_seconds = max(
            1,
            math.ceil((not_before - datetime.now(timezone.utc)).total_seconds()),
        )
    delivery_generation = max(
        0,
        int(operation.get("delivery_generation") or 0),
    )
    await _retry_redis_call(
        lambda: redis.enqueue_job(
            _JOB_NAME,
            str(operation["id"]),
            max(1, int(operation.get("attempt") or 1)),
            delivery_generation,
            _defer_by=timedelta(seconds=retry_after_seconds),
            _job_id=(
                f"volcano-asset:{operation['id']}:"
                f"{operation.get('attempt', 1)}:{delivery_generation}"
            ),
        )
    )
    await persistence.update(
        operation,
        delivery_enqueued=True,
    )
    return True


async def _complete_operation(
    persistence: _OperationPersistence,
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    provider: VideoProviderDefinition | None = None,
    receipt_exists: bool = False,
) -> dict[str, Any]:
    if provider is not None:
        operation["provider_name"] = provider.name
        operation["region"] = provider.region
        operation["provider_binding"] = video_provider_binding_fingerprint(provider)
    await _persist_terminal_operation(
        persistence,
        operation,
        status="succeeded",
        result=result,
        error=None,
        retryable=False,
        retry_after_seconds=None,
        receipt_exists=receipt_exists,
    )
    if provider is not None:
        action = str(operation.get("action") or "")
        event_type = {
            "create_group": "video_asset_group.create",
            "update_group": "video_asset_group.update",
            "delete_group": "video_asset_group.delete",
            "create_asset": "video_asset.create",
            "update_asset": "video_asset.update",
            "delete_asset": "video_asset.delete",
        }.get(action)
        details = {
            "operation_id": operation.get("id"),
            "action": action,
            "model": operation.get("model"),
            "provider_name": provider.name,
            "project_name": provider.project_name,
            **persistence.fence.details(),
        }
        if action.endswith("_group"):
            details["group_id"] = result.get("id") or operation.get("group_id")
        else:
            details["asset_id"] = result.get("id") or operation.get("asset_id")
            details["group_id"] = result.get("group_id") or operation.get("group_id")
        if action == "create_asset":
            details.update(
                {
                    "asset_type": operation.get("asset_type"),
                    "local_source_id": operation.get("local_source_id"),
                }
            )
        elif action == "update_group":
            details["changed_fields"] = [
                key
                for key in ("name", "description")
                if _operation_has_value(operation, key)
            ]
        elif action == "update_asset":
            details["changed_fields"] = ["name"]
        elif action in {"delete_group", "delete_asset"}:
            details["already_deleted"] = bool(result.get("already_deleted"))
        try:
            if event_type is not None:
                await _write_audit(
                    operation,
                    event_type=event_type,
                    details=details,
                )
        except Exception:  # noqa: BLE001
            logger.error(
                "video_asset.success_audit_failed operation_id=%s",
                operation.get("id"),
                exc_info=True,
            )
    return {
        "status": "succeeded",
        "operation_id": str(operation.get("id") or ""),
        "result": result,
    }


_is_not_found = _action_parts._is_not_found
_parse_operation_time = _action_parts._parse_operation_time
_require_asset_scope = _action_parts._require_asset_scope
_get_scoped_group = _action_parts._get_scoped_group
_get_scoped_asset = _action_parts._get_scoped_asset
_group_target_reached = _action_parts._group_target_reached
_asset_target_reached = _action_parts._asset_target_reached
_reconcile_update_group = _action_parts._reconcile_update_group
_reconcile_update_asset = _action_parts._reconcile_update_asset
_resource_is_deleted = _action_parts._resource_is_deleted
_operation_deleted_asset_ids = _action_parts._operation_deleted_asset_ids
_list_group_asset_ids_best_effort = _action_parts._list_group_asset_ids_best_effort
_delete_group_result = _action_parts._delete_group_result
_delete_asset_result = _action_parts._delete_asset_result
_ambiguous_create_group_failure = _action_parts._ambiguous_create_group_failure
_ambiguous_create_asset_failure = _action_parts._ambiguous_create_asset_failure
_process_create_group = _action_parts._process_create_group
_process_update_group = _action_parts._process_update_group
_process_delete_group = _action_parts._process_delete_group
_process_update_asset = _action_parts._process_update_asset
_process_delete_asset = _action_parts._process_delete_asset
_record_operation_failure = _action_parts._record_operation_failure
_process_management_action = _action_parts._process_management_action
_operation_contract_failure = _action_parts._operation_contract_failure
_process_create_asset = _create_parts._process_create_asset


_process_locked = _dispatch_parts._process_locked


_schedule_lock_recovery = _lease_parts._schedule_lock_recovery
process_volcano_asset_operation = _lease_parts.process_volcano_asset_operation


__all__ = ["process_volcano_asset_operation"]
