"""Durable Volcano asset success-receipt helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lumen_core.models import AuditLog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


AIGC_GROUP_TYPE = "AIGC"
SUCCESS_RECEIPT_EVENT = "video_asset.operation.receipt"
LEGACY_SUCCESS_RECEIPT_EVENT = "video_asset.create.receipt"
RECEIPT_BINDING_FIELDS = ("provider_name", "region", "provider_binding")


def receipt_asset(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        key: asset.get(key)
        for key in (
            "id",
            "group_id",
            "name",
            "asset_type",
            "status",
            "project_name",
            "create_time",
            "update_time",
            "error_code",
            "error_message",
        )
    }


def receipt_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        key: group.get(key)
        for key in (
            "id",
            "name",
            "title",
            "description",
            "group_type",
            "project_name",
            "create_time",
            "update_time",
        )
    }


def operation_has_value(operation: dict[str, Any], key: str) -> bool:
    return key in operation and operation.get(key) is not None


def receipt_result(
    operation: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    action = str(operation.get("action") or "")
    if action in {"create_group", "update_group"}:
        return receipt_group(result)
    if action in {"create_asset", "update_asset"}:
        return receipt_asset(result)
    if action in {"delete_group", "delete_asset"}:
        return {
            "id": result.get("id"),
            "deleted": bool(result.get("deleted")),
            "resource_type": result.get("resource_type"),
            "group_id": result.get("group_id"),
            "asset_id": result.get("asset_id"),
            "deleted_asset_ids": [
                str(asset_id)
                for asset_id in (
                    result.get("deleted_asset_ids")
                    if isinstance(result.get("deleted_asset_ids"), list)
                    else []
                )
                if asset_id is not None and str(asset_id)
            ],
            "already_deleted": bool(result.get("already_deleted")),
            "cascade_assets": result.get("cascade_assets"),
        }
    return {}


def success_receipt_details(
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    fence: Any | None = None,
) -> dict[str, Any]:
    details = {
        "operation_id": str(operation.get("id") or ""),
        "action": str(operation.get("action") or ""),
        "provider_name": str(operation.get("provider_name") or ""),
        "provider_binding": str(operation.get("provider_binding") or ""),
        "project_name": str(operation.get("project_name") or ""),
        "region": str(operation.get("region") or ""),
        "result": receipt_result(operation, result),
    }
    if fence is not None:
        details.update(fence.details())
    return details


def receipt_binding_matches(
    operation: dict[str, Any],
    details: dict[str, Any],
) -> bool:
    for field in RECEIPT_BINDING_FIELDS:
        expected = str(operation.get(field) or "")
        if not expected or str(details.get(field) or "") != expected:
            return False
    return True


def receipt_fence_matches(
    operation: dict[str, Any],
    details: dict[str, Any],
    fence: Any,
) -> bool:
    try:
        attempt = int(details.get("attempt"))
        fencing = int(details.get("fencing"))
    except (TypeError, ValueError):
        return False
    lock_token = str(details.get("lock_token") or "")
    if (
        attempt != max(1, int(operation.get("attempt") or 1))
        or fencing <= 0
        or fencing > fence.fencing
        or not lock_token
    ):
        return False
    return fencing != fence.fencing or lock_token == fence.lock_token


def validated_receipt_result(
    operation: dict[str, Any],
    raw: Any,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    action = str(operation.get("action") or "")
    project_name = str(operation.get("project_name") or "")
    if action == "create_asset":
        expected = {
            "group_id": str(operation.get("group_id") or ""),
            "name": str(operation.get("name") or ""),
            "asset_type": str(operation.get("asset_type") or ""),
            "project_name": project_name,
        }
        if any(str(raw.get(key) or "") != value for key, value in expected.items()):
            return None
    elif action == "update_asset":
        if (
            str(raw.get("id") or "") != str(operation.get("asset_id") or "")
            or str(raw.get("name") or "") != str(operation.get("name") or "")
            or str(raw.get("project_name") or "") != project_name
        ):
            return None
    elif action in {"create_group", "update_group"}:
        expected_id = (
            str(operation.get("group_id") or "")
            if action == "update_group"
            else str(raw.get("id") or "")
        )
        if (
            str(raw.get("id") or "") != expected_id
            or str(raw.get("group_type") or "").upper() != AIGC_GROUP_TYPE
            or str(raw.get("project_name") or "") != project_name
        ):
            return None
        if operation_has_value(operation, "name") and str(raw.get("name") or "") != str(
            operation.get("name") or ""
        ):
            return None
        if operation_has_value(operation, "description") and str(
            raw.get("description") or ""
        ) != str(operation.get("description") or ""):
            return None
    elif action == "delete_group":
        if (
            str(raw.get("id") or "") != str(operation.get("group_id") or "")
            or raw.get("resource_type") != "group"
            or str(raw.get("group_id") or "") != str(operation.get("group_id") or "")
            or raw.get("deleted") is not True
            or raw.get("cascade_assets") is not True
            or not isinstance(raw.get("deleted_asset_ids"), list)
        ):
            return None
    elif action == "delete_asset":
        if (
            str(raw.get("id") or "") != str(operation.get("asset_id") or "")
            or raw.get("resource_type") != "asset"
            or str(raw.get("asset_id") or "") != str(operation.get("asset_id") or "")
            or raw.get("deleted") is not True
            or not isinstance(raw.get("deleted_asset_ids"), list)
            or str(operation.get("asset_id") or "")
            not in {str(item) for item in raw.get("deleted_asset_ids") or []}
        ):
            return None
    else:
        return None
    return receipt_result(operation, raw)


async def read_success_receipt(
    operation: dict[str, Any],
    *,
    fence: Any,
    session_factory: Callable[[], Any],
) -> dict[str, Any] | None:
    operation_id = str(operation.get("id") or "")
    user_id = str(operation.get("user_id") or "")
    if not operation_id or not user_id:
        return None
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.id == operation_id,
                    AuditLog.user_id == user_id,
                    AuditLog.event_type.in_(
                        (
                            SUCCESS_RECEIPT_EVENT,
                            LEGACY_SUCCESS_RECEIPT_EVENT,
                        )
                    ),
                )
            )
        ).scalar_one_or_none()
    if row is None or not isinstance(row.details, dict):
        return None
    if str(row.details.get("operation_id") or "") != operation_id:
        return None
    if not receipt_fence_matches(operation, row.details, fence):
        return None
    action = str(operation.get("action") or "")
    if row.event_type == LEGACY_SUCCESS_RECEIPT_EVENT:
        if action != "create_asset" or not receipt_binding_matches(
            operation,
            row.details,
        ):
            return None
        raw_result = row.details.get("asset")
    else:
        if str(row.details.get("action") or "") != action:
            return None
        if str(row.details.get("project_name") or "") != str(
            operation.get("project_name") or ""
        ):
            return None
        if not receipt_binding_matches(operation, row.details):
            return None
        raw_result = row.details.get("result")
    return validated_receipt_result(operation, raw_result)


async def write_success_receipt(
    operation: dict[str, Any],
    result: dict[str, Any],
    *,
    fence: Any,
    session_factory: Callable[[], Any],
) -> None:
    operation_id = str(operation.get("id") or "")
    receipt = AuditLog(
        id=operation_id,
        user_id=str(operation.get("user_id") or "") or None,
        event_type=SUCCESS_RECEIPT_EVENT,
        actor_email_hash=operation.get("actor_email_hash"),
        actor_ip_hash=operation.get("actor_ip_hash"),
        details=success_receipt_details(operation, result, fence=fence),
    )
    async with session_factory() as session:
        session.add(receipt)
        try:
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()
        existing = await session.get(AuditLog, operation_id)
        if (
            existing is None
            or existing.event_type
            not in {SUCCESS_RECEIPT_EVENT, LEGACY_SUCCESS_RECEIPT_EVENT}
            or str(existing.user_id or "") != str(operation.get("user_id") or "")
            or not isinstance(existing.details, dict)
            or not receipt_binding_matches(operation, existing.details)
            or not receipt_fence_matches(operation, existing.details, fence)
            or (
                existing.event_type == LEGACY_SUCCESS_RECEIPT_EVENT
                and str(operation.get("action") or "") != "create_asset"
            )
            or (
                existing.event_type == SUCCESS_RECEIPT_EVENT
                and (
                    str(existing.details.get("action") or "")
                    != str(operation.get("action") or "")
                    or str(existing.details.get("project_name") or "")
                    != str(operation.get("project_name") or "")
                )
            )
            or validated_receipt_result(
                operation,
                (
                    existing.details.get("asset")
                    if existing.event_type == LEGACY_SUCCESS_RECEIPT_EVENT
                    else existing.details.get("result")
                ),
            )
            is None
        ):
            raise RuntimeError("Volcano asset success receipt conflicts")
