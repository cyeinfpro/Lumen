"""Poster workflow synchronization stages."""

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


@dataclass(frozen=True)
class _GenerationAssets:
    generations_by_id: dict[str, Generation]
    images_by_generation: dict[str, Image]


def _first_images_by_generation(images: Iterable[Image]) -> dict[str, Image]:
    images_by_generation: dict[str, Image] = {}
    for image in images:
        generation_id = image.owner_generation_id
        if generation_id and generation_id not in images_by_generation:
            images_by_generation[generation_id] = image
    return images_by_generation


async def _load_generation_assets(
    db: AsyncSession,
    task_ids: list[str],
) -> _GenerationAssets:
    if not task_ids:
        return _GenerationAssets({}, {})
    generations = list(
        (await db.execute(select(Generation).where(Generation.id.in_(task_ids))))
        .scalars()
        .all()
    )
    images = list(
        (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_(
                        [generation.id for generation in generations]
                    ),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return _GenerationAssets(
        {generation.id: generation for generation in generations},
        _first_images_by_generation(images),
    )


async def sync_copy_analysis_step(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: PosterSyncHooks,
) -> None:
    copy_step = steps.get("copy_analysis")
    if not copy_step or copy_step.status != "running" or not copy_step.task_ids:
        return
    completion = (
        await db.execute(
            select(Completion)
            .where(Completion.id.in_(copy_step.task_ids))
            .order_by(desc(Completion.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if completion is None:
        return
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


def _sync_master_state(
    master: PosterMaster,
    assets: _GenerationAssets,
) -> None:
    if master.image_id is None:
        for task_id in master.task_ids or []:
            image = assets.images_by_generation.get(task_id)
            if image is not None:
                master.image_id = image.id
                break
    if master.image_id and master.status == "generating":
        master.status = "ready"
    elif (
        master.status == "generating"
        and master.task_ids
        and all(
            assets.generations_by_id.get(task_id) is not None
            and assets.generations_by_id[task_id].status
            == GenerationStatus.FAILED.value
            for task_id in master.task_ids
        )
    ):
        master.status = "failed"


def _failed_generations(
    assets: _GenerationAssets,
    task_ids: set[str],
) -> list[Generation]:
    return [
        generation
        for generation in assets.generations_by_id.values()
        if generation.status == GenerationStatus.FAILED.value
        and (not task_ids or generation.id in task_ids)
    ]


def _advance_master_step(
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    masters: list[PosterMaster],
    assets: _GenerationAssets,
    hooks: PosterSyncHooks,
) -> None:
    master_step = steps.get("master_generation")
    if not master_step or master_step.status != "running":
        return
    current_task_ids = {
        task_id for task_id in (master_step.task_ids or []) if isinstance(task_id, str)
    }
    current_masters = [
        master
        for master in masters
        if not current_task_ids
        or current_task_ids.intersection(set(master.task_ids or []))
    ]
    outcome = hooks.generation_batch_outcome(
        ready_count=sum(master.status == "ready" for master in current_masters),
        active_count=sum(master.status == "generating" for master in current_masters),
        expected_count=int(
            (master_step.input_json or {}).get("candidate_count")
            or len(current_masters)
            or len(masters)
        ),
    )
    failed_generations = _failed_generations(assets, current_task_ids)
    if outcome in {"complete", "partial"}:
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
        if outcome == "partial":
            master_step.output_json = hooks.failed_generation_output(
                master_step.output_json,
                failed_generations,
                fallback="部分母版生成失败",
                partial=True,
            )
    elif outcome == "failed":
        master_step.status = "failed"
        master_step.output_json = hooks.failed_generation_output(
            master_step.output_json,
            failed_generations,
            fallback="母版生成失败",
            partial=False,
        )
        run.status = "failed"
        run.current_step = "master_generation"


async def sync_master_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: PosterSyncHooks,
) -> None:
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
    if not masters:
        return
    task_ids = [task_id for master in masters for task_id in (master.task_ids or [])]
    assets = await _load_generation_assets(db, task_ids)
    for master in masters:
        _sync_master_state(master, assets)
    _advance_master_step(run, steps, masters, assets, hooks)


def _active_render_task_ids(multi_step: WorkflowStep | None) -> set[str]:
    if not multi_step or multi_step.status != "running":
        return set()
    raw_task_ids = (multi_step.input_json or {}).get("active_task_ids")
    if not isinstance(raw_task_ids, list):
        return set()
    return {task_id for task_id in raw_task_ids if isinstance(task_id, str) and task_id}


def _render_task_ids_for_status(
    render: PosterRender,
    active_task_ids: set[str],
) -> list[str]:
    render_task_ids = [
        task_id for task_id in (render.task_ids or []) if isinstance(task_id, str)
    ]
    if render.status not in {"generating", "revising"} or not active_task_ids:
        return render_task_ids
    active_for_render = [
        task_id for task_id in render_task_ids if task_id in active_task_ids
    ]
    return active_for_render or render_task_ids


def _sync_render_state(
    render: PosterRender,
    assets: _GenerationAssets,
    active_task_ids: set[str],
) -> None:
    task_ids = _render_task_ids_for_status(render, active_task_ids)
    latest_image_id: str | None = None
    for task_id in task_ids:
        image = assets.images_by_generation.get(task_id)
        if image is not None:
            latest_image_id = image.id
    if latest_image_id and latest_image_id != render.image_id:
        render.image_id = latest_image_id
    if latest_image_id and render.status in {"generating", "revising"}:
        render.status = "ready"
    elif (
        render.status in {"generating", "revising"}
        and task_ids
        and all(
            assets.generations_by_id.get(task_id) is not None
            and assets.generations_by_id[task_id].status
            == GenerationStatus.FAILED.value
            for task_id in task_ids
        )
    ):
        render.status = "failed"


def _current_render_batch(
    renders: list[PosterRender],
    active_task_ids: set[str],
) -> list[PosterRender]:
    if not active_task_ids:
        return renders
    return [
        render
        for render in renders
        if active_task_ids.intersection(
            task_id for task_id in (render.task_ids or []) if isinstance(task_id, str)
        )
    ]


def _advance_multi_size_step(
    run: WorkflowRun,
    multi_step: WorkflowStep,
    renders: list[PosterRender],
    assets: _GenerationAssets,
    active_task_ids: set[str],
    hooks: PosterSyncHooks,
) -> None:
    current_renders = _current_render_batch(renders, active_task_ids)
    raw_expected = int(
        (multi_step.input_json or {}).get("expected_render_count")
        or len(current_renders)
        or len(renders)
    )
    expected = min(raw_expected, len(current_renders) or raw_expected)
    multi_step.image_ids = hooks.dedupe_nonempty(
        render.image_id for render in renders if isinstance(render.image_id, str)
    )
    outcome = hooks.generation_batch_outcome(
        ready_count=sum(render.status == "ready" for render in current_renders),
        active_count=sum(
            render.status in {"generating", "revising"} for render in current_renders
        ),
        expected_count=expected,
    )
    failed_generations = _failed_generations(assets, active_task_ids)
    if outcome in {"complete", "partial"}:
        multi_step.status = "needs_review"
        run.current_step = "multi_size_generation"
        run.status = "needs_review"
        if outcome == "partial":
            multi_step.output_json = hooks.failed_generation_output(
                multi_step.output_json,
                failed_generations,
                fallback="部分多尺寸生成失败",
                partial=True,
            )
    elif outcome == "failed":
        multi_step.status = "failed"
        multi_step.output_json = hooks.failed_generation_output(
            multi_step.output_json,
            failed_generations,
            fallback="多尺寸生成失败",
            partial=False,
        )
        run.status = "failed"
        run.current_step = "multi_size_generation"


def _refresh_multi_size_image_ids(
    multi_step: WorkflowStep,
    renders: list[PosterRender],
    hooks: PosterSyncHooks,
) -> None:
    multi_step.image_ids = hooks.dedupe_nonempty(
        render.image_id for render in renders if isinstance(render.image_id, str)
    )


async def sync_render_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: PosterSyncHooks,
) -> None:
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
    if not renders:
        return
    task_ids = [task_id for render in renders for task_id in (render.task_ids or [])]
    assets = await _load_generation_assets(db, task_ids)
    multi_step = steps.get("multi_size_generation")
    active_task_ids = _active_render_task_ids(multi_step)
    for render in renders:
        _sync_render_state(render, assets, active_task_ids)
    if multi_step and multi_step.status == "running":
        _advance_multi_size_step(
            run,
            multi_step,
            renders,
            assets,
            active_task_ids,
            hooks,
        )
    elif multi_step and multi_step.status in {"needs_review", "completed"}:
        _refresh_multi_size_image_ids(multi_step, renders, hooks)
