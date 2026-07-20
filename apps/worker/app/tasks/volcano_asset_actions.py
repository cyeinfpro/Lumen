"""Management actions for Volcano asset operations."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from lumen_core.video_providers import VideoProviderDefinition
from lumen_core.volcano_assets import (
    VOLCANO_ASSET_MAX_GROUPS,
    VolcanoAssetQuotaExceeded,
    VolcanoAssetServiceError,
    normalize_asset,
    normalize_asset_group,
    normalize_asset_group_list,
    normalize_asset_list,
    volcano_asset_quota_key,
)

from .volcano_asset_runtime import (
    VolcanoAssetRuntimeContext,
    VolcanoAssetRuntimeSlot,
    VolcanoAssetRuntimeView,
)

logger = logging.getLogger(__name__)

_AIGC_GROUP_TYPE = "AIGC"
_AMBIGUOUS_RECONCILE_ATTEMPTS = 3
_RUNTIME = VolcanoAssetRuntimeSlot(
    owner=__name__,
    dependencies=frozenset(
        {
            "VolcanoAssetClient",
            "VolcanoAssetRedisUnavailable",
            "_LeaseLostError",
            "_OperationFailure",
            "_SUPPORTED_ACTIONS",
            "_SuccessPersistenceError",
            "_ambiguous_create_group_failure",
            "_asset_target_reached",
            "_complete_operation",
            "_confirm_operation_lock",
            "_delete_asset_result",
            "_delete_group_result",
            "_get_scoped_asset",
            "_get_scoped_group",
            "_group_target_reached",
            "_is_not_found",
            "_list_group_asset_ids_best_effort",
            "_operation_deleted_asset_ids",
            "_operation_has_value",
            "_persist_terminal_operation",
            "_process_create_group",
            "_process_delete_asset",
            "_process_delete_group",
            "_process_update_asset",
            "_process_update_group",
            "_provider_for_operation",
            "_reconcile_update_asset",
            "_reconcile_update_group",
            "_record_operation_failure",
            "_release_quota_best_effort",
            "_require_asset_scope",
            "_require_group_scope",
            "_resource_is_deleted",
            "_service_failure",
            "_utc_iso",
            "_write_audit",
            "reserve_volcano_asset_quota",
        }
    ),
)


def install_runtime(context: VolcanoAssetRuntimeContext) -> None:
    _RUNTIME.install(context)


def _runtime() -> VolcanoAssetRuntimeView:
    return _RUNTIME.get()


def _is_not_found(exc: VolcanoAssetServiceError) -> bool:
    return exc.code == "volcano_asset_not_found" or exc.status_code in {404, 410}


def _parse_operation_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None:
        return _parse_operation_time(numeric)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_asset_scope(
    asset: dict[str, Any],
    provider: VideoProviderDefinition,
    asset_id: str,
) -> None:
    if (
        asset.get("id") != asset_id
        or not asset.get("group_id")
        or asset.get("project_name") != provider.project_name
    ):
        raise _runtime()._OperationFailure(
            "volcano_asset_scope_mismatch",
            "the asset is outside the configured AIGC project",
            retryable=False,
        )


async def _get_scoped_group(
    client: Any,
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
    _runtime()._require_group_scope(group, provider, group_id)
    return group


async def _get_scoped_asset(
    client: Any,
    provider: VideoProviderDefinition,
    asset_id: str,
) -> dict[str, Any]:
    runtime = _runtime()
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
    runtime._require_asset_scope(asset, provider, asset_id)
    await runtime._get_scoped_group(client, provider, str(asset["group_id"]))
    return asset


def _group_target_reached(
    operation: dict[str, Any],
    group: dict[str, Any],
) -> bool:
    runtime = _runtime()
    if runtime._operation_has_value(operation, "name") and str(
        group.get("name") or ""
    ) != str(operation.get("name") or ""):
        return False
    if runtime._operation_has_value(operation, "description") and str(
        group.get("description") or ""
    ) != str(operation.get("description") or ""):
        return False
    return True


def _asset_target_reached(
    operation: dict[str, Any],
    asset: dict[str, Any],
) -> bool:
    return str(asset.get("name") or "") == str(operation.get("name") or "")


async def _reconcile_update_group(
    client: Any,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _runtime()
    try:
        group = await runtime._get_scoped_group(
            client,
            provider,
            str(operation.get("group_id") or ""),
        )
    except VolcanoAssetServiceError:
        return None
    return group if runtime._group_target_reached(operation, group) else None


async def _reconcile_update_asset(
    client: Any,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> dict[str, Any] | None:
    runtime = _runtime()
    try:
        asset = await runtime._get_scoped_asset(
            client,
            provider,
            str(operation.get("asset_id") or ""),
        )
    except VolcanoAssetServiceError:
        return None
    return asset if runtime._asset_target_reached(operation, asset) else None


async def _resource_is_deleted(
    read: Callable[[], Awaitable[dict[str, Any]]],
) -> bool:
    try:
        await read()
    except VolcanoAssetServiceError as exc:
        if _runtime()._is_not_found(exc):
            return True
        raise
    return False


def _operation_deleted_asset_ids(operation: dict[str, Any]) -> list[str]:
    raw_ids = operation.get("deleted_asset_ids")
    if not isinstance(raw_ids, list):
        return []
    return list(
        dict.fromkeys(
            str(asset_id)
            for asset_id in raw_ids
            if asset_id is not None and str(asset_id)
        )
    )


async def _list_group_asset_ids_best_effort(
    client: Any,
    provider: VideoProviderDefinition,
    operation: dict[str, Any],
) -> list[str]:
    runtime = _runtime()
    group_id = str(operation.get("group_id") or "")
    existing_ids = runtime._operation_deleted_asset_ids(operation)
    for attempt in range(_AMBIGUOUS_RECONCILE_ATTEMPTS):
        if attempt:
            delay = min(2.0, 0.25 * (2 ** (attempt - 1)))
            await asyncio.sleep(delay + random.uniform(0, delay))
        try:
            listed = normalize_asset_list(
                await client.request(
                    "ListAssets",
                    {
                        "ProjectName": provider.project_name,
                        "Filter": {
                            "GroupType": _AIGC_GROUP_TYPE,
                            "GroupIds": [group_id],
                        },
                        "PageNumber": 1,
                        "PageSize": 100,
                    },
                ),
                project_name=provider.project_name,
                page_number=1,
                page_size=100,
            )
        except VolcanoAssetServiceError:
            continue
        listed_ids = [
            str(asset.get("id"))
            for asset in listed["items"]
            if asset.get("id")
            and asset.get("group_id") == group_id
            and asset.get("project_name") == provider.project_name
        ]
        return list(dict.fromkeys([*existing_ids, *listed_ids]))
    logger.warning(
        "video_asset.group_delete_asset_inventory_unavailable operation_id=%s",
        operation.get("id"),
    )
    return existing_ids


def _delete_group_result(
    group_id: str,
    deleted_asset_ids: list[str],
    *,
    already_deleted: bool,
) -> dict[str, Any]:
    return {
        "id": group_id,
        "deleted": True,
        "resource_type": "group",
        "group_id": group_id,
        "deleted_asset_ids": list(deleted_asset_ids),
        "already_deleted": already_deleted,
        "cascade_assets": True,
    }


def _delete_asset_result(
    asset_id: str,
    *,
    group_id: str | None,
    already_deleted: bool,
) -> dict[str, Any]:
    return {
        "id": asset_id,
        "deleted": True,
        "resource_type": "asset",
        "group_id": group_id,
        "asset_id": asset_id,
        "deleted_asset_ids": [asset_id],
        "already_deleted": already_deleted,
        "cascade_assets": False,
    }


def _ambiguous_create_group_failure() -> Exception:
    return _runtime()._OperationFailure(
        "volcano_asset_create_group_reconcile_ambiguous",
        "could not safely identify the submitted Volcano asset group",
        retryable=True,
        retry_after_seconds=10,
    )


def _ambiguous_create_asset_failure() -> Exception:
    return _runtime()._OperationFailure(
        "volcano_asset_create_reconcile_ambiguous",
        "could not uniquely reconcile the submitted Volcano asset",
        retryable=True,
        retry_after_seconds=10,
    )


async def _process_create_group(
    redis: Any,
    operation: dict[str, Any],
    provider: VideoProviderDefinition,
    client: Any,
    *,
    persistence: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    operation_id = str(operation.get("id") or "")
    prior_submit_is_uncertain = bool(operation.get("submit_outcome_uncertain")) or (
        bool(operation.get("submit_started_at"))
        and "submit_outcome_uncertain" not in operation
    )
    if prior_submit_is_uncertain:
        await persistence.update(
            operation,
            progress_stage="submit_outcome_uncertain",
        )
        raise runtime._ambiguous_create_group_failure()

    await persistence.update(
        operation,
        progress_stage="checking_quota",
    )
    listed = normalize_asset_group_list(
        await client.request(
            "ListAssetGroups",
            {
                "ProjectName": provider.project_name,
                "Filter": {"GroupType": _AIGC_GROUP_TYPE},
                "PageNumber": 1,
                "PageSize": 1,
            },
        ),
        project_name=provider.project_name,
        page_number=1,
        page_size=1,
    )
    quota_key = volcano_asset_quota_key(provider)
    reservation_acquired = False
    await runtime.reserve_volcano_asset_quota(
        redis,
        quota_key,
        resource="asset_groups",
        operation_id=operation_id,
        upstream_total=listed["total_count"],
        limit=VOLCANO_ASSET_MAX_GROUPS,
        now_ms=int(time.time() * 1000),
    )
    reservation_acquired = True
    try:
        await runtime._confirm_operation_lock(persistence)
        await persistence.update(
            operation,
            progress_stage="submitting",
            submit_started_at=runtime._utc_iso(),
            submit_outcome_uncertain=True,
        )
        try:
            raw_group = await client.request(
                "CreateAssetGroup",
                {
                    "Name": str(operation.get("name") or ""),
                    "Description": str(operation.get("description") or ""),
                    "GroupType": _AIGC_GROUP_TYPE,
                    "ProjectName": provider.project_name,
                },
            )
        except VolcanoAssetServiceError as exc:
            mapped_failure = runtime._service_failure(exc)
            if exc.status_code in {502, 503, 504}:
                raise runtime._ambiguous_create_group_failure() from exc
            await persistence.update(
                operation,
                submit_started_at=None,
                submit_outcome_uncertain=False,
            )
            raise mapped_failure from exc

        group = normalize_asset_group(
            raw_group,
            project_name=provider.project_name,
            fallback={
                "name": str(operation.get("name") or ""),
                "description": str(operation.get("description") or ""),
                "group_type": _AIGC_GROUP_TYPE,
                "project_name": provider.project_name,
            },
        )
        valid_group = (
            bool(group.get("id"))
            and str(group.get("group_type") or "").upper() == _AIGC_GROUP_TYPE
            and group.get("project_name") == provider.project_name
            and runtime._group_target_reached(operation, group)
        )
        if not valid_group:
            raise runtime._ambiguous_create_group_failure()
        return await runtime._complete_operation(
            persistence,
            operation,
            group,
            provider=provider,
        )
    finally:
        if reservation_acquired:
            await runtime._release_quota_best_effort(
                redis,
                quota_key,
                operation_id,
                resource="asset_groups",
            )


async def _process_update_group(
    redis: Any,
    operation: dict[str, Any],
    provider: VideoProviderDefinition,
    client: Any,
    *,
    persistence: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    group_id = str(operation.get("group_id") or "")
    current = await runtime._get_scoped_group(client, provider, group_id)
    if runtime._group_target_reached(operation, current):
        return await runtime._complete_operation(
            persistence,
            operation,
            current,
            provider=provider,
        )
    payload: dict[str, Any] = {
        "Id": group_id,
        "ProjectName": provider.project_name,
    }
    fallback = dict(current)
    if runtime._operation_has_value(operation, "name"):
        payload["Name"] = str(operation.get("name") or "")
        fallback["name"] = payload["Name"]
        fallback["title"] = payload["Name"]
    if runtime._operation_has_value(operation, "description"):
        payload["Description"] = str(operation.get("description") or "")
        fallback["description"] = payload["Description"]

    await runtime._confirm_operation_lock(persistence)
    await persistence.update(
        operation,
        progress_stage="submitting",
        submit_started_at=runtime._utc_iso(),
    )
    try:
        raw_group = await client.request("UpdateAssetGroup", payload)
    except VolcanoAssetServiceError as exc:
        if runtime._service_failure(exc).retryable:
            recovered = await runtime._reconcile_update_group(
                client,
                provider,
                operation,
            )
            if recovered is not None:
                return await runtime._complete_operation(
                    persistence,
                    operation,
                    recovered,
                    provider=provider,
                )
        raise
    group = normalize_asset_group(
        raw_group,
        project_name=provider.project_name,
        fallback=fallback,
    )
    valid_group = (
        group.get("id") == group_id
        and str(group.get("group_type") or "").upper() == _AIGC_GROUP_TYPE
        and group.get("project_name") == provider.project_name
        and runtime._group_target_reached(operation, group)
    )
    if not valid_group:
        recovered = await runtime._reconcile_update_group(
            client,
            provider,
            operation,
        )
        if recovered is None:
            raise runtime._OperationFailure(
                "volcano_asset_update_group_unconfirmed",
                "could not confirm the Volcano asset group update",
                retryable=True,
                retry_after_seconds=10,
            )
        group = recovered
    return await runtime._complete_operation(
        persistence,
        operation,
        group,
        provider=provider,
    )


async def _process_delete_group(
    redis: Any,
    operation: dict[str, Any],
    provider: VideoProviderDefinition,
    client: Any,
    *,
    persistence: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    group_id = str(operation.get("group_id") or "")
    try:
        await runtime._get_scoped_group(client, provider, group_id)
    except VolcanoAssetServiceError as exc:
        if not runtime._is_not_found(exc):
            raise
        return await runtime._complete_operation(
            persistence,
            operation,
            runtime._delete_group_result(
                group_id,
                runtime._operation_deleted_asset_ids(operation),
                already_deleted=True,
            ),
            provider=provider,
        )

    await persistence.update(
        operation,
        progress_stage="inventorying_assets",
    )
    deleted_asset_ids = await runtime._list_group_asset_ids_best_effort(
        client,
        provider,
        operation,
    )
    if deleted_asset_ids != runtime._operation_deleted_asset_ids(operation):
        await persistence.update(
            operation,
            deleted_asset_ids=deleted_asset_ids,
        )
    await runtime._confirm_operation_lock(persistence)
    await persistence.update(
        operation,
        progress_stage="submitting",
        submit_started_at=runtime._utc_iso(),
    )
    already_deleted = False
    try:
        await client.request(
            "DeleteAssetGroup",
            {
                "Id": group_id,
                "ProjectName": provider.project_name,
            },
        )
    except VolcanoAssetServiceError as exc:
        if runtime._is_not_found(exc):
            already_deleted = True
        elif runtime._service_failure(
            exc
        ).retryable and await runtime._resource_is_deleted(
            lambda: runtime._get_scoped_group(client, provider, group_id)
        ):
            already_deleted = True
        else:
            raise
    return await runtime._complete_operation(
        persistence,
        operation,
        runtime._delete_group_result(
            group_id,
            deleted_asset_ids,
            already_deleted=already_deleted,
        ),
        provider=provider,
    )


async def _process_update_asset(
    redis: Any,
    operation: dict[str, Any],
    provider: VideoProviderDefinition,
    client: Any,
    *,
    persistence: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    asset_id = str(operation.get("asset_id") or "")
    current = await runtime._get_scoped_asset(client, provider, asset_id)
    if runtime._asset_target_reached(operation, current):
        return await runtime._complete_operation(
            persistence,
            operation,
            current,
            provider=provider,
        )
    await runtime._confirm_operation_lock(persistence)
    await persistence.update(
        operation,
        progress_stage="submitting",
        submit_started_at=runtime._utc_iso(),
    )
    try:
        raw_asset = await client.request(
            "UpdateAsset",
            {
                "Id": asset_id,
                "Name": str(operation.get("name") or ""),
                "ProjectName": provider.project_name,
            },
        )
    except VolcanoAssetServiceError as exc:
        if runtime._service_failure(exc).retryable:
            recovered = await runtime._reconcile_update_asset(
                client,
                provider,
                operation,
            )
            if recovered is not None:
                return await runtime._complete_operation(
                    persistence,
                    operation,
                    recovered,
                    provider=provider,
                )
        raise
    asset = normalize_asset(
        raw_asset,
        project_name=provider.project_name,
        fallback={
            **current,
            "name": str(operation.get("name") or ""),
        },
    )
    valid_asset = (
        asset.get("id") == asset_id
        and asset.get("project_name") == provider.project_name
        and runtime._asset_target_reached(operation, asset)
    )
    if not valid_asset:
        recovered = await runtime._reconcile_update_asset(
            client,
            provider,
            operation,
        )
        if recovered is None:
            raise runtime._OperationFailure(
                "volcano_asset_update_unconfirmed",
                "could not confirm the Volcano asset update",
                retryable=True,
                retry_after_seconds=10,
            )
        asset = recovered
    return await runtime._complete_operation(
        persistence,
        operation,
        asset,
        provider=provider,
    )


async def _process_delete_asset(
    redis: Any,
    operation: dict[str, Any],
    provider: VideoProviderDefinition,
    client: Any,
    *,
    persistence: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    asset_id = str(operation.get("asset_id") or "")
    current: dict[str, Any] | None = None
    try:
        current = await runtime._get_scoped_asset(client, provider, asset_id)
    except VolcanoAssetServiceError as exc:
        if not runtime._is_not_found(exc):
            raise
        return await runtime._complete_operation(
            persistence,
            operation,
            runtime._delete_asset_result(
                asset_id,
                group_id=(str(operation.get("group_id") or "") or None),
                already_deleted=True,
            ),
            provider=provider,
        )

    await runtime._confirm_operation_lock(persistence)
    await persistence.update(
        operation,
        progress_stage="submitting",
        submit_started_at=runtime._utc_iso(),
    )
    already_deleted = False
    try:
        await client.request(
            "DeleteAsset",
            {
                "Id": asset_id,
                "ProjectName": provider.project_name,
            },
        )
    except VolcanoAssetServiceError as exc:
        if runtime._is_not_found(exc):
            already_deleted = True
        elif runtime._service_failure(
            exc
        ).retryable and await runtime._resource_is_deleted(
            lambda: runtime._get_scoped_asset(client, provider, asset_id)
        ):
            already_deleted = True
        else:
            raise
    return await runtime._complete_operation(
        persistence,
        operation,
        runtime._delete_asset_result(
            asset_id,
            group_id=str(current.get("group_id") or "") if current else None,
            already_deleted=already_deleted,
        ),
        provider=provider,
    )


async def _record_operation_failure(
    persistence: Any,
    operation: dict[str, Any],
    failure: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    operation_id = str(operation.get("id") or "")
    action = str(operation.get("action") or "")
    error = {
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
        "retry_after_seconds": failure.retry_after_seconds,
    }
    await runtime._persist_terminal_operation(
        persistence,
        operation,
        status="failed",
        result=None,
        error=error,
        retryable=failure.retryable,
        retry_after_seconds=failure.retry_after_seconds,
    )
    event_type = {
        "create_group": "video_asset_group.create.failed",
        "update_group": "video_asset_group.update.failed",
        "delete_group": "video_asset_group.delete.failed",
        "create_asset": "video_asset.create.failed",
        "update_asset": "video_asset.update.failed",
        "delete_asset": "video_asset.delete.failed",
    }.get(action, "video_asset.operation.failed")
    try:
        await runtime._write_audit(
            operation,
            event_type=event_type,
            details={
                "operation_id": operation_id,
                "action": action,
                "group_id": operation.get("group_id"),
                "asset_id": operation.get("asset_id"),
                "asset_type": operation.get("asset_type"),
                "local_source_id": operation.get("local_source_id"),
                "model": operation.get("model"),
                "provider_name": operation.get("provider_name"),
                "project_name": operation.get("project_name"),
                "error_code": failure.code,
                "retryable": failure.retryable,
                **persistence.fence.details(),
            },
        )
    except Exception:  # noqa: BLE001
        logger.error(
            "video_asset.failure_audit_failed operation_id=%s",
            operation_id,
            exc_info=True,
        )
    return {
        "status": "failed",
        "operation_id": operation_id,
        "error": {
            "code": failure.code,
            "message": failure.message,
            "retryable": failure.retryable,
        },
    }


async def _process_management_action(
    redis: Any,
    operation: dict[str, Any],
    *,
    persistence: Any,
) -> dict[str, Any]:
    runtime = _runtime()
    action = str(operation.get("action") or "")
    try:
        if persistence.fence.lease_lost.is_set():
            raise runtime._LeaseLostError("Volcano asset operation lease was lost")
        await persistence.update(
            operation,
            status="running",
            progress_stage="validating_scope",
            retryable=False,
            retry_after_seconds=None,
            error=None,
        )
        provider = await runtime._provider_for_operation(operation)
        client = runtime.VolcanoAssetClient(provider)
        handlers = {
            "create_group": runtime._process_create_group,
            "update_group": runtime._process_update_group,
            "delete_group": runtime._process_delete_group,
            "update_asset": runtime._process_update_asset,
            "delete_asset": runtime._process_delete_asset,
        }
        handler = handlers.get(action)
        if handler is None:
            raise runtime._OperationFailure(
                "video_asset_operation_action_invalid",
                "video asset operation action is invalid",
                retryable=False,
            )
        return await handler(
            redis,
            operation,
            provider,
            client,
            persistence=persistence,
        )
    except (
        runtime._LeaseLostError,
        runtime._SuccessPersistenceError,
        runtime.VolcanoAssetRedisUnavailable,
    ):
        raise
    except VolcanoAssetQuotaExceeded as exc:
        failure = runtime._OperationFailure(
            "volcano_asset_group_quota_exceeded",
            "the Volcano project already has 50 asset groups",
            retryable=True,
        )
        logger.info(
            "video_asset.group_quota_exceeded operation_id=%s "
            "upstream_total=%s reservations=%s",
            operation.get("id"),
            exc.upstream_total,
            exc.local_reservations,
        )
    except VolcanoAssetServiceError as exc:
        failure = runtime._service_failure(exc)
    except runtime._OperationFailure as exc:
        failure = exc
    except Exception:  # noqa: BLE001
        logger.exception(
            "video_asset.worker_failed operation_id=%s action=%s",
            operation.get("id"),
            action,
        )
        failure = runtime._OperationFailure(
            "video_asset_operation_failed",
            "video asset operation failed",
            retryable=True,
            retry_after_seconds=10,
        )
    return await runtime._record_operation_failure(
        persistence,
        operation,
        failure,
    )


def _operation_contract_failure(operation: dict[str, Any]) -> Any | None:
    runtime = _runtime()
    action = str(operation.get("action") or "")
    if action not in runtime._SUPPORTED_ACTIONS:
        return runtime._OperationFailure(
            "video_asset_operation_action_invalid",
            "video asset operation action is invalid",
            retryable=False,
        )
    required_by_action = {
        "create_group": ("name",),
        "update_group": ("group_id",),
        "delete_group": ("group_id",),
        "create_asset": (
            "group_id",
            "name",
            "asset_type",
            "local_source_id",
            "public_base_url",
        ),
        "update_asset": ("asset_id", "name"),
        "delete_asset": ("asset_id",),
    }
    missing = [
        key
        for key in required_by_action[action]
        if not str(operation.get(key) or "").strip()
    ]
    if missing:
        return runtime._OperationFailure(
            "video_asset_operation_payload_invalid",
            "video asset operation payload is incomplete",
            retryable=False,
        )
    if action == "create_asset" and str(operation.get("asset_type") or "") not in {
        "Image",
        "Video",
    }:
        return runtime._OperationFailure(
            "video_asset_type_invalid",
            "asset type must be Image or Video",
            retryable=False,
        )
    return None


__all__ = [
    "_ambiguous_create_asset_failure",
    "_ambiguous_create_group_failure",
    "_asset_target_reached",
    "_delete_asset_result",
    "_delete_group_result",
    "_get_scoped_asset",
    "_get_scoped_group",
    "_group_target_reached",
    "_is_not_found",
    "_list_group_asset_ids_best_effort",
    "_operation_contract_failure",
    "_operation_deleted_asset_ids",
    "_parse_operation_time",
    "_process_create_group",
    "_process_delete_asset",
    "_process_delete_group",
    "_process_management_action",
    "_process_update_asset",
    "_process_update_group",
    "_reconcile_update_asset",
    "_reconcile_update_group",
    "_record_operation_failure",
    "_require_asset_scope",
    "_resource_is_deleted",
    "install_runtime",
]
