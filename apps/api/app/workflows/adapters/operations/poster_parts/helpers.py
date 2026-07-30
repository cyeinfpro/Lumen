"""Poster workflow input, style, and step helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.model_entities import (
    PosterStyleItem,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schema_models import ImageParamsIn

from ....application.poster_design import (
    POSTER_MASTER_ASPECT,
    PosterStyleSnapshot,
    build_poster_step_seeds,
    poster_brand_attachment_ids as collect_poster_brand_attachment_ids,
)
from ...serialization import http as _http
from ...workflow_runtime import image_params as _image_params


def poster_image_params(
    *,
    aspect_ratio: str,
    quality_mode: str,
    count: int = 1,
) -> ImageParamsIn:
    final_quality = "4k" if quality_mode == "premium" else "high"
    return _image_params(
        aspect_ratio=aspect_ratio,
        count=count,
        render_quality="high",
        final_quality=final_quality,
        fast=False,
    )


def poster_master_image_params(quality_mode: str) -> ImageParamsIn:
    return poster_image_params(
        aspect_ratio=POSTER_MASTER_ASPECT,
        quality_mode=quality_mode,
        count=1,
    )


async def poster_find_preset_item(
    db: AsyncSession, *, user_id: str, style_id: str
) -> dict[str, Any] | None:
    from .....services.poster_styles.workflow_lookup import (
        bootstrap_local_presets_if_empty,
        find_preset_item,
    )

    await bootstrap_local_presets_if_empty()
    return await find_preset_item(db, user_id=user_id, item_id=style_id)


def poster_style_from_preset(raw: dict[str, Any]) -> PosterStyleSnapshot:
    return PosterStyleSnapshot.from_mapping(raw)


async def poster_load_style(
    db: AsyncSession,
    *,
    user_id: str,
    style_id: str,
) -> PosterStyleItem | PosterStyleSnapshot:
    """Load a private database style or a shared preset."""
    if style_id.startswith("preset:"):
        preset = await poster_find_preset_item(
            db,
            user_id=user_id,
            style_id=style_id,
        )
        if preset is not None:
            return poster_style_from_preset(preset)
        raise _http("style_not_found", "poster style not found", 404)
    row = (
        await db.execute(
            select(PosterStyleItem).where(
                PosterStyleItem.id == style_id,
                PosterStyleItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("style_not_found", "poster style not found", 404)
    return row


def poster_brand_attachment_ids(run: WorkflowRun) -> list[str]:
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    return collect_poster_brand_attachment_ids(
        metadata,
        getattr(run, "product_image_ids", None) or [],
    )


def poster_seed_steps(run: WorkflowRun) -> list[WorkflowStep]:
    metadata = run.metadata_jsonb if isinstance(run.metadata_jsonb, dict) else {}
    return [
        WorkflowStep(
            workflow_run_id=run.id,
            step_key=seed.step_key,
            status=seed.status,
            input_json=dict(seed.input_json),
            output_json=dict(seed.output_json),
        )
        for seed in build_poster_step_seeds(
            user_prompt=run.user_prompt,
            metadata=metadata,
        )
    ]
