from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import CompletionStatus
from lumen_core.models import (
    Completion,
    Image,
    QualityReport,
    WorkflowRun,
    WorkflowStep,
)

from .output_sync_steps import (
    OutputSyncHooks,
    sync_accessory_outputs,
    sync_model_candidate_outputs,
    sync_product_analysis_step,
    sync_showcase_outputs,
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
    hooks = OutputSyncHooks(
        model_candidate_count=MODEL_CANDIDATE_COUNT,
        candidate_generated_image_ids=_candidate_generated_image_ids,
        dedupe_nonempty=_dedupe_nonempty,
        failed_generation_output=_failed_generation_output,
        first_images_by_generation=_first_images_by_generation,
        generation_batch_outcome=_generation_batch_outcome,
        load_quality_reports=_load_quality_reports,
        merge_quality_summary_payload=_merge_quality_summary_payload,
        showcase_expected_image_count=_showcase_expected_image_count,
        sync_quality_reports_from_tasks=_sync_quality_reports_from_tasks,
        task_error_summary=_task_error_summary,
        try_parse_json_text=_try_parse_json_text,
    )
    await sync_product_analysis_step(db, run, steps, hooks)
    await sync_model_candidate_outputs(db, run, steps, hooks)
    await sync_accessory_outputs(db, run, steps, hooks)
    await sync_showcase_outputs(db, run, steps, hooks)
