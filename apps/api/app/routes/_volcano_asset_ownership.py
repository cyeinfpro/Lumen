"""Provider-bound ownership receipts for Volcano asset routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import AuditLog
from lumen_core.video_providers import (
    VideoProviderDefinition,
    video_provider_binding_fingerprint,
)


_RECEIPT_EVENT = "video_asset.operation.receipt"


@dataclass(frozen=True)
class OwnedResourceReceipts:
    resource_ids: frozenset[str]
    group_ids: frozenset[str]


def _receipt_action(resource_type: str) -> str:
    return "create_group" if resource_type == "group" else "create_asset"


def _receipt_scope_filters(
    provider: VideoProviderDefinition,
    *,
    action: str,
) -> tuple[Any, ...]:
    return (
        AuditLog.event_type == _RECEIPT_EVENT,
        AuditLog.details["action"].as_string() == action,
        AuditLog.details["project_name"].as_string() == provider.project_name,
        AuditLog.details["provider_name"].as_string() == provider.name,
        AuditLog.details["region"].as_string() == provider.region,
        AuditLog.details["provider_binding"].as_string()
        == video_provider_binding_fingerprint(provider),
    )


async def resource_owner_user_id(
    db: AsyncSession,
    *,
    provider: VideoProviderDefinition,
    resource_type: str,
    resource_id: str,
) -> str | None:
    action = _receipt_action(resource_type)
    return (
        await db.execute(
            select(AuditLog.user_id)
            .where(
                *_receipt_scope_filters(provider, action=action),
                AuditLog.details["result"]["id"].as_string() == resource_id,
            )
            .order_by(AuditLog.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def owned_resource_receipts(
    db: AsyncSession,
    *,
    provider: VideoProviderDefinition,
    resource_type: str,
    user_id: str,
) -> OwnedResourceReceipts:
    action = _receipt_action(resource_type)
    details_rows = (
        (
            await db.execute(
                select(AuditLog.details).where(
                    AuditLog.user_id == str(user_id),
                    *_receipt_scope_filters(provider, action=action),
                )
            )
        )
        .scalars()
        .all()
    )
    resource_ids: set[str] = set()
    group_ids: set[str] = set()
    for details in details_rows:
        if not isinstance(details, dict):
            continue
        result = details.get("result")
        if not isinstance(result, dict):
            continue
        resource_id = str(result.get("id") or "").strip()
        if resource_id:
            resource_ids.add(resource_id)
        group_id = str(result.get("group_id") or "").strip()
        if group_id:
            group_ids.add(group_id)
    return OwnedResourceReceipts(
        resource_ids=frozenset(resource_ids),
        group_ids=frozenset(group_ids),
    )


def operation_matches_provider_snapshot(
    operation: dict[str, Any],
    provider: VideoProviderDefinition,
) -> bool:
    expected = {
        "provider_name": provider.name,
        "project_name": provider.project_name,
        "region": provider.region,
        "provider_binding": video_provider_binding_fingerprint(provider),
    }
    return all(
        bool(str(operation.get(key) or "").strip())
        and str(operation.get(key) or "") == value
        for key, value in expected.items()
    )
