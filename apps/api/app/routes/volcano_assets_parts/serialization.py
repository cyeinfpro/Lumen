"""Serialization helpers for queued Volcano asset operations."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from lumen_core.schemas import (
    VideoAssetCreateAcceptedOut,
    VideoAssetOperationOut,
    VideoAssetOut,
)
from lumen_core.volcano_assets import VolcanoAssetQuotaKey


OPERATION_ACTIONS = frozenset(
    {
        "create_group",
        "update_group",
        "delete_group",
        "create_asset",
        "update_asset",
        "delete_asset",
    }
)


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def operation_quota_key(operation: dict[str, Any]) -> VolcanoAssetQuotaKey:
    return VolcanoAssetQuotaKey(
        provider_name=str(operation.get("provider_name") or ""),
        project_name=str(operation.get("project_name") or ""),
        region=str(operation.get("region") or ""),
    )


def operation_out(
    operation: dict[str, Any],
    *,
    http_error: Callable[..., HTTPException],
    now_iso: Callable[[], str],
) -> VideoAssetOperationOut:
    action = str(operation.get("action") or "")
    if action not in OPERATION_ACTIONS:
        raise http_error(
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
        created_at=str(operation.get("created_at") or now_iso()),
        updated_at=str(operation.get("updated_at") or now_iso()),
        completed_at=operation.get("completed_at"),
    )


def operation_asset_response(
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


def same_operation_scope(
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


def same_operation_intent(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    same_scope: Callable[[dict[str, Any], dict[str, Any]], bool],
) -> bool:
    return (
        same_scope(left, right)
        and left.get("target_id") == right.get("target_id")
        and left.get("fields") == right.get("fields")
        and str(left.get("public_base_url") or "")
        == str(right.get("public_base_url") or "")
    )
