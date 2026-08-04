"""Model-library standalone and project-candidate job aggregation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from .runtime import ModelLibraryRuntimeAdapter

if TYPE_CHECKING:
    from lumen_core.models import WorkflowRun, WorkflowStep
    from lumen_core.schemas import ApparelModelLibraryJobOut, ModelAgeSegment


async def replay_apparel_model_library_job(
    db: AsyncSession,
    *,
    workflow_run_id: str,
    user: Any,
    runtime: ModelLibraryRuntimeAdapter,
) -> ApparelModelLibraryJobOut:
    run = await runtime._get_run(
        db,
        user_id=user.id,
        run_id=workflow_run_id,
    )
    if run.type != runtime.WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE:
        raise runtime._http(
            "idempotency_conflict",
            "idempotency_key resolved to a different workflow operation",
            409,
        )
    await runtime._ensure_legacy_user_library_migrated(db, user.id)
    saved_map = await runtime._saved_image_id_set(db, user.id)
    job = await runtime._job_from_library_run(
        db,
        run=run,
        saved_map=saved_map,
    )
    await db.commit()
    return job


async def job_from_library_run(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    saved_map: dict[str, str],
    runtime: ModelLibraryRuntimeAdapter,
) -> ApparelModelLibraryJobOut:
    step = (
        await db.execute(
            runtime.select(runtime.WorkflowStep).where(
                runtime.WorkflowStep.workflow_run_id == run.id,
                runtime.WorkflowStep.step_key
                == runtime.MODEL_LIBRARY_GENERATE_STEP_KEY,
            )
        )
    ).scalar_one_or_none()
    inputs: dict[str, Any] = {}
    image_ids: list[str] = []
    requested = 0
    step_status = "queued"
    if step is not None:
        inputs = runtime._model_library_run_inputs(step)
        image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
        requested = max(
            inputs.get("count") or 0,
            len(step.task_ids or []),
            len(image_ids),
        )
        step_status = step.status
    finished = len(image_ids)
    bonus_ids = runtime._extract_bonus_ids(step, image_ids)
    image_out_map = await runtime._gather_job_image_outs(
        db,
        user_id=run.user_id,
        image_ids=image_ids + bonus_ids,
    )
    image_meta_map = await runtime._model_library_image_meta_by_id(
        db,
        user_id=run.user_id,
        image_ids=image_ids + bonus_ids,
    )
    tagging_results = (step.output_json or {}).get("tagging_results") if step else None
    tagging_map: dict[str, dict[str, Any]] = (
        tagging_results if isinstance(tagging_results, dict) else {}
    )
    items = [
        runtime._job_item_out(
            image_id=iid,
            image_out=image_out_map.get(iid),
            saved_item_id=saved_map.get(iid),
            age_segment=inputs.get("age_segment"),
            gender=(image_meta_map.get(iid) or {}).get("gender")
            or (tagging_map.get(iid) or {}).get("gender")
            or inputs.get("gender"),
            style_tags=runtime._clean_style_tags(
                [
                    *(inputs.get("style_tags") or []),
                    *((tagging_map.get(iid) or {}).get("style_tags") or []),
                ]
            ),
            appearance_direction=(tagging_map.get(iid) or {}).get(
                "appearance_direction"
            ),
            image_meta=image_meta_map.get(iid),
        )
        for iid in image_ids
    ]
    candidates = [
        runtime._job_item_out(
            image_id=bonus_id,
            image_out=image_out_map.get(bonus_id),
            saved_item_id=saved_map.get(bonus_id),
            age_segment=inputs.get("age_segment"),
            gender=(image_meta_map.get(bonus_id) or {}).get("gender")
            or inputs.get("gender"),
            style_tags=inputs.get("style_tags") or [],
            appearance_direction=inputs.get("appearance_direction"),
            image_meta=image_meta_map.get(bonus_id),
        )
        for bonus_id in bonus_ids
    ]
    error_message = None
    if step is not None:
        out_json = step.output_json if isinstance(step.output_json, dict) else {}
        error_message = runtime._clean_optional_text(
            out_json.get("error_message"),
            max_len=400,
        )
        task_generations = await runtime._workflow_generation_rows_from_task_ids(
            db,
            user_id=run.user_id,
            task_ids=list(step.task_ids or []),
            include_dual_bonus=False,
        )
        failed_generations = [
            generation
            for generation in task_generations
            if generation.status == runtime.GenerationStatus.FAILED.value
        ]
        active_generations = [
            generation
            for generation in task_generations
            if generation.status
            in {
                runtime.GenerationStatus.QUEUED.value,
                runtime.GenerationStatus.RUNNING.value,
            }
        ]
        if failed_generations and not active_generations and finished < requested:
            if step_status == "running":
                step_status = "failed"
            if error_message is None:
                error_message = runtime._clean_optional_text(
                    runtime._task_error_summary(
                        failed_generations,
                        "模特库生成失败",
                    ),
                    max_len=400,
                )
    job_status = runtime._model_library_job_status(
        step_status=step_status,
        requested_count=requested,
        finished_count=finished,
    )
    return runtime.ApparelModelLibraryJobOut(
        job_id=run.id,
        origin="library_generate",
        workflow_run_id=run.id,
        project_title=None,
        status=job_status,
        requested_count=requested,
        finished_count=finished,
        age_segment=inputs.get("age_segment"),
        gender=inputs.get("gender"),
        appearance_direction=inputs.get("appearance_direction"),
        extra_requirements=inputs.get("extra_requirements"),
        reference_image_id=inputs.get("reference_image_id"),
        reference_image_url=(
            runtime._image_url(inputs["reference_image_id"])
            if inputs.get("reference_image_id")
            else None
        ),
        extracted_profile=inputs.get("extracted_profile"),
        items=items,
        candidates=candidates,
        error_message=error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def job_from_project_candidate_step(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    saved_map: dict[str, str],
    runtime: ModelLibraryRuntimeAdapter,
) -> ApparelModelLibraryJobOut:
    image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
    requested_count = runtime.MODEL_CANDIDATE_COUNT
    raw_input = step.input_json if isinstance(step.input_json, dict) else {}
    candidate_count = raw_input.get("candidate_count")
    if isinstance(candidate_count, int) and candidate_count > 0:
        requested_count = candidate_count
    bonus_ids = runtime._extract_bonus_ids(step, image_ids)
    image_out_map = await runtime._gather_job_image_outs(
        db,
        user_id=run.user_id,
        image_ids=image_ids + bonus_ids,
    )
    image_meta_map = await runtime._model_library_image_meta_by_id(
        db,
        user_id=run.user_id,
        image_ids=image_ids + bonus_ids,
    )
    profile = (run.metadata_jsonb or {}).get("model_profile") or {}
    age_segment = (
        runtime._normalize_age_segment(profile.get("age_segment"))
        if isinstance(profile, dict)
        else None
    )
    gender = profile.get("gender") if isinstance(profile, dict) else None
    appearance_direction = (
        profile.get("appearance_direction") if isinstance(profile, dict) else None
    )
    items = [
        runtime._job_item_out(
            image_id=image_id,
            image_out=image_out_map.get(image_id),
            saved_item_id=saved_map.get(image_id),
            age_segment=age_segment,
            gender=gender,
            style_tags=[],
            appearance_direction=appearance_direction,
            image_meta=image_meta_map.get(image_id),
        )
        for image_id in image_ids
    ]
    candidates = [
        runtime._job_item_out(
            image_id=bonus_id,
            image_out=image_out_map.get(bonus_id),
            saved_item_id=saved_map.get(bonus_id),
            age_segment=age_segment,
            gender=gender,
            style_tags=[],
            appearance_direction=appearance_direction,
            image_meta=image_meta_map.get(bonus_id),
        )
        for bonus_id in bonus_ids
    ]
    out_json = step.output_json if isinstance(step.output_json, dict) else {}
    error_message = runtime._clean_optional_text(
        out_json.get("error_message"),
        max_len=400,
    )
    step_status = step.status
    task_generations = await runtime._workflow_generation_rows_from_task_ids(
        db,
        user_id=run.user_id,
        task_ids=list(step.task_ids or []),
        include_dual_bonus=False,
    )
    failed_generations = [
        generation
        for generation in task_generations
        if generation.status == runtime.GenerationStatus.FAILED.value
    ]
    active_generations = [
        generation
        for generation in task_generations
        if generation.status
        in {
            runtime.GenerationStatus.QUEUED.value,
            runtime.GenerationStatus.RUNNING.value,
        }
    ]
    if (
        failed_generations
        and not active_generations
        and len(image_ids) < requested_count
    ):
        if step_status == "running":
            step_status = "failed"
        if error_message is None:
            error_message = runtime._clean_optional_text(
                runtime._task_error_summary(
                    failed_generations,
                    "项目模特候选生成失败",
                ),
                max_len=400,
            )
    job_status = runtime._model_library_job_status(
        step_status=step_status,
        requested_count=requested_count,
        finished_count=len(image_ids),
    )
    return runtime.ApparelModelLibraryJobOut(
        job_id=f"{run.id}:model_candidates",
        origin="project_candidate",
        workflow_run_id=run.id,
        project_title=run.title,
        status=job_status,
        requested_count=requested_count,
        finished_count=len(image_ids),
        age_segment=cast("ModelAgeSegment | None", age_segment),
        gender=gender,
        appearance_direction=appearance_direction,
        extra_requirements=None,
        reference_image_id=None,
        reference_image_url=None,
        extracted_profile=None,
        items=items,
        candidates=candidates,
        error_message=error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
