from __future__ import annotations

from typing import Iterable

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

from .output_values import (  # noqa: F401
    MODEL_CANDIDATE_COUNT,
    PRODUCT_ANALYSIS_FIELDS,
    _candidate_generated_image_ids,
    _clamp_score,
    _coerce_string_list,
    _extract_jsonish_value,
    _failed_generation_output,
    _generation_batch_outcome,
    _merge_quality_summary_payload,
    _normalize_product_analysis_payload,
    _quality_payload_from_text,
    _quality_summary_payload,
    _showcase_expected_image_count,
    _task_error_summary,
    _try_parse_json_text,
)
from .serialization import _dedupe_nonempty


def _first_images_by_generation(images: Iterable[Image]) -> dict[str, Image]:
    images_by_generation: dict[str, Image] = {}
    for image in images:
        generation_id = image.owner_generation_id
        if generation_id and generation_id not in images_by_generation:
            images_by_generation[generation_id] = image
    return images_by_generation


async def _lock_workflow_run_for_sync(
    db: AsyncSession,
    run: WorkflowRun,
) -> WorkflowRun | None:
    if not isinstance(run, WorkflowRun):
        return run
    return (
        await db.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.user_id == run.user_id,
                WorkflowRun.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _load_steps_for_sync(
    db: AsyncSession,
    run_id: str,
) -> list[WorkflowStep]:
    return list(
        (
            await db.execute(
                select(WorkflowStep)
                .where(WorkflowStep.workflow_run_id == run_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )


async def _load_quality_reports(
    db: AsyncSession,
    run_id: str,
) -> list[QualityReport]:
    return list(
        (
            await db.execute(
                select(QualityReport)
                .where(QualityReport.workflow_run_id == run_id)
                .order_by(QualityReport.created_at.asc(), QualityReport.id.asc())
            )
        )
        .scalars()
        .all()
    )


async def _sync_quality_reports_from_tasks(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    quality_step: WorkflowStep,
) -> None:
    output_json = dict(quality_step.output_json or {})
    review_map = output_json.get("review_tasks")
    if not isinstance(review_map, dict) or not review_map:
        return
    existing_by_image = {
        image_id: report
        for image_id, report in (
            (
                report.image_id,
                report,
            )
            for report in await _load_quality_reports(db, run.id)
        )
    }
    task_ids = [
        task_id
        for task_id in review_map.values()
        if isinstance(task_id, str) and task_id
    ]
    if not task_ids:
        return
    completions = (
        (
            await db.execute(
                select(Completion).where(
                    Completion.id.in_(task_ids),
                    Completion.user_id == run.user_id,
                )
            )
        )
        .scalars()
        .all()
    )
    completion_by_id = {completion.id: completion for completion in completions}
    for image_id, raw_task_id in review_map.items():
        if not isinstance(image_id, str) or not isinstance(raw_task_id, str):
            continue
        completion = completion_by_id.get(raw_task_id)
        if completion is None:
            continue
        if completion.status == CompletionStatus.SUCCEEDED.value:
            payload = _quality_payload_from_text(completion.text)
        elif completion.status == CompletionStatus.FAILED.value:
            payload = {
                "overall_score": 0,
                "product_fidelity_score": 0,
                "model_consistency_score": 0,
                "aesthetic_score": 0,
                "artifact_score": 0,
                "issues_json": [
                    {
                        "severity": "high",
                        "type": "quality_review_failed",
                        "message": completion.error_message
                        or "Automatic quality review failed; revise or rerun before delivery.",
                    }
                ],
                "recommendation": "revise",
            }
        else:
            continue
        existing = existing_by_image.get(image_id)
        if existing is None:
            db.add(
                QualityReport(
                    workflow_run_id=run.id,
                    image_id=image_id,
                    **payload,
                )
            )
        else:
            existing.overall_score = payload["overall_score"]
            existing.product_fidelity_score = payload["product_fidelity_score"]
            existing.model_consistency_score = payload["model_consistency_score"]
            existing.aesthetic_score = payload["aesthetic_score"]
            existing.artifact_score = payload["artifact_score"]
            existing.issues_json = payload["issues_json"]
            existing.recommendation = payload["recommendation"]


async def _sync_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    locked_run = await _lock_workflow_run_for_sync(db, run)
    if locked_run is None:
        return
    run = locked_run
    steps = {step.step_key: step for step in await _load_steps_for_sync(db, run.id)}

    product_step = steps.get("product_analysis")
    if product_step and product_step.status == "running" and product_step.task_ids:
        completion = (
            await db.execute(
                select(Completion)
                .where(Completion.id.in_(product_step.task_ids))
                .order_by(desc(Completion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if completion is not None:
            if completion.status == CompletionStatus.SUCCEEDED.value:
                parsed = _try_parse_json_text(completion.text)
                product_step.output_json = parsed
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
    if candidates:
        all_candidate_task_ids = [
            task_id
            for candidate in candidates
            for task_id in (candidate.task_ids or [])
        ]
        images_by_gen: dict[str, Image] = {}
        gens_by_id: dict[str, Generation] = {}
        bonus_gen_ids_by_parent: dict[str, list[str]] = {}
        bonus_parent_by_gen: dict[str, str] = {}
        if all_candidate_task_ids:
            base_generations = (
                (
                    await db.execute(
                        select(Generation).where(
                            Generation.id.in_(all_candidate_task_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            bonus_generations = (
                (
                    await db.execute(
                        select(Generation)
                        .where(
                            Generation.user_id == run.user_id,
                            Generation.upstream_request[
                                "parent_generation_id"
                            ].astext.in_(all_candidate_task_ids),
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
            gens_by_id = {g.id: g for g in generations}
            for generation in bonus_generations:
                req = generation.upstream_request or {}
                parent_id = (
                    req.get("parent_generation_id") if isinstance(req, dict) else None
                )
                if isinstance(parent_id, str) and parent_id:
                    bonus_gen_ids_by_parent.setdefault(parent_id, []).append(
                        generation.id
                    )
                    bonus_parent_by_gen[generation.id] = parent_id
            images = (
                (
                    await db.execute(
                        select(Image)
                        .where(
                            Image.owner_generation_id.in_([g.id for g in generations]),
                            Image.deleted_at.is_(None),
                        )
                        .order_by(Image.created_at.asc(), Image.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            images_by_gen = _first_images_by_generation(images)
        for candidate in candidates:
            candidate_image_ids: list[str] = []
            for task_id in candidate.task_ids or []:
                candidate_image = images_by_gen.get(task_id)
                if candidate_image is not None:
                    candidate_image_ids.append(candidate_image.id)
            candidate_image_ids = _dedupe_nonempty(candidate_image_ids)
            if candidate_image_ids:
                brief = dict(candidate.model_brief_json or {})
                brief["candidate_image_ids"] = candidate_image_ids
                candidate.model_brief_json = brief
            if candidate.contact_sheet_image_id is None:
                if candidate_image_ids:
                    candidate.contact_sheet_image_id = candidate_image_ids[0]
            if candidate.contact_sheet_image_id and candidate.status == "generating":
                candidate.status = "ready"
            elif (
                candidate.status == "generating"
                and candidate.task_ids
                and all(
                    gens_by_id.get(task_id) is not None
                    and gens_by_id[task_id].status == GenerationStatus.FAILED.value
                    for task_id in candidate.task_ids
                )
            ):
                candidate.status = "failed"

        existing_bonus_gen_ids = {
            task_id
            for candidate in candidates
            for task_id in (candidate.task_ids or [])
            if task_id in bonus_parent_by_gen
        }
        next_index = max((c.candidate_index for c in candidates), default=0) + 1
        for parent_task_id, bonus_gen_ids in bonus_gen_ids_by_parent.items():
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
            for bonus_gen_id in bonus_gen_ids:
                if bonus_gen_id in existing_bonus_gen_ids:
                    continue
                bonus_image = images_by_gen.get(bonus_gen_id)
                if bonus_image is None:
                    continue
                brief = dict(parent_candidate.model_brief_json or {})
                brief["candidate_image_ids"] = [bonus_image.id]
                brief["source_candidate_id"] = parent_candidate.id
                brief["source_generation_id"] = parent_task_id
                brief["is_dual_race_bonus"] = True
                bonus_candidate = ModelCandidate(
                    workflow_run_id=run.id,
                    candidate_index=next_index,
                    status="ready",
                    contact_sheet_image_id=bonus_image.id,
                    model_brief_json=brief,
                    task_ids=[bonus_gen_id],
                )
                db.add(bonus_candidate)
                candidates.append(bonus_candidate)
                existing_bonus_gen_ids.add(bonus_gen_id)
                next_index += 1

        candidate_step = steps.get("model_candidates")
        if candidate_step and candidate_step.status == "running":
            ready_count = sum(1 for c in candidates if c.status == "ready")
            active_count = sum(1 for c in candidates if c.status == "generating")
            batch_outcome = _generation_batch_outcome(
                ready_count=ready_count,
                active_count=active_count,
                expected_count=MODEL_CANDIDATE_COUNT,
            )
            if batch_outcome in {"complete", "partial"}:
                candidate_step.status = "needs_review"
                candidate_step.image_ids = _dedupe_nonempty(
                    image_id
                    for c in candidates
                    for image_id in _candidate_generated_image_ids(c)
                )
                run.current_step = "model_approval"
                run.status = "needs_review"
                approval_step = steps.get("model_approval")
                if approval_step and approval_step.status == "waiting_input":
                    approval_step.status = "needs_review"
                if batch_outcome == "partial":
                    failed_generations = [
                        generation
                        for generation in gens_by_id.values()
                        if generation.status == GenerationStatus.FAILED.value
                    ]
                    candidate_step.output_json = _failed_generation_output(
                        candidate_step.output_json,
                        failed_generations,
                        fallback="部分模特候选生成失败",
                        partial=True,
                    )
            elif batch_outcome == "failed":
                candidate_step.status = "failed"
                failed_generations = [
                    generation
                    for generation in gens_by_id.values()
                    if generation.status == GenerationStatus.FAILED.value
                ]
                candidate_step.output_json = _failed_generation_output(
                    candidate_step.output_json,
                    failed_generations,
                    fallback="模特候选生成失败",
                    partial=False,
                )
                run.current_step = "model_candidates"
                run.status = "failed"

    showcase_step = steps.get("showcase_generation")
    quality_step = steps.get("quality_review")
    approval_step = steps.get("model_approval")
    if approval_step and approval_step.task_ids:
        accessory_base_generations = (
            (
                await db.execute(
                    select(Generation).where(Generation.id.in_(approval_step.task_ids))
                )
            )
            .scalars()
            .all()
        )
        accessory_bonus_generations = (
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == run.user_id,
                        Generation.upstream_request["parent_generation_id"].astext.in_(
                            approval_step.task_ids
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
        accessory_generations = [
            *accessory_base_generations,
            *accessory_bonus_generations,
        ]
        accessory_images = (
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_(
                            [generation.id for generation in accessory_generations]
                        ),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if accessory_images:
            approval_step.image_ids = _dedupe_nonempty(
                image.id for image in accessory_images
            )
            if approval_step.status == "running":
                approval_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "model_approval"
        else:
            failed = [
                generation
                for generation in accessory_generations
                if generation.status == GenerationStatus.FAILED.value
            ]
            active = [
                generation
                for generation in accessory_generations
                if generation.status
                in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
            ]
            if approval_step.status == "running" and failed and not active:
                output_json = dict(approval_step.output_json or {})
                output_json["failed_generation_ids"] = [g.id for g in failed]
                output_json["error_message"] = _task_error_summary(
                    failed,
                    "配饰四宫格生成失败",
                )
                approval_step.output_json = output_json
                approval_step.status = "failed"
                run.status = "failed"
                run.current_step = "model_approval"
    if showcase_step and showcase_step.task_ids:
        base_generations = (
            (
                await db.execute(
                    select(Generation).where(Generation.id.in_(showcase_step.task_ids))
                )
            )
            .scalars()
            .all()
        )
        bonus_generations = (
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == run.user_id,
                        Generation.upstream_request["parent_generation_id"].astext.in_(
                            showcase_step.task_ids
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
        images = (
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
        image_ids = _dedupe_nonempty(
            [
                *(showcase_step.image_ids or []),
                *(image.id for image in images),
            ]
        )
        if image_ids:
            showcase_step.image_ids = image_ids
        expected = _showcase_expected_image_count(
            showcase_input=showcase_step.input_json or {},
            fallback_task_count=len(showcase_step.task_ids),
        )
        succeeded = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.SUCCEEDED.value
        ]
        active = [
            generation
            for generation in generations
            if generation.status
            in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
        ]
        failed = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.FAILED.value
        ]
        canceled = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.CANCELED.value
        ]
        terminal_problems = [*failed, *canceled]
        has_enough_output_images = len(image_ids) >= expected
        if showcase_step.status in {"running", "failed"} and has_enough_output_images:
            showcase_step.status = "completed"
            if terminal_problems:
                output_json = dict(showcase_step.output_json or {})
                if failed:
                    output_json["failed_generation_ids"] = [g.id for g in failed]
                if canceled:
                    output_json["canceled_generation_ids"] = [g.id for g in canceled]
                output_json["succeeded_generation_ids"] = [g.id for g in succeeded]
                output_json["error_message"] = _task_error_summary(
                    terminal_problems,
                    "部分展示图生成失败或取消",
                )
                output_json["recovered_by_bonus_images"] = True
                showcase_step.output_json = output_json
            if quality_step:
                quality_step.status = "needs_review"
                quality_step.image_ids = image_ids
                reports = await _load_quality_reports(db, run.id)
                quality_step.output_json = _merge_quality_summary_payload(
                    quality_step.output_json,
                    reports,
                )
                run.current_step = "quality_review"
            else:
                run.current_step = "showcase_generation"
            run.status = "needs_review"
        elif showcase_step.status == "running" and terminal_problems and not active:
            showcase_step.status = "failed"
            output_json = {
                "succeeded_generation_ids": [g.id for g in succeeded],
                "error_message": _task_error_summary(
                    terminal_problems,
                    "展示图生成失败或取消",
                ),
            }
            if failed:
                output_json["failed_generation_ids"] = [g.id for g in failed]
            if canceled:
                output_json["canceled_generation_ids"] = [g.id for g in canceled]
            showcase_step.output_json = output_json
            run.status = "failed"
        elif showcase_step.status == "completed" and quality_step:
            quality_step.image_ids = image_ids
            await _sync_quality_reports_from_tasks(
                db,
                run=run,
                quality_step=quality_step,
            )
            reports = await _load_quality_reports(db, run.id)
            if (
                image_ids
                and len(reports) >= len(image_ids)
                and quality_step.status == "running"
            ):
                quality_step.status = "needs_review"
                run.status = "needs_review"
            quality_step.output_json = _merge_quality_summary_payload(
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
