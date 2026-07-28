"""Standalone model-library generation, job aggregation, and auto-tag routes."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, cast

from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import GenerationStatus, Intent
from lumen_core.models import (
    Conversation,
    Generation,
    Image,
    ModelLibraryItem,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.model_image_metadata import parse_model_image_metadata
from lumen_core.schemas import (
    ApparelModelLibraryAutoTagOut,
    ApparelModelLibraryGenerateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryJobItemOut,
    ApparelModelLibraryJobOut,
    ApparelModelLibraryJobsClearOut,
    ApparelModelLibraryJobsOut,
    ApparelModelLibrarySaveJobItemIn,
    ImageOut,
    ImageParamsIn,
    ModelAgeSegment,
)
from ....deps import CurrentUser
from ....observability import (
    apparel_model_library_generate_mode_total,
    apparel_model_library_reference_extract_total,
)
from ...application import model_library_tasks as model_library_task_application
from ...application.errors import WorkflowRequestError
from ...application.model_library_generation import (
    model_library_gender_label as _model_library_gender_label,
    model_library_generate_prompt as application_model_library_generate_prompt,
    model_library_job_status as _model_library_job_status,
    model_library_run_title as application_model_library_run_title,
)
from ...application.model_library_jobs import (
    ModelLibraryJobItemValues,
    ReferenceProfileValues,
    clean_optional_text as _clean_optional_text,
    clean_style_tags as _clean_style_tags,
    dedupe_nonempty as _dedupe_nonempty,
    extract_bonus_image_ids,
    merge_reference_overrides as merge_model_library_reference_overrides,
    model_library_explicit_genders as explicit_model_library_genders,
    model_library_generate_genders as generate_model_library_genders,
    normalize_model_library_run_inputs,
    reference_profile_has_required_fields,
    resolve_model_library_job_item,
)
from ...application.model_library_tagging import (
    ModelLibraryTagItemMissingImage,
    ModelLibraryTagItemNotFound,
    auto_tag_model_library_item,
)
from ...application.output_values import task_error_summary as _task_error_summary
from ...domain.apparel_library import (
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_GENERATE_COUNTS,
    MODEL_LIBRARY_GENERATE_STEP_KEY,
    MODEL_LIBRARY_GENERATE_WORKER_ACTION,
    WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
    model_library_folder_for_age as _model_library_folder_for_age,
    normalize_age_segment as _normalize_age_segment,
)
from ...domain.showcase_model_policy import (
    model_diversity_anchor as _model_diversity_anchor,
)
from ...domain.workflow_contracts import PublishBundle
from ...ports.model_library_tagging import (
    ModelLibraryTagItem,
    ModelLibraryTagUpdate,
    ModelLibraryTaggingPort,
)
from ...ports.model_library_tasks import (
    ModelLibraryGenerationTask,
    ModelLibraryTaskPort,
    ModelLibraryTaskResult,
)
from ..apparel_library_reference import (
    ReferenceProfile,
    auto_tag_owned_model_library_image,
    extract_reference_profile,
)
from ..library_items import (
    ensure_legacy_user_library_migrated as _ensure_legacy_user_library_migrated,
    model_library_item_out as _model_library_item_out,
)
from ..library_materialization import (
    add_user_library_item as _add_user_library_item,
    image_url as _image_url,
)
from ..output_sync import MODEL_CANDIDATE_COUNT
from ..serialization import http as _http, now as _now
from ..showcase_inputs import validate_owned_images as _validate_owned_images
from ..workflow_runtime import (
    create_workflow_task as _create_workflow_task,
    get_or_create_workflow_conversation as _get_or_create_workflow_conversation,
    get_run as _get_run,
    image_out_map as _image_out_map,
    image_params as _image_params,
    load_steps as _load_steps,
    post_commit_workflow_generated_cleanup as _post_commit_workflow_generated_cleanup,
    publish_bundles as _publish_bundles,
    soft_delete_workflow_generated_images as _soft_delete_workflow_generated_images,
    workflow_generation_rows_from_task_ids as _workflow_generation_rows_from_task_ids,
)


logger = logging.getLogger("app.routes.workflows.model_library")
WORKFLOW_TYPE = "apparel_model_showcase"


# ---------------------------------------------------------------------------
# 模特库独立生成 + 任务中心聚合 + vision 自动打标签
#
# 设计要点：
# 1. 每次"生成 N 张模特"请求 = 一条隐藏的 WorkflowRun(type=
#    apparel_model_library_generate) + 1 个 step(step_key=
#    model_library_generate) + N 个 worker generation task。每张产出一张独立
#    模特肖像；不创建 ModelCandidate（不和项目里的"候选 4 视图"逻辑混淆）。
# 2. 任务中心同时聚合两类来源：模特库独立生成 + 项目里的 model_candidates
#    step（origin 字段区分）。
# 3. 自动打标签走 worker tasks/model_library_tagging.py 调 vision provider；
#    解析失败 graceful，不影响主流程。
# ---------------------------------------------------------------------------
def _model_library_generate_genders(body: ApparelModelLibraryGenerateIn) -> list[str]:
    return generate_model_library_genders(
        getattr(body, "genders", None) or [],
        body.gender,
    )


def _model_library_run_title(
    *,
    age_segment: str | None,
    gender: str | None = None,
    genders: list[str] | None = None,
    appearance_direction: str | None,
    mode: str = "text",
) -> str:
    return application_model_library_run_title(
        age_segment=age_segment,
        gender=gender,
        genders=genders,
        appearance_direction=appearance_direction,
        mode=mode,
        gender_label=_model_library_gender_label,
    )


def _model_library_generate_prompt(
    *,
    age_segment: str,
    gender: str,
    appearance_direction: str | None,
    extra_requirements: str | None,
    style_tags: list[str],
    candidate_index: int,
    reference_mode: bool = False,
) -> str:
    return application_model_library_generate_prompt(
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        extra_requirements=extra_requirements,
        style_tags=style_tags,
        candidate_index=candidate_index,
        reference_mode=reference_mode,
        clean_style_tags=_clean_style_tags,
        model_diversity_anchor=_model_diversity_anchor,
    )


def _model_library_generate_image_params() -> ImageParamsIn:
    """模特库独立生成 2x2 contact sheet：4:5 跟项目候选一致，PNG 高质量。"""
    params = _image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        fast=False,
    )
    return params.model_copy(
        update={"output_format": "png", "output_compression": None}
    )


def _model_library_run_inputs(step: WorkflowStep) -> dict[str, Any]:
    raw = step.input_json if isinstance(step.input_json, dict) else {}
    return normalize_model_library_run_inputs(
        raw,
        task_count=len(step.task_ids or []),
    ).as_dict()


async def _saved_image_id_set(db: AsyncSession, user_id: str) -> dict[str, str]:
    """{ image_id -> library_item_id } map: 看哪些图已经收藏到当前用户的库。"""
    rows = (
        await db.execute(
            select(ModelLibraryItem.image_id, ModelLibraryItem.id)
            .where(ModelLibraryItem.user_id == user_id)
            .order_by(ModelLibraryItem.created_at.asc())
        )
    ).all()
    out: dict[str, str] = {}
    for image_id, item_id in rows:
        if not image_id or not item_id:
            continue
        out.setdefault(str(image_id), str(item_id))
    return out


async def _gather_job_image_outs(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
) -> dict[str, ImageOut]:
    if not image_ids:
        return {}
    images = list(
        (
            await db.execute(
                select(Image).where(
                    Image.id.in_(image_ids),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return await _image_out_map(db, images)


async def _model_library_image_meta_by_id(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
) -> dict[str, dict[str, Any]]:
    ids = _dedupe_nonempty(image_ids)
    if not ids:
        return {}
    images = list(
        (
            await db.execute(
                select(Image).where(
                    Image.id.in_(ids),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    gen_ids = _dedupe_nonempty(image.owner_generation_id or "" for image in images)
    generation_req: dict[str, dict[str, Any]] = {}
    if gen_ids:
        generations = list(
            (
                await db.execute(
                    select(Generation).where(
                        Generation.id.in_(gen_ids),
                        Generation.user_id == user_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        generation_req = {
            generation.id: dict(generation.upstream_request or {})
            for generation in generations
            if isinstance(generation.upstream_request, dict)
        }
    out: dict[str, dict[str, Any]] = {}
    for image in images:
        meta: dict[str, Any] = {"mime": image.mime}
        stored = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
        parsed = parse_model_image_metadata(stored.get("model_library"))
        if parsed is not None:
            meta.update(
                {
                    "age_segment": parsed.age_segment,
                    "gender": parsed.gender,
                    "appearance_direction": parsed.appearance_direction,
                    "style_tags": list(parsed.style_tags or []),
                    "prompt_hint": parsed.prompt_hint,
                }
            )
        filename = _clean_optional_text(stored.get("suggested_filename"), max_len=160)
        if filename:
            meta["download_filename"] = filename
        for key in (
            "is_dual_race_bonus",
            "billing_free",
            "billing_label",
            "billing_exempt_reason",
        ):
            if key in stored:
                meta[key] = stored[key]
        req = generation_req.get(image.owner_generation_id or "", {})
        if req:
            for key in (
                "is_dual_race_bonus",
                "billing_free",
                "billing_label",
                "billing_exempt_reason",
            ):
                if key in req and key not in meta:
                    meta[key] = req[key]
            if not meta.get("age_segment"):
                meta["age_segment"] = _clean_optional_text(
                    req.get("workflow_model_library_age_segment"), max_len=32
                )
            if not meta.get("gender"):
                meta["gender"] = _clean_optional_text(
                    req.get("workflow_model_library_gender"), max_len=16
                )
            if not meta.get("appearance_direction"):
                meta["appearance_direction"] = _clean_optional_text(
                    req.get("workflow_model_library_appearance_direction"),
                    max_len=80,
                )
            if not meta.get("style_tags"):
                meta["style_tags"] = _clean_style_tags(
                    req.get("workflow_model_library_style_tags") or []
                )
        out[image.id] = meta
    return out


def _job_item_out(
    *,
    image_id: str,
    image_out: ImageOut | None,
    saved_item_id: str | None,
    age_segment: str | None,
    gender: str | None,
    style_tags: list[str],
    appearance_direction: str | None,
    image_meta: dict[str, Any] | None = None,
) -> ApparelModelLibraryJobItemOut:
    resolved = resolve_model_library_job_item(
        ModelLibraryJobItemValues(
            image_id=image_id,
            image_url=(
                image_out.url if image_out is not None else _image_url(image_id)
            ),
            display_url=(image_out.display_url if image_out is not None else None),
            thumb_url=(image_out.thumb_url if image_out is not None else None),
            mime=(image_out.mime if image_out is not None else None),
            saved_item_id=saved_item_id,
            age_segment=age_segment,
            gender=gender,
            style_tags=style_tags,
            appearance_direction=appearance_direction,
            image_meta=image_meta or {},
            image_is_dual_race_bonus=bool(
                getattr(image_out, "is_dual_race_bonus", False)
                if image_out is not None
                else False
            ),
            image_billing_free=bool(
                getattr(image_out, "billing_free", False)
                if image_out is not None
                else False
            ),
            image_billing_label=(
                getattr(image_out, "billing_label", None)
                if image_out is not None
                else None
            ),
            image_billing_exempt_reason=(
                getattr(image_out, "billing_exempt_reason", None)
                if image_out is not None
                else None
            ),
        ),
    )
    return ApparelModelLibraryJobItemOut(
        image_id=resolved.image_id,
        image_url=resolved.image_url,
        display_url=resolved.display_url,
        thumb_url=resolved.thumb_url,
        saved_item_id=resolved.saved_item_id,
        style_tags=list(resolved.style_tags),
        appearance_direction=resolved.appearance_direction,
        gender=resolved.gender,
        download_filename=resolved.download_filename,
        is_dual_race_bonus=resolved.is_dual_race_bonus,
        billing_free=resolved.billing_free,
        billing_label=resolved.billing_label,
        billing_exempt_reason=resolved.billing_exempt_reason,
    )


def _extract_bonus_ids(
    step: WorkflowStep | None, image_ids: Iterable[str]
) -> list[str]:
    if step is None:
        return []
    output = step.output_json if isinstance(step.output_json, dict) else {}
    return extract_bonus_image_ids(output, image_ids)


async def _workflow_produced_model_image_ids(
    db: AsyncSession,
    *,
    user_id: str,
    steps: list[WorkflowStep],
) -> set[str]:
    """Image ids produced by a model workflow, including dual_race bonus outputs."""
    produced = {
        iid
        for step in steps
        for iid in (step.image_ids or [])
        if isinstance(iid, str) and iid
    }
    for step in steps:
        produced.update(_extract_bonus_ids(step, produced))
    all_task_ids = _dedupe_nonempty(
        task_id for step in steps for task_id in (step.task_ids or [])
    )
    if not all_task_ids:
        return produced
    owned = (
        (
            await db.execute(
                select(Image.id).where(
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                    or_(
                        Image.owner_generation_id.in_(all_task_ids),
                        Image.owner_generation_id.in_(
                            select(Generation.id).where(
                                Generation.user_id == user_id,
                                Generation.upstream_request[
                                    "parent_generation_id"
                                ].astext.in_(all_task_ids),
                                Generation.upstream_request["is_dual_race_bonus"]
                                .as_boolean()
                                .is_(True),
                            )
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    produced.update(iid for iid in owned if isinstance(iid, str) and iid)
    return produced


async def _job_from_library_run(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    saved_map: dict[str, str],
) -> ApparelModelLibraryJobOut:
    step = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == MODEL_LIBRARY_GENERATE_STEP_KEY,
            )
        )
    ).scalar_one_or_none()
    inputs: dict[str, Any] = {}
    image_ids: list[str] = []
    requested = 0
    step_status = "queued"
    if step is not None:
        inputs = _model_library_run_inputs(step)
        image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
        requested = max(
            inputs.get("count") or 0,
            len(step.task_ids or []),
            len(image_ids),
        )
        step_status = step.status
    finished = len(image_ids)
    # dual_race loser 写回的 bonus image_ids（与 winner image_ids 物理隔离）
    bonus_ids = _extract_bonus_ids(step, image_ids)
    # 一次查询拿到 winner + bonus 全部 image meta，省一次 DB roundtrip
    image_out_map = await _gather_job_image_outs(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    image_meta_map = await _model_library_image_meta_by_id(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    tagging_results = (step.output_json or {}).get("tagging_results") if step else None
    tagging_map: dict[str, dict[str, Any]] = (
        tagging_results if isinstance(tagging_results, dict) else {}
    )
    items = [
        _job_item_out(
            image_id=iid,
            image_out=image_out_map.get(iid),
            saved_item_id=saved_map.get(iid),
            age_segment=inputs.get("age_segment"),
            gender=(image_meta_map.get(iid) or {}).get("gender")
            or (tagging_map.get(iid) or {}).get("gender")
            or inputs.get("gender"),
            style_tags=_clean_style_tags(
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
    # candidate（loser）不跑 tagging，但可沿用任务元信息手动入库。
    candidates = [
        _job_item_out(
            image_id=bid,
            image_out=image_out_map.get(bid),
            saved_item_id=saved_map.get(bid),
            age_segment=inputs.get("age_segment"),
            gender=(image_meta_map.get(bid) or {}).get("gender")
            or inputs.get("gender"),
            style_tags=inputs.get("style_tags") or [],
            appearance_direction=inputs.get("appearance_direction"),
            image_meta=image_meta_map.get(bid),
        )
        for bid in bonus_ids
    ]
    error_message = None
    if step is not None:
        out_json = step.output_json if isinstance(step.output_json, dict) else {}
        error_message = _clean_optional_text(out_json.get("error_message"), max_len=400)
        task_generations = await _workflow_generation_rows_from_task_ids(
            db,
            user_id=run.user_id,
            task_ids=list(step.task_ids or []),
            include_dual_bonus=False,
        )
        failed_generations = [
            generation
            for generation in task_generations
            if generation.status == GenerationStatus.FAILED.value
        ]
        active_generations = [
            generation
            for generation in task_generations
            if generation.status
            in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
        ]
        if failed_generations and not active_generations and finished < requested:
            if step_status == "running":
                step_status = "failed"
            if error_message is None:
                error_message = _clean_optional_text(
                    _task_error_summary(failed_generations, "模特库生成失败"),
                    max_len=400,
                )
    job_status = _model_library_job_status(
        step_status=step_status,
        requested_count=requested,
        finished_count=finished,
    )
    return ApparelModelLibraryJobOut(
        job_id=run.id,
        origin="library_generate",
        workflow_run_id=run.id,
        project_title=None,
        status=job_status,  # type: ignore[arg-type]
        requested_count=requested,
        finished_count=finished,
        age_segment=inputs.get("age_segment"),
        gender=inputs.get("gender"),
        appearance_direction=inputs.get("appearance_direction"),
        extra_requirements=inputs.get("extra_requirements"),
        reference_image_id=inputs.get("reference_image_id"),
        reference_image_url=(
            _image_url(inputs["reference_image_id"])
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


async def _job_from_project_candidate_step(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    saved_map: dict[str, str],
) -> ApparelModelLibraryJobOut:
    image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
    requested_count = MODEL_CANDIDATE_COUNT
    raw_input = step.input_json if isinstance(step.input_json, dict) else {}
    candidate_count = raw_input.get("candidate_count")
    if isinstance(candidate_count, int) and candidate_count > 0:
        requested_count = candidate_count
    # dual_race loser 写回的 bonus image_ids（如该 origin 也走 dual_race）
    bonus_ids = _extract_bonus_ids(step, image_ids)
    image_out_map = await _gather_job_image_outs(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    image_meta_map = await _model_library_image_meta_by_id(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    profile = (run.metadata_jsonb or {}).get("model_profile") or {}
    age_segment = (
        _normalize_age_segment(profile.get("age_segment"))
        if isinstance(profile, dict)
        else None
    )
    gender = profile.get("gender") if isinstance(profile, dict) else None
    appearance_direction = (
        profile.get("appearance_direction") if isinstance(profile, dict) else None
    )
    items = [
        _job_item_out(
            image_id=iid,
            image_out=image_out_map.get(iid),
            saved_item_id=saved_map.get(iid),
            age_segment=age_segment,
            gender=gender,
            style_tags=[],
            appearance_direction=appearance_direction,
            image_meta=image_meta_map.get(iid),
        )
        for iid in image_ids
    ]
    candidates = [
        _job_item_out(
            image_id=bid,
            image_out=image_out_map.get(bid),
            saved_item_id=saved_map.get(bid),
            age_segment=age_segment,
            gender=gender,
            style_tags=[],
            appearance_direction=appearance_direction,
            image_meta=image_meta_map.get(bid),
        )
        for bid in bonus_ids
    ]
    out_json = step.output_json if isinstance(step.output_json, dict) else {}
    error_message = _clean_optional_text(out_json.get("error_message"), max_len=400)
    step_status = step.status
    task_generations = await _workflow_generation_rows_from_task_ids(
        db,
        user_id=run.user_id,
        task_ids=list(step.task_ids or []),
        include_dual_bonus=False,
    )
    failed_generations = [
        generation
        for generation in task_generations
        if generation.status == GenerationStatus.FAILED.value
    ]
    active_generations = [
        generation
        for generation in task_generations
        if generation.status
        in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
    ]
    if (
        failed_generations
        and not active_generations
        and len(image_ids) < requested_count
    ):
        if step_status == "running":
            step_status = "failed"
        if error_message is None:
            error_message = _clean_optional_text(
                _task_error_summary(failed_generations, "项目模特候选生成失败"),
                max_len=400,
            )
    job_status = _model_library_job_status(
        step_status=step_status,
        requested_count=requested_count,
        finished_count=len(image_ids),
    )
    return ApparelModelLibraryJobOut(
        job_id=f"{run.id}:model_candidates",
        origin="project_candidate",
        workflow_run_id=run.id,
        project_title=run.title,
        status=job_status,  # type: ignore[arg-type]
        requested_count=requested_count,
        finished_count=len(image_ids),
        age_segment=cast(ModelAgeSegment | None, age_segment),
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


@dataclass(slots=True)
class _ModelLibraryTaskAdapter(ModelLibraryTaskPort):
    db: AsyncSession
    user: User
    conversation: Conversation
    run_id: str

    async def submit(
        self,
        task: ModelLibraryGenerationTask,
    ) -> ModelLibraryTaskResult:
        bundle, _, generation_ids = await _create_workflow_task(
            db=self.db,
            user=self.user,
            conv=self.conversation,
            intent=Intent(task.intent),
            text=task.prompt,
            attachment_ids=list(task.attachment_ids),
            idempotency_key=task.idempotency_key,
            workflow_run_id=self.run_id,
            workflow_step_key=MODEL_LIBRARY_GENERATE_STEP_KEY,
            image_params=_model_library_generate_image_params(),
            workflow_meta=dict(task.workflow_meta),
        )
        return ModelLibraryTaskResult(
            bundle=bundle,
            generation_ids=tuple(generation_ids),
        )


async def _enqueue_model_library_generate_tasks(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    run: WorkflowRun,
    step: WorkflowStep,
    body: ApparelModelLibraryGenerateIn,
    reference_image_id: str | None = None,
) -> tuple[list[PublishBundle], list[str]]:
    genders = _model_library_generate_genders(body)
    batch = await model_library_task_application.generate_model_library_tasks(
        model_library_task_application.GenerateModelLibraryTasks(
            run_id=run.id,
            workflow_action=MODEL_LIBRARY_GENERATE_WORKER_ACTION,
            age_segment=body.age_segment or "young_adult",
            genders=genders,
            count_per_gender=int(body.count),
            appearance_direction=body.appearance_direction,
            extra_requirements=body.extra_requirements,
            style_tags=body.style_tags,
            auto_tag=bool(body.auto_tag),
            reference_image_id=reference_image_id,
        ),
        port=_ModelLibraryTaskAdapter(db, user, conv, run.id),
    )
    task_ids = list(batch.generation_ids)
    step.task_ids = task_ids
    return cast(list[PublishBundle], list(batch.bundles)), task_ids


def _model_library_explicit_genders(
    body: ApparelModelLibraryGenerateIn,
) -> list[str]:
    return explicit_model_library_genders(
        getattr(body, "genders", None) or [],
        body.gender,
    )


def _reference_profile_values(
    extracted: ReferenceProfile | None,
) -> ReferenceProfileValues | None:
    if extracted is None:
        return None
    return ReferenceProfileValues(
        age_segment=extracted.age_segment,
        gender=extracted.gender,
        appearance_direction=extracted.appearance_direction,
        style_tags=tuple(extracted.style_tags or []),
    )


def _reference_profile_has_required_text_fields(
    body: ApparelModelLibraryGenerateIn,
    extracted: ReferenceProfile | None,
) -> bool:
    return reference_profile_has_required_fields(
        age_segment=body.age_segment,
        explicit_genders=_model_library_explicit_genders(body),
        profile=_reference_profile_values(extracted),
    )


def _merge_reference_overrides(
    body: ApparelModelLibraryGenerateIn,
    extracted: ReferenceProfile | None,
) -> ApparelModelLibraryGenerateIn:
    resolved = merge_model_library_reference_overrides(
        age_segment=body.age_segment,
        explicit_genders=_model_library_explicit_genders(body),
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
        profile=_reference_profile_values(extracted),
    )
    return body.model_copy(
        update={
            "age_segment": resolved.age_segment,
            "gender": resolved.gender,
            "genders": list(resolved.genders),
            "appearance_direction": resolved.appearance_direction,
            "style_tags": list(resolved.style_tags),
        }
    )


async def generate_apparel_model_library_job(
    body: ApparelModelLibraryGenerateIn,
    user: CurrentUser,
    db: AsyncSession,
) -> ApparelModelLibraryJobOut:
    """模特库独立生成入口。
    创建一条隐藏 WorkflowRun + 一个 step + N 个 worker generation task。
    返回一个 Job 视图（status=queued/running，items=空，前端再轮询 GET /jobs）。
    """
    if int(body.count) not in MODEL_LIBRARY_GENERATE_COUNTS:
        raise _http(
            "invalid_count",
            f"count must be one of {sorted(MODEL_LIBRARY_GENERATE_COUNTS)}",
            422,
        )
    apparel_model_library_generate_mode_total.labels(mode=body.mode).inc()
    reference_image_id: str | None = None
    extracted_profile: ReferenceProfile | None = None
    if body.mode == "reference_image":
        reference_image_id = body.reference_image_id
        await _validate_owned_images(
            db,
            user_id=user.id,
            image_ids=[reference_image_id or ""],
            min_count=1,
            max_count=1,
        )
        extracted_profile = await extract_reference_profile(
            db=db,
            user=user,
            image_id=reference_image_id or "",
        )
        if not _reference_profile_has_required_text_fields(body, extracted_profile):
            apparel_model_library_reference_extract_total.labels(result="failed").inc()
            raise _http(
                "reference_extract_failed",
                "无法识别参考图人物特征，请换一张更清晰的人像，或切回文生图模式。",
                422,
            )
        apparel_model_library_reference_extract_total.labels(result="ok").inc()
        body = _merge_reference_overrides(body, extracted_profile)
    genders = _model_library_generate_genders(body)
    title = _model_library_run_title(
        age_segment=body.age_segment,
        gender=body.gender,
        genders=genders,
        appearance_direction=body.appearance_direction,
        mode=body.mode,
    )
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        conversation_id=None,
        title=title,
        workflow_type=WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        status="running",
        title=title,
        user_prompt=body.extra_requirements or "",
        product_image_ids=[],
        current_step=MODEL_LIBRARY_GENERATE_STEP_KEY,
        quality_mode="standard",
        metadata_jsonb={
            "template": "apparel_model_library_generate",
            "mode": body.mode,
            "reference_image_id": reference_image_id,
            "extracted_profile": (
                extracted_profile.to_dict() if extracted_profile else None
            ),
            "model_profile": {
                "age_segment": body.age_segment,
                "gender": genders[0],
                "genders": genders,
                "appearance_direction": body.appearance_direction,
            },
        },
    )
    db.add(run)
    await db.flush()
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=MODEL_LIBRARY_GENERATE_STEP_KEY,
        status="running",
        input_json={
            "mode": body.mode,
            "reference_image_id": reference_image_id,
            "extracted_profile": (
                extracted_profile.to_dict() if extracted_profile else None
            ),
            "age_segment": body.age_segment,
            "gender": genders[0],
            "genders": genders,
            "appearance_direction": body.appearance_direction,
            "extra_requirements": body.extra_requirements,
            "style_tags": _clean_style_tags(body.style_tags),
            "count": int(body.count),
            "count_per_gender": int(body.count),
            "auto_tag": bool(body.auto_tag),
        },
        output_json={},
    )
    db.add(step)
    await db.flush()
    bundles, _ = await _enqueue_model_library_generate_tasks(
        db=db,
        user=user,
        conv=conv,
        run=run,
        step=step,
        body=body,
        reference_image_id=reference_image_id,
    )
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    await _ensure_legacy_user_library_migrated(db, user.id)
    saved_map = await _saved_image_id_set(db, user.id)
    run = await _get_run(db, user_id=user.id, run_id=run.id)
    job = await _job_from_library_run(db, run=run, saved_map=saved_map)
    await db.commit()
    return job


async def list_apparel_model_library_jobs(
    user: CurrentUser,
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> ApparelModelLibraryJobsOut:
    """聚合任务中心：模特库独立生成 + 项目候选 step。"""
    migrated_legacy = await _ensure_legacy_user_library_migrated(db, user.id)
    saved_map = await _saved_image_id_set(db, user.id)
    fetch_limit = offset + limit + 1
    library_runs = list(
        (
            await db.execute(
                select(WorkflowRun)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
                )
                .order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id))
                .limit(fetch_limit)
            )
        )
        .scalars()
        .all()
    )
    library_jobs: list[ApparelModelLibraryJobOut] = []
    for run in library_runs:
        library_jobs.append(
            await _job_from_library_run(db, run=run, saved_map=saved_map)
        )
    candidate_rows = list(
        (
            await db.execute(
                select(WorkflowRun, WorkflowStep)
                .join(WorkflowStep, WorkflowStep.workflow_run_id == WorkflowRun.id)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE,
                    WorkflowStep.step_key == "model_candidates",
                    WorkflowStep.status.in_(
                        [
                            "queued",
                            "running",
                            "succeeded",
                            "failed",
                            "needs_review",
                            "approved",
                            "completed",
                        ]
                    ),
                )
                .order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id))
                .limit(fetch_limit)
            )
        ).all()
    )
    project_jobs: list[ApparelModelLibraryJobOut] = []
    for run_obj, step in candidate_rows:
        project_jobs.append(
            await _job_from_project_candidate_step(
                db, run=run_obj, step=step, saved_map=saved_map
            )
        )
    merged = sorted(
        [*library_jobs, *project_jobs],
        key=lambda job: job.updated_at or job.created_at,
        reverse=True,
    )
    page = merged[offset : offset + limit]
    if migrated_legacy:
        await db.commit()
    return ApparelModelLibraryJobsOut(
        items=page,
        limit=limit,
        offset=offset,
        has_more=len(merged) > offset + limit,
    )


async def delete_apparel_model_library_job(
    workflow_run_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> dict[str, bool]:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE:
        raise _http(
            "invalid_workflow_type",
            "only standalone model-library jobs can be cleaned here",
            400,
        )
    deleted_at = _now()
    cleanup = await _soft_delete_workflow_generated_images(
        db,
        run=run,
        deleted_at=deleted_at,
        cancel_message="model library job deleted",
        account_mode=getattr(user, "account_mode", "wallet"),
    )
    run.deleted_at = deleted_at
    if run.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == run.conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conv is not None:
            conv.deleted_at = deleted_at
    await db.commit()
    await _post_commit_workflow_generated_cleanup(user_id=user.id, cleanup=cleanup)
    return {"ok": True}


async def clear_apparel_model_library_jobs(
    user: CurrentUser,
    db: AsyncSession,
) -> ApparelModelLibraryJobsClearOut:
    rows = list(
        (
            await db.execute(
                select(WorkflowRun)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
                    WorkflowRun.status.in_(["completed", "failed", "canceled"]),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    cleanups: list[dict[str, Any]] = []
    for run in rows:
        cleanup = await _soft_delete_workflow_generated_images(
            db,
            run=run,
            deleted_at=now,
            cancel_message="model library job cleared",
            account_mode=getattr(user, "account_mode", "wallet"),
        )
        cleanups.append(cleanup)
        run.deleted_at = now
        if run.conversation_id:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.id == run.conversation_id,
                        Conversation.user_id == user.id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conv is not None:
                conv.deleted_at = now
    await db.commit()
    for cleanup in cleanups:
        await _post_commit_workflow_generated_cleanup(user_id=user.id, cleanup=cleanup)
    return ApparelModelLibraryJobsClearOut(deleted=len(rows))


async def save_apparel_model_library_job_item(
    workflow_run_id: str,
    image_id: str,
    body: ApparelModelLibrarySaveJobItemIn,
    user: CurrentUser,
    db: AsyncSession,
    background_tasks: Any,
) -> ApparelModelLibraryItemOut:
    """从任务中心把一张产出图收藏到模特库。
    校验：workflow 属于当前用户；image_id 是该 workflow 任一 step 的产出。
    若 auto_tag=True，触发后台 vision 识别（不阻塞响应）。
    """
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    if run.type not in {WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE, WORKFLOW_TYPE}:
        raise _http(
            "invalid_workflow_type",
            "workflow type does not produce model images",
            400,
        )
    steps = await _load_steps(db, run.id)
    produced = await _workflow_produced_model_image_ids(
        db,
        user_id=user.id,
        steps=steps,
    )
    if image_id not in produced:
        raise _http("invalid_image", "image is not a product of this workflow", 404)
    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source="generated",
        image_id=image_id,
        title=body.title,
        age_segment=body.age_segment,
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    await db.commit()
    item_id = str(item.get("id") or "")
    if body.auto_tag and item_id:
        # BackgroundTasks 在响应发出后再跑，避免阻塞用户。失败 graceful。
        background_tasks.add_task(run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


async def _api_call_tagging_upstream(
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
) -> dict[str, Any]:
    """API 进程内同步调 vision provider 做模特库自动打标签。
    与参考图生模特共用 ``lumen_core.vision_tagging`` 的 prompt、解析和
    Responses 请求构造；失败 graceful，返回 {} 让调用方留默认空字段。
    """
    result = await auto_tag_owned_model_library_image(
        db,
        user_id=user_id,
        image_id=image_id,
    )
    return result.to_dict() if result else {}


@dataclass(slots=True)
class _ModelLibraryTaggingAdapter(ModelLibraryTaggingPort):
    db: AsyncSession
    _loaded_row: ModelLibraryItem | None = field(
        default=None,
        init=False,
        repr=False,
    )

    async def ensure_legacy_migrated(self, *, user_id: str) -> bool:
        return await _ensure_legacy_user_library_migrated(self.db, user_id)

    async def load_item(
        self,
        *,
        user_id: str,
        item_id: str,
    ) -> ModelLibraryTagItem | None:
        row = (
            await self.db.execute(
                select(ModelLibraryItem).where(
                    ModelLibraryItem.id == item_id,
                    ModelLibraryItem.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        self._loaded_row = row
        if row is None:
            return None
        return ModelLibraryTagItem(
            item_id=row.id,
            image_id=row.image_id or "",
            style_tags=tuple(row.style_tags or ()),
            appearance_direction=row.appearance_direction,
            age_segment=row.age_segment,
            gender=row.gender,
        )

    async def fetch_tags(
        self,
        *,
        user_id: str,
        image_id: str,
    ) -> dict[str, object]:
        return await _api_call_tagging_upstream(
            self.db,
            image_id=image_id,
            user_id=user_id,
        )

    async def save_update(
        self,
        *,
        user_id: str,
        item_id: str,
        update: ModelLibraryTagUpdate,
    ) -> None:
        row = self._loaded_row
        if row is None or row.id != item_id or row.user_id != user_id:
            raise ModelLibraryTagItemNotFound(item_id)
        if update.style_tags is not None:
            row.style_tags = list(update.style_tags)
        if update.appearance_direction is not None:
            row.appearance_direction = update.appearance_direction
        if update.age_segment is not None:
            row.age_segment = update.age_segment
        if update.gender is not None:
            row.gender = update.gender
        if update.age_segment is not None or update.gender is not None:
            row.library_folder = _model_library_folder_for_age(
                _normalize_age_segment(row.age_segment),
                row.gender,
            )
        if update.notes is not None:
            row.auto_tag_notes = update.notes
        row.auto_tagged_at = _now()
        await self.db.commit()
        await self.db.refresh(row)

    async def commit_migration(self) -> None:
        await self.db.commit()


async def _auto_tag_library_item(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
) -> ApparelModelLibraryAutoTagOut:
    try:
        result = await auto_tag_model_library_item(
            user_id=user_id,
            item_id=item_id,
            age_segments=MODEL_LIBRARY_AGE_SEGMENTS,
            port=_ModelLibraryTaggingAdapter(db),
        )
    except ModelLibraryTagItemNotFound as exc:
        raise _http("not_found", "model library item not found", 404) from exc
    except ModelLibraryTagItemMissingImage as exc:
        raise _http(
            "invalid_item",
            "library item has no backing image",
            422,
        ) from exc
    return ApparelModelLibraryAutoTagOut(
        item_id=result.item_id,
        style_tags=list(result.style_tags),
        appearance_direction=result.appearance_direction,
        age_segment=result.age_segment,  # type: ignore[arg-type]
        gender=result.gender,
        notes=result.notes,
    )


async def run_auto_tag_in_background(user_id: str, item_id: str) -> None:
    """Background trigger for vision tagging. Uses its own DB session
    because it runs after the request response has been flushed.
    """
    try:
        from app.db import SessionLocal as _Session

        async with _Session() as session:
            await _auto_tag_library_item(
                db=session,
                user_id=user_id,
                item_id=item_id,
            )
    except WorkflowRequestError as exc:
        # Structured 404/422 (item gone / no backing image): expected, info level.
        logger.info(
            "model_library auto_tag background skipped user=%s item=%s status=%s",
            user_id,
            item_id,
            exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        # Unexpected exceptions are real failures — surface to monitoring.
        logger.exception(
            "model_library auto_tag background failed user=%s item=%s err=%s",
            user_id,
            item_id,
            exc,
        )


async def auto_tag_apparel_model_library_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> ApparelModelLibraryAutoTagOut:
    """同步触发 vision 自动识别，并把结果写回 library index。"""
    return await _auto_tag_library_item(db=db, user_id=user.id, item_id=item_id)
