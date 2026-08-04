"""Workflow asset metadata and image response projection helpers."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    Image,
    ImageVariant,
    WorkflowRun,
)
from lumen_core.schema_models import ImageOut

from .serialization import dedupe_nonempty, http, now as current_time

WORKFLOW_ASSET_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")


def workflow_asset_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("workflow_run_id") or ""),
        str(record.get("image_id") or ""),
        str(record.get("asset_type") or ""),
        str(record.get("source_step_key") or ""),
    )


def workflow_asset_records(
    *,
    run: WorkflowRun,
    image_ids: list[str],
    asset_type: str,
    source_step_key: str,
    label: str | None,
    added_at: datetime,
) -> list[dict[str, Any]]:
    clean_label = (label or "").strip() or None
    records: list[dict[str, Any]] = []
    for image_id in image_ids:
        record: dict[str, Any] = {
            "workflow_run_id": run.id,
            "workflow_type": run.type,
            "project_title": run.title,
            "image_id": image_id,
            "asset_type": asset_type,
            "source_step_key": source_step_key,
            "added_at": added_at.isoformat(),
        }
        if clean_label:
            record["label"] = clean_label
        records.append(record)
    return records


def merge_workflow_asset_metadata(
    metadata: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(metadata or {})
    existing_raw = payload.get("assets")
    existing = (
        [
            dict(record)
            for record in existing_raw
            if isinstance(record, dict) and isinstance(record.get("image_id"), str)
        ]
        if isinstance(existing_raw, list)
        else []
    )
    replace_keys = {workflow_asset_key(record) for record in records}
    merged = [
        record for record in existing if workflow_asset_key(record) not in replace_keys
    ]
    merged.extend(records)
    payload["assets"] = merged[-200:]
    payload["asset_image_ids"] = dedupe_nonempty(
        str(record.get("image_id") or "") for record in payload["assets"]
    )
    payload["asset_count"] = len(payload["asset_image_ids"])
    return payload


def merge_image_workflow_asset_metadata(
    metadata: dict[str, Any] | None,
    record: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(metadata or {})
    existing_raw = payload.get("workflow_assets")
    existing = (
        [
            dict(item)
            for item in existing_raw
            if isinstance(item, dict) and isinstance(item.get("workflow_run_id"), str)
        ]
        if isinstance(existing_raw, list)
        else []
    )
    key = workflow_asset_key(record)
    merged = [item for item in existing if workflow_asset_key(item) != key]
    merged.append(record)
    payload["workflow_assets"] = merged[-50:]
    payload["latest_workflow_asset"] = record
    return payload


async def attach_workflow_assets(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    user_id: str,
    image_ids: list[str],
    asset_type: str,
    source_step_key: str,
    label: str | None = None,
    added_at: datetime | None = None,
    step_loader: Any,
) -> list[dict[str, Any]]:
    clean_asset_type = (asset_type or "").strip()
    if not WORKFLOW_ASSET_TYPE_RE.fullmatch(clean_asset_type):
        raise http("invalid_asset_type", "asset_type is invalid", 422)
    clean_step_key = (source_step_key or "").strip()
    if not clean_step_key:
        raise http("missing_source_step", "source_step_key is required", 422)
    deduped_image_ids = dedupe_nonempty(image_ids)
    if not deduped_image_ids:
        raise http("missing_images", "image_ids cannot be empty", 422)

    images = list(
        (
            await db.execute(
                select(Image).where(
                    Image.user_id == user_id,
                    Image.id.in_(deduped_image_ids),
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    image_by_id = {image.id: image for image in images}
    missing = [
        image_id for image_id in deduped_image_ids if image_id not in image_by_id
    ]
    if missing:
        raise http(
            "image_not_found",
            "one or more images are not available for this workflow",
            404,
        )

    step = await step_loader(db, run.id, clean_step_key)
    added_time = added_at or current_time()
    records = workflow_asset_records(
        run=run,
        image_ids=deduped_image_ids,
        asset_type=clean_asset_type,
        source_step_key=clean_step_key,
        label=label,
        added_at=added_time,
    )
    run.metadata_jsonb = merge_workflow_asset_metadata(run.metadata_jsonb, records)
    step.image_ids = dedupe_nonempty([*(step.image_ids or []), *deduped_image_ids])
    existing_step_asset_ids = (step.output_json or {}).get("asset_image_ids")
    if not isinstance(existing_step_asset_ids, list):
        existing_step_asset_ids = []
    step_asset_ids = dedupe_nonempty([*existing_step_asset_ids, *deduped_image_ids])
    step.output_json = {
        **(step.output_json or {}),
        "asset_image_ids": step_asset_ids,
        "asset_count": len(step_asset_ids),
        "asset_updated_at": added_time.isoformat(),
    }
    for record in records:
        image = image_by_id[record["image_id"]]
        image.metadata_jsonb = merge_image_workflow_asset_metadata(
            image.metadata_jsonb,
            record,
        )
    return records


async def image_out_map(db: AsyncSession, images: list[Image]) -> dict[str, ImageOut]:
    if not images:
        return {}
    variant_rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_([image.id for image in images])
            )
        )
    ).all()
    variant_map: dict[str, set[str]] = {}
    for image_id, kind in variant_rows:
        variant_map.setdefault(image_id, set()).add(kind)
    return {
        image.id: image_to_out(image, variant_map.get(image.id)) for image in images
    }


def image_to_out(img: Image, variant_kinds: set[str] | None = None) -> ImageOut:
    variant_kinds = variant_kinds or set()
    metadata = img.metadata_jsonb if isinstance(img.metadata_jsonb, dict) else {}
    billing_label = (
        metadata.get("billing_label")
        if isinstance(metadata.get("billing_label"), str)
        else None
    )
    billing_exempt_reason = (
        metadata.get("billing_exempt_reason")
        if isinstance(metadata.get("billing_exempt_reason"), str)
        else None
    )
    is_dual_race_bonus = metadata.get("is_dual_race_bonus") is True
    billing_free = (
        metadata.get("billing_free") is True
        if "billing_free" in metadata
        else billing_label == "free"
        or (is_dual_race_bonus and billing_label is None)
    )
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=f"/api/images/{img.id}/binary",
        display_url=f"/api/images/{img.id}/variants/display2048",
        preview_url=(
            f"/api/images/{img.id}/variants/preview1024"
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            f"/api/images/{img.id}/variants/thumb256"
            if "thumb256" in variant_kinds
            else None
        ),
        metadata_jsonb=metadata,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )
