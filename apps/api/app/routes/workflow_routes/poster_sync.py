"""State synchronization for poster workflow outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.models import (
    Completion,
    Generation,
    Image,
    PosterMaster,
    PosterRender,
    WorkflowRun,
    WorkflowStep,
)


@dataclass(frozen=True)
class PosterSyncHooks:
    load_steps: Callable[..., Awaitable[list[WorkflowStep]]]
    parse_copy_analysis_text: Callable[[str], dict[str, Any]]
    generation_batch_outcome: Callable[..., str]
    failed_generation_output: Callable[..., dict[str, Any]]
    dedupe_nonempty: Callable[[Any], list[str]]


def _first_images_by_generation(images: Iterable[Image]) -> dict[str, Image]:
    images_by_generation: dict[str, Image] = {}
    for image in images:
        generation_id = image.owner_generation_id
        if generation_id and generation_id not in images_by_generation:
            images_by_generation[generation_id] = image
    return images_by_generation


async def sync_poster_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    workflow_type: str,
    hooks: PosterSyncHooks,
) -> None:
    """Advance copy, master, and multi-size output state from durable tasks."""
    if run.type != workflow_type:
        return
    steps = {step.step_key: step for step in await hooks.load_steps(db, run.id)}

    copy_step = steps.get("copy_analysis")
    if copy_step and copy_step.status == "running" and copy_step.task_ids:
        completion = (
            await db.execute(
                select(Completion)
                .where(Completion.id.in_(copy_step.task_ids))
                .order_by(desc(Completion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if completion is not None:
            if completion.status == CompletionStatus.SUCCEEDED.value:
                copy_step.output_json = hooks.parse_copy_analysis_text(completion.text)
                copy_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "copy_analysis"
            elif completion.status == CompletionStatus.FAILED.value:
                copy_step.status = "failed"
                copy_step.output_json = {
                    "error_code": completion.error_code,
                    "error_message": completion.error_message,
                }
                run.status = "failed"

    masters = list(
        (
            await db.execute(
                select(PosterMaster)
                .where(PosterMaster.workflow_run_id == run.id)
                .order_by(PosterMaster.candidate_index.asc())
            )
        )
        .scalars()
        .all()
    )
    if masters:
        all_master_task_ids = [
            task_id for master in masters for task_id in (master.task_ids or [])
        ]
        gens_by_id: dict[str, Generation] = {}
        images_by_gen: dict[str, Image] = {}
        if all_master_task_ids:
            master_generations = (
                (
                    await db.execute(
                        select(Generation).where(Generation.id.in_(all_master_task_ids))
                    )
                )
                .scalars()
                .all()
            )
            gens_by_id = {
                generation.id: generation for generation in master_generations
            }
            images = (
                (
                    await db.execute(
                        select(Image)
                        .where(
                            Image.owner_generation_id.in_(
                                [generation.id for generation in master_generations]
                            ),
                            Image.deleted_at.is_(None),
                        )
                        .order_by(Image.created_at.asc(), Image.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            images_by_gen = _first_images_by_generation(images)

        for master in masters:
            if master.image_id is None:
                for task_id in master.task_ids or []:
                    master_image = images_by_gen.get(task_id)
                    if master_image is not None:
                        master.image_id = master_image.id
                        break
            if master.image_id and master.status == "generating":
                master.status = "ready"
            elif (
                master.status == "generating"
                and master.task_ids
                and all(
                    gens_by_id.get(task_id) is not None
                    and gens_by_id[task_id].status == GenerationStatus.FAILED.value
                    for task_id in master.task_ids
                )
            ):
                master.status = "failed"

        master_step = steps.get("master_generation")
        if master_step and master_step.status == "running":
            current_task_ids = {
                task_id
                for task_id in (master_step.task_ids or [])
                if isinstance(task_id, str)
            }
            current_masters = [
                master
                for master in masters
                if not current_task_ids
                or current_task_ids.intersection(set(master.task_ids or []))
            ]
            ready_count = sum(
                1 for master in current_masters if master.status == "ready"
            )
            active_count = sum(
                1 for master in current_masters if master.status == "generating"
            )
            expected = int(
                (master_step.input_json or {}).get("candidate_count")
                or len(current_masters)
                or len(masters)
            )
            batch_outcome = hooks.generation_batch_outcome(
                ready_count=ready_count,
                active_count=active_count,
                expected_count=expected,
            )
            if batch_outcome in {"complete", "partial"}:
                master_step.status = "needs_review"
                master_step.image_ids = hooks.dedupe_nonempty(
                    master.image_id
                    for master in current_masters
                    if isinstance(master.image_id, str)
                )
                run.current_step = "master_approval"
                run.status = "needs_review"
                approval_step = steps.get("master_approval")
                if approval_step and approval_step.status == "waiting_input":
                    approval_step.status = "needs_review"
                if batch_outcome == "partial":
                    failed_generations = [
                        generation
                        for generation in gens_by_id.values()
                        if generation.status == GenerationStatus.FAILED.value
                        and (not current_task_ids or generation.id in current_task_ids)
                    ]
                    master_step.output_json = hooks.failed_generation_output(
                        master_step.output_json,
                        failed_generations,
                        fallback="部分母版生成失败",
                        partial=True,
                    )
            elif batch_outcome == "failed":
                failed_generations = [
                    generation
                    for generation in gens_by_id.values()
                    if generation.status == GenerationStatus.FAILED.value
                    and (not current_task_ids or generation.id in current_task_ids)
                ]
                master_step.status = "failed"
                master_step.output_json = hooks.failed_generation_output(
                    master_step.output_json,
                    failed_generations,
                    fallback="母版生成失败",
                    partial=False,
                )
                run.status = "failed"
                run.current_step = "master_generation"

    renders = list(
        (
            await db.execute(
                select(PosterRender)
                .where(PosterRender.workflow_run_id == run.id)
                .order_by(PosterRender.created_at.asc(), PosterRender.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if renders:
        all_render_task_ids = [
            task_id for render in renders for task_id in (render.task_ids or [])
        ]
        render_gens_by_id: dict[str, Generation] = {}
        render_images_by_gen: dict[str, Image] = {}
        if all_render_task_ids:
            render_generations = (
                (
                    await db.execute(
                        select(Generation).where(Generation.id.in_(all_render_task_ids))
                    )
                )
                .scalars()
                .all()
            )
            render_gens_by_id = {
                generation.id: generation for generation in render_generations
            }
            render_images = (
                (
                    await db.execute(
                        select(Image)
                        .where(
                            Image.owner_generation_id.in_(
                                [generation.id for generation in render_generations]
                            ),
                            Image.deleted_at.is_(None),
                        )
                        .order_by(Image.created_at.asc(), Image.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            render_images_by_gen = _first_images_by_generation(render_images)
        multi_step = steps.get("multi_size_generation")
        active_task_ids: set[str] = set()
        if multi_step and multi_step.status == "running":
            raw_active_task_ids = (multi_step.input_json or {}).get("active_task_ids")
            if isinstance(raw_active_task_ids, list):
                active_task_ids = {
                    task_id
                    for task_id in raw_active_task_ids
                    if isinstance(task_id, str) and task_id
                }
        for render in renders:
            render_task_ids = [
                task_id
                for task_id in (render.task_ids or [])
                if isinstance(task_id, str)
            ]
            task_ids_for_status = render_task_ids
            if render.status in {"generating", "revising"} and active_task_ids:
                active_for_render = [
                    task_id for task_id in render_task_ids if task_id in active_task_ids
                ]
                if active_for_render:
                    task_ids_for_status = active_for_render

            latest_image_id: str | None = None
            for task_id in task_ids_for_status:
                render_image = render_images_by_gen.get(task_id)
                if render_image is not None:
                    latest_image_id = render_image.id
            if latest_image_id and latest_image_id != render.image_id:
                render.image_id = latest_image_id
            if latest_image_id and render.status in {"generating", "revising"}:
                render.status = "ready"
            elif (
                render.status in {"generating", "revising"}
                and task_ids_for_status
                and all(
                    render_gens_by_id.get(task_id) is not None
                    and render_gens_by_id[task_id].status
                    == GenerationStatus.FAILED.value
                    for task_id in task_ids_for_status
                )
            ):
                render.status = "failed"

        if multi_step and multi_step.status == "running":
            current_renders = (
                [
                    render
                    for render in renders
                    if active_task_ids.intersection(
                        task_id
                        for task_id in (render.task_ids or [])
                        if isinstance(task_id, str)
                    )
                ]
                if active_task_ids
                else renders
            )
            ready_count = sum(
                1 for render in current_renders if render.status == "ready"
            )
            active_count = sum(
                1
                for render in current_renders
                if render.status in {"generating", "revising"}
            )
            raw_expected = int(
                (multi_step.input_json or {}).get("expected_render_count")
                or len(current_renders)
                or len(renders)
            )
            expected = min(raw_expected, len(current_renders) or raw_expected)
            multi_step.image_ids = hooks.dedupe_nonempty(
                render.image_id
                for render in renders
                if isinstance(render.image_id, str)
            )
            batch_outcome = hooks.generation_batch_outcome(
                ready_count=ready_count,
                active_count=active_count,
                expected_count=expected,
            )
            if batch_outcome in {"complete", "partial"}:
                multi_step.status = "needs_review"
                run.current_step = "multi_size_generation"
                run.status = "needs_review"
                if batch_outcome == "partial":
                    failed_generations = [
                        generation
                        for generation in render_gens_by_id.values()
                        if generation.status == GenerationStatus.FAILED.value
                        and (not active_task_ids or generation.id in active_task_ids)
                    ]
                    multi_step.output_json = hooks.failed_generation_output(
                        multi_step.output_json,
                        failed_generations,
                        fallback="部分多尺寸生成失败",
                        partial=True,
                    )
            elif batch_outcome == "failed":
                failed_generations = [
                    generation
                    for generation in render_gens_by_id.values()
                    if generation.status == GenerationStatus.FAILED.value
                    and (not active_task_ids or generation.id in active_task_ids)
                ]
                multi_step.status = "failed"
                multi_step.output_json = hooks.failed_generation_output(
                    multi_step.output_json,
                    failed_generations,
                    fallback="多尺寸生成失败",
                    partial=False,
                )
                run.status = "failed"
                run.current_step = "multi_size_generation"
        elif multi_step and multi_step.status in {"needs_review", "completed"}:
            multi_step.image_ids = hooks.dedupe_nonempty(
                render.image_id
                for render in renders
                if isinstance(render.image_id, str)
            )
