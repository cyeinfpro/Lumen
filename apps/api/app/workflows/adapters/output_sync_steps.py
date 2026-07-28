"""Stage implementations for durable workflow output synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import CompletionStatus, GenerationStatus
from lumen_core.models import (
    Completion,
    Generation,
    Image,
    ModelCandidate,
    QualityReport,
    WorkflowRun,
    WorkflowStep,
)


@dataclass(frozen=True)
class OutputSyncHooks:
    model_candidate_count: int
    candidate_generated_image_ids: Callable[[ModelCandidate], list[str]]
    dedupe_nonempty: Callable[[Any], list[str]]
    failed_generation_output: Callable[..., dict[str, Any]]
    first_images_by_generation: Callable[[Any], dict[str, Image]]
    generation_batch_outcome: Callable[..., str]
    load_quality_reports: Callable[..., Awaitable[list[QualityReport]]]
    merge_quality_summary_payload: Callable[..., dict[str, Any]]
    showcase_expected_image_count: Callable[..., int]
    sync_quality_reports_from_tasks: Callable[..., Awaitable[None]]
    task_error_summary: Callable[..., str]
    try_parse_json_text: Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class _CandidateGenerationState:
    generations_by_id: dict[str, Generation]
    images_by_generation: dict[str, Image]
    bonus_generation_ids_by_parent: dict[str, list[str]]
    bonus_parent_by_generation: dict[str, str]


@dataclass(frozen=True)
class _GenerationOutcome:
    succeeded: list[Generation]
    active: list[Generation]
    failed: list[Generation]
    canceled: list[Generation]

    @property
    def terminal_problems(self) -> list[Generation]:
        return [*self.failed, *self.canceled]


async def sync_product_analysis_step(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: OutputSyncHooks,
) -> None:
    product_step = steps.get("product_analysis")
    if (
        not product_step
        or product_step.status != "running"
        or not product_step.task_ids
    ):
        return
    completion = (
        await db.execute(
            select(Completion)
            .where(Completion.id.in_(product_step.task_ids))
            .order_by(desc(Completion.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if completion is None:
        return
    if completion.status == CompletionStatus.SUCCEEDED.value:
        product_step.output_json = hooks.try_parse_json_text(completion.text)
        product_step.status = "needs_review"
        run.status = "needs_review"
        run.current_step = "product_analysis"
    elif completion.status == CompletionStatus.FAILED.value:
        product_step.status = "failed"
        product_step.output_json = {
            "error_code": completion.error_code,
            "error_message": completion.error_message,
        }
        run.status = "failed"


async def _load_candidate_generation_state(
    db: AsyncSession,
    run: WorkflowRun,
    candidates: list[ModelCandidate],
    hooks: OutputSyncHooks,
) -> _CandidateGenerationState:
    task_ids = [
        task_id for candidate in candidates for task_id in (candidate.task_ids or [])
    ]
    if not task_ids:
        return _CandidateGenerationState({}, {}, {}, {})
    base_generations = list(
        (await db.execute(select(Generation).where(Generation.id.in_(task_ids))))
        .scalars()
        .all()
    )
    bonus_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == run.user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    generations = [*base_generations, *bonus_generations]
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
    bonus_ids_by_parent: dict[str, list[str]] = {}
    bonus_parent_by_generation: dict[str, str] = {}
    for generation in bonus_generations:
        request = generation.upstream_request or {}
        parent_id = (
            request.get("parent_generation_id") if isinstance(request, dict) else None
        )
        if isinstance(parent_id, str) and parent_id:
            bonus_ids_by_parent.setdefault(parent_id, []).append(generation.id)
            bonus_parent_by_generation[generation.id] = parent_id
    return _CandidateGenerationState(
        generations_by_id={generation.id: generation for generation in generations},
        images_by_generation=hooks.first_images_by_generation(images),
        bonus_generation_ids_by_parent=bonus_ids_by_parent,
        bonus_parent_by_generation=bonus_parent_by_generation,
    )


def _candidate_image_ids(
    candidate: ModelCandidate,
    state: _CandidateGenerationState,
    hooks: OutputSyncHooks,
) -> list[str]:
    return hooks.dedupe_nonempty(
        image.id
        for task_id in (candidate.task_ids or [])
        if (image := state.images_by_generation.get(task_id)) is not None
    )


def _refresh_candidate_states(
    candidates: list[ModelCandidate],
    state: _CandidateGenerationState,
    hooks: OutputSyncHooks,
) -> None:
    for candidate in candidates:
        image_ids = _candidate_image_ids(candidate, state, hooks)
        if image_ids:
            brief = dict(candidate.model_brief_json or {})
            brief["candidate_image_ids"] = image_ids
            candidate.model_brief_json = brief
        if candidate.contact_sheet_image_id is None and image_ids:
            candidate.contact_sheet_image_id = image_ids[0]
        if candidate.contact_sheet_image_id and candidate.status == "generating":
            candidate.status = "ready"
        elif (
            candidate.status == "generating"
            and candidate.task_ids
            and all(
                state.generations_by_id.get(task_id) is not None
                and state.generations_by_id[task_id].status
                == GenerationStatus.FAILED.value
                for task_id in candidate.task_ids
            )
        ):
            candidate.status = "failed"


def _materialize_bonus_candidates(
    db: AsyncSession,
    run: WorkflowRun,
    candidates: list[ModelCandidate],
    state: _CandidateGenerationState,
) -> None:
    existing_bonus_ids = {
        task_id
        for candidate in candidates
        for task_id in (candidate.task_ids or [])
        if task_id in state.bonus_parent_by_generation
    }
    next_index = (
        max(
            (candidate.candidate_index for candidate in candidates),
            default=0,
        )
        + 1
    )
    for parent_task_id, bonus_ids in state.bonus_generation_ids_by_parent.items():
        parent_candidate = next(
            (
                candidate
                for candidate in candidates
                if parent_task_id in (candidate.task_ids or [])
            ),
            None,
        )
        if parent_candidate is None:
            continue
        for bonus_id in bonus_ids:
            if bonus_id in existing_bonus_ids:
                continue
            bonus_image = state.images_by_generation.get(bonus_id)
            if bonus_image is None:
                continue
            brief = dict(parent_candidate.model_brief_json or {})
            brief.update(
                {
                    "candidate_image_ids": [bonus_image.id],
                    "source_candidate_id": parent_candidate.id,
                    "source_generation_id": parent_task_id,
                    "is_dual_race_bonus": True,
                }
            )
            bonus_candidate = ModelCandidate(
                workflow_run_id=run.id,
                candidate_index=next_index,
                status="ready",
                contact_sheet_image_id=bonus_image.id,
                model_brief_json=brief,
                task_ids=[bonus_id],
            )
            db.add(bonus_candidate)
            candidates.append(bonus_candidate)
            existing_bonus_ids.add(bonus_id)
            next_index += 1


def _failed_candidate_generations(
    state: _CandidateGenerationState,
) -> list[Generation]:
    return [
        generation
        for generation in state.generations_by_id.values()
        if generation.status == GenerationStatus.FAILED.value
    ]


def _advance_candidate_step(
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    candidates: list[ModelCandidate],
    state: _CandidateGenerationState,
    hooks: OutputSyncHooks,
) -> None:
    candidate_step = steps.get("model_candidates")
    if not candidate_step or candidate_step.status != "running":
        return
    outcome = hooks.generation_batch_outcome(
        ready_count=sum(candidate.status == "ready" for candidate in candidates),
        active_count=sum(candidate.status == "generating" for candidate in candidates),
        expected_count=hooks.model_candidate_count,
    )
    failed_generations = _failed_candidate_generations(state)
    if outcome in {"complete", "partial"}:
        candidate_step.status = "needs_review"
        candidate_step.image_ids = hooks.dedupe_nonempty(
            image_id
            for candidate in candidates
            for image_id in hooks.candidate_generated_image_ids(candidate)
        )
        run.current_step = "model_approval"
        run.status = "needs_review"
        approval_step = steps.get("model_approval")
        if approval_step and approval_step.status == "waiting_input":
            approval_step.status = "needs_review"
        if outcome == "partial":
            candidate_step.output_json = hooks.failed_generation_output(
                candidate_step.output_json,
                failed_generations,
                fallback="部分模特候选生成失败",
                partial=True,
            )
    elif outcome == "failed":
        candidate_step.status = "failed"
        candidate_step.output_json = hooks.failed_generation_output(
            candidate_step.output_json,
            failed_generations,
            fallback="模特候选生成失败",
            partial=False,
        )
        run.current_step = "model_candidates"
        run.status = "failed"


async def sync_model_candidate_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: OutputSyncHooks,
) -> None:
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return
    state = await _load_candidate_generation_state(db, run, candidates, hooks)
    _refresh_candidate_states(candidates, state, hooks)
    _materialize_bonus_candidates(db, run, candidates, state)
    _advance_candidate_step(run, steps, candidates, state, hooks)


async def _load_generations_with_bonus(
    db: AsyncSession,
    run: WorkflowRun,
    task_ids: list[str],
) -> list[Generation]:
    base_generations = list(
        (await db.execute(select(Generation).where(Generation.id.in_(task_ids))))
        .scalars()
        .all()
    )
    bonus_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == run.user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return [*base_generations, *bonus_generations]


async def _load_generation_images(
    db: AsyncSession,
    generations: list[Generation],
) -> list[Image]:
    return list(
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


async def sync_accessory_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: OutputSyncHooks,
) -> None:
    approval_step = steps.get("model_approval")
    if not approval_step or not approval_step.task_ids:
        return
    generations = await _load_generations_with_bonus(
        db,
        run,
        approval_step.task_ids,
    )
    images = await _load_generation_images(db, generations)
    if images:
        approval_step.image_ids = hooks.dedupe_nonempty(image.id for image in images)
        if approval_step.status == "running":
            approval_step.status = "needs_review"
            run.status = "needs_review"
            run.current_step = "model_approval"
        return
    failed = [
        generation
        for generation in generations
        if generation.status == GenerationStatus.FAILED.value
    ]
    active = [
        generation
        for generation in generations
        if generation.status
        in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
    ]
    if approval_step.status != "running" or not failed or active:
        return
    output_json = dict(approval_step.output_json or {})
    output_json["failed_generation_ids"] = [generation.id for generation in failed]
    output_json["error_message"] = hooks.task_error_summary(
        failed,
        "配饰四宫格生成失败",
    )
    approval_step.output_json = output_json
    approval_step.status = "failed"
    run.status = "failed"
    run.current_step = "model_approval"


def _generation_outcome(
    generations: list[Generation],
) -> _GenerationOutcome:
    return _GenerationOutcome(
        succeeded=[
            generation
            for generation in generations
            if generation.status == GenerationStatus.SUCCEEDED.value
        ],
        active=[
            generation
            for generation in generations
            if generation.status
            in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
        ],
        failed=[
            generation
            for generation in generations
            if generation.status == GenerationStatus.FAILED.value
        ],
        canceled=[
            generation
            for generation in generations
            if generation.status == GenerationStatus.CANCELED.value
        ],
    )


async def _complete_showcase_step(
    db: AsyncSession,
    run: WorkflowRun,
    showcase_step: WorkflowStep,
    quality_step: WorkflowStep | None,
    image_ids: list[str],
    outcome: _GenerationOutcome,
    hooks: OutputSyncHooks,
) -> None:
    showcase_step.status = "completed"
    if outcome.terminal_problems:
        output_json = dict(showcase_step.output_json or {})
        if outcome.failed:
            output_json["failed_generation_ids"] = [
                generation.id for generation in outcome.failed
            ]
        if outcome.canceled:
            output_json["canceled_generation_ids"] = [
                generation.id for generation in outcome.canceled
            ]
        output_json["succeeded_generation_ids"] = [
            generation.id for generation in outcome.succeeded
        ]
        output_json["error_message"] = hooks.task_error_summary(
            outcome.terminal_problems,
            "部分展示图生成失败或取消",
        )
        output_json["recovered_by_bonus_images"] = True
        showcase_step.output_json = output_json
    if quality_step:
        quality_step.status = "needs_review"
        quality_step.image_ids = image_ids
        reports = await hooks.load_quality_reports(db, run.id)
        quality_step.output_json = hooks.merge_quality_summary_payload(
            quality_step.output_json,
            reports,
        )
        run.current_step = "quality_review"
    else:
        run.current_step = "showcase_generation"
    run.status = "needs_review"


def _fail_showcase_step(
    run: WorkflowRun,
    showcase_step: WorkflowStep,
    outcome: _GenerationOutcome,
    hooks: OutputSyncHooks,
) -> None:
    showcase_step.status = "failed"
    output_json = {
        "succeeded_generation_ids": [generation.id for generation in outcome.succeeded],
        "error_message": hooks.task_error_summary(
            outcome.terminal_problems,
            "展示图生成失败或取消",
        ),
    }
    if outcome.failed:
        output_json["failed_generation_ids"] = [
            generation.id for generation in outcome.failed
        ]
    if outcome.canceled:
        output_json["canceled_generation_ids"] = [
            generation.id for generation in outcome.canceled
        ]
    showcase_step.output_json = output_json
    run.status = "failed"


async def _sync_completed_showcase_quality(
    db: AsyncSession,
    run: WorkflowRun,
    quality_step: WorkflowStep,
    image_ids: list[str],
    hooks: OutputSyncHooks,
) -> None:
    quality_step.image_ids = image_ids
    await hooks.sync_quality_reports_from_tasks(
        db,
        run=run,
        quality_step=quality_step,
    )
    reports = await hooks.load_quality_reports(db, run.id)
    if (
        image_ids
        and len(reports) >= len(image_ids)
        and quality_step.status == "running"
    ):
        quality_step.status = "needs_review"
        run.status = "needs_review"
    quality_step.output_json = hooks.merge_quality_summary_payload(
        quality_step.output_json,
        reports,
    )
    if (
        image_ids
        and quality_step.status in {"waiting_input", "running", "needs_review"}
        and run.status != "completed"
        and run.current_step == "showcase_generation"
    ):
        quality_step.status = "needs_review"
        run.current_step = "quality_review"
        run.status = "needs_review"


async def sync_showcase_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    steps: dict[str, WorkflowStep],
    hooks: OutputSyncHooks,
) -> None:
    showcase_step = steps.get("showcase_generation")
    if not showcase_step or not showcase_step.task_ids:
        return
    quality_step = steps.get("quality_review")
    generations = await _load_generations_with_bonus(
        db,
        run,
        showcase_step.task_ids,
    )
    images = await _load_generation_images(db, generations)
    image_ids = hooks.dedupe_nonempty(
        [
            *(showcase_step.image_ids or []),
            *(image.id for image in images),
        ]
    )
    if image_ids:
        showcase_step.image_ids = image_ids
    expected = hooks.showcase_expected_image_count(
        showcase_input=showcase_step.input_json or {},
        fallback_task_count=len(showcase_step.task_ids),
    )
    outcome = _generation_outcome(generations)
    if showcase_step.status in {"running", "failed"} and len(image_ids) >= expected:
        await _complete_showcase_step(
            db,
            run,
            showcase_step,
            quality_step,
            image_ids,
            outcome,
            hooks,
        )
    elif (
        showcase_step.status == "running"
        and outcome.terminal_problems
        and not outcome.active
    ):
        _fail_showcase_step(run, showcase_step, outcome, hooks)
    elif showcase_step.status == "completed" and quality_step:
        await _sync_completed_showcase_quality(
            db,
            run,
            quality_step,
            image_ids,
            hooks,
        )
