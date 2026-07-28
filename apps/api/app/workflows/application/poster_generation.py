"""Poster master/render generation use-case orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..ports.poster_generation import (
    PosterGenerationPort,
    PosterMasterTask,
    PosterRenderTask,
)
from .poster_design import poster_master_prompt, poster_render_prompt


@dataclass(frozen=True, slots=True)
class GeneratePosterMasters:
    run_id: str
    existing_count: int
    candidate_count: int
    style_summary: Mapping[str, object]
    copy_analysis: Mapping[str, object]
    brand_assets: Mapping[str, object]
    brand_attachment_ids: Sequence[str]
    quality_mode: str
    size_mode: str
    size: str | None


@dataclass(frozen=True, slots=True)
class GeneratePosterRenders:
    run_id: str
    master_id: str
    pending_aspects: Sequence[str]
    style_summary: Mapping[str, object]
    copy_analysis: Mapping[str, object]
    reference_image_ids: Sequence[str]
    quality_mode: str
    use_master_as_reference: bool
    adjustments: str


@dataclass(frozen=True, slots=True)
class PosterGenerationBatchResult:
    bundles: tuple[object, ...]
    generation_ids: tuple[str, ...]


async def generate_poster_masters(
    command: GeneratePosterMasters,
    *,
    port: PosterGenerationPort,
) -> PosterGenerationBatchResult:
    bundles: list[object] = []
    generation_ids: list[str] = []
    count = max(1, min(8, command.candidate_count))
    attachment_ids = tuple(command.brand_attachment_ids)
    intent = "image_to_image" if attachment_ids else "text_to_image"
    for offset in range(1, count + 1):
        candidate_index = command.existing_count + offset
        task = PosterMasterTask(
            candidate_index=candidate_index,
            style_summary=command.style_summary,
            copy_analysis=command.copy_analysis,
            intent=intent,
            prompt=poster_master_prompt(
                style_summary=command.style_summary,
                copy_analysis=command.copy_analysis,
                brand_assets=command.brand_assets,
                candidate_index=candidate_index,
            ),
            attachment_ids=attachment_ids,
            idempotency_key=f"wf:{command.run_id[:22]}:m:{candidate_index}",
            quality_mode=command.quality_mode,
            size_mode=command.size_mode,
            size=command.size,
            workflow_meta={
                "workflow_action": "poster_master",
                "workflow_master_index": candidate_index,
            },
        )
        result = await port.submit_master(task)
        bundles.append(result.bundle)
        generation_ids.extend(result.generation_ids)
    return PosterGenerationBatchResult(
        bundles=tuple(bundles),
        generation_ids=tuple(generation_ids),
    )


async def generate_poster_renders(
    command: GeneratePosterRenders,
    *,
    port: PosterGenerationPort,
) -> PosterGenerationBatchResult:
    bundles: list[object] = []
    generation_ids: list[str] = []
    attachment_ids = tuple(command.reference_image_ids)
    intent = "image_to_image" if attachment_ids else "text_to_image"
    for task_index, aspect in enumerate(command.pending_aspects, start=1):
        task = PosterRenderTask(
            master_id=command.master_id,
            aspect_ratio=aspect,
            intent=intent,
            prompt=poster_render_prompt(
                style_summary=command.style_summary,
                copy_analysis=command.copy_analysis,
                target_aspect=aspect,
                adjustments=command.adjustments,
            ),
            attachment_ids=attachment_ids,
            idempotency_key=(f"wf:{command.run_id[:18]}:r:{task_index}:{aspect}"),
            quality_mode=command.quality_mode,
            use_master_as_reference=command.use_master_as_reference,
            workflow_meta={
                "workflow_action": "poster_render",
                "workflow_master_id": command.master_id,
                "workflow_target_aspect": aspect,
                "workflow_quality_mode": command.quality_mode,
            },
        )
        result = await port.submit_render(task)
        bundles.append(result.bundle)
        generation_ids.extend(result.generation_ids)
    return PosterGenerationBatchResult(
        bundles=tuple(bundles),
        generation_ids=tuple(generation_ids),
    )
