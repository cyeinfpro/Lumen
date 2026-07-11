"""Standalone model-library generation, job aggregation, and auto-tag routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Iterable, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
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

from ...db import get_db
from ...deps import CurrentUser, verify_csrf
from ...observability import (
    apparel_model_library_generate_mode_total,
    apparel_model_library_reference_extract_total,
)
from ...workflow_services.output_sync import MODEL_CANDIDATE_COUNT
from .._apparel_library import (
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_GENERATE_COUNTS,
    MODEL_LIBRARY_GENERATE_STEP_KEY,
    MODEL_LIBRARY_GENERATE_WORKER_ACTION,
    WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
)
from .._apparel_library_reference import (
    ReferenceProfile,
    auto_tag_owned_model_library_image,
    extract_reference_profile,
)
from . import model_library_generation as _generation_helpers
from . import model_library_tagging as _tagging_helpers
from ._facade import RouteFacade, _PublishBundle


router = APIRouter()
logger = logging.getLogger("app.routes.workflows")
_ROUTE_FACADE = RouteFacade(__name__)
FACADE_RUNTIME = _ROUTE_FACADE.runtime
facade_entry = _ROUTE_FACADE.entry
WORKFLOW_TYPE = "apparel_model_showcase"

_clean_optional_text = _ROUTE_FACADE.sync_hook("_clean_optional_text")
_clean_style_tags = _ROUTE_FACADE.sync_hook("_clean_style_tags")
_dedupe_nonempty = _ROUTE_FACADE.sync_hook("_dedupe_nonempty")
_http = _ROUTE_FACADE.sync_hook("_http")
_image_params = _ROUTE_FACADE.sync_hook("_image_params")
_image_url = _ROUTE_FACADE.sync_hook("_image_url")
_model_diversity_anchor = _ROUTE_FACADE.sync_hook("_model_diversity_anchor")
_model_library_download_filename = _ROUTE_FACADE.sync_hook(
    "_model_library_download_filename"
)
_model_library_folder_for_age = _ROUTE_FACADE.sync_hook("_model_library_folder_for_age")
_model_library_item_out = _ROUTE_FACADE.sync_hook("_model_library_item_out")
_normalize_age_segment = _ROUTE_FACADE.sync_hook("_normalize_age_segment")
_normalize_model_gender = _ROUTE_FACADE.sync_hook("_normalize_model_gender")
_now = _ROUTE_FACADE.sync_hook("_now")
_task_error_summary = _ROUTE_FACADE.sync_hook("_task_error_summary")

_add_user_library_item = _ROUTE_FACADE.async_hook("_add_user_library_item")
_create_workflow_task = _ROUTE_FACADE.async_hook("_create_workflow_task")
_ensure_legacy_user_library_migrated = _ROUTE_FACADE.async_hook(
    "_ensure_legacy_user_library_migrated"
)
_get_or_create_workflow_conversation = _ROUTE_FACADE.async_hook(
    "_get_or_create_workflow_conversation"
)
_get_run = _ROUTE_FACADE.async_hook("_get_run")
_image_out_map = _ROUTE_FACADE.async_hook("_image_out_map")
_load_steps = _ROUTE_FACADE.async_hook("_load_steps")
_post_commit_workflow_generated_cleanup = _ROUTE_FACADE.async_hook(
    "_post_commit_workflow_generated_cleanup"
)
_publish_bundles = _ROUTE_FACADE.async_hook("_publish_bundles")
_soft_delete_workflow_generated_images = _ROUTE_FACADE.async_hook(
    "_soft_delete_workflow_generated_images"
)
_validate_owned_images = _ROUTE_FACADE.async_hook("_validate_owned_images")
_workflow_generation_rows_from_task_ids = _ROUTE_FACADE.async_hook(
    "_workflow_generation_rows_from_task_ids"
)

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


_MODEL_LIBRARY_TITLE_AGE_LABELS = _generation_helpers.MODEL_LIBRARY_TITLE_AGE_LABELS


@facade_entry
def _model_library_generate_genders(body: ApparelModelLibraryGenerateIn) -> list[str]:
    raw = getattr(body, "genders", None)
    genders = _dedupe_nonempty(raw or [])
    if not genders and body.gender:
        genders = [body.gender]
    genders = [gender for gender in genders if gender in {"female", "male"}]
    return genders or ["female"]


@facade_entry
def _model_library_gender_label(genders: list[str]) -> str:
    return _generation_helpers.model_library_gender_label(genders)


@facade_entry
def _model_library_run_title(
    *,
    age_segment: str | None,
    gender: str | None = None,
    genders: list[str] | None = None,
    appearance_direction: str | None,
    mode: str = "text",
) -> str:
    return _generation_helpers.model_library_run_title(
        age_segment=age_segment,
        gender=gender,
        genders=genders,
        appearance_direction=appearance_direction,
        mode=mode,
        gender_label=_model_library_gender_label,
    )


@facade_entry
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
    return _generation_helpers.model_library_generate_prompt(
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


@facade_entry
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


@facade_entry
def _model_library_run_inputs(step: WorkflowStep) -> dict[str, Any]:
    """从 step.input_json 拿生成请求快照（age_segment / gender 等）。"""
    raw = step.input_json if isinstance(step.input_json, dict) else {}
    genders = _dedupe_nonempty(raw.get("genders") or [])
    genders = [gender for gender in genders if gender in {"female", "male"}]
    gender = (
        "/".join(genders)
        if len(genders) > 1
        else _normalize_model_gender(genders[0] if genders else raw.get("gender"))
    )
    return {
        "mode": raw.get("mode") or "text",
        "reference_image_id": _clean_optional_text(
            raw.get("reference_image_id"), max_len=64
        ),
        "extracted_profile": raw.get("extracted_profile")
        if isinstance(raw.get("extracted_profile"), dict)
        else None,
        "age_segment": _normalize_age_segment(raw.get("age_segment")),
        "gender": gender,
        "genders": genders,
        "appearance_direction": _clean_optional_text(
            raw.get("appearance_direction"), max_len=80
        ),
        "extra_requirements": _clean_optional_text(
            raw.get("extra_requirements"), max_len=400
        ),
        "style_tags": _clean_style_tags(raw.get("style_tags") or []),
        "auto_tag": bool(raw.get("auto_tag", True)),
        "count": int(raw.get("count") or 0) or len(step.task_ids or []),
    }


@facade_entry
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


@facade_entry
def _model_library_job_status(
    *,
    step_status: str,
    requested_count: int,
    finished_count: int,
) -> str:
    return _generation_helpers.model_library_job_status(
        step_status=step_status,
        requested_count=requested_count,
        finished_count=finished_count,
    )


@facade_entry
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


@facade_entry
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


@facade_entry
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
    if image_out is not None:
        image_url = image_out.url
        display_url = image_out.display_url
        thumb_url = image_out.thumb_url
    else:
        image_url = _image_url(image_id)
        display_url = None
        thumb_url = None
    meta = image_meta or {}
    resolved_tags = _clean_style_tags([*(meta.get("style_tags") or []), *style_tags])
    resolved_age = _normalize_age_segment(meta.get("age_segment") or age_segment)
    if resolved_age == "user_favorites" and age_segment:
        resolved_age = _normalize_age_segment(age_segment)
    resolved_gender = _clean_optional_text(meta.get("gender") or gender, max_len=40)
    resolved_appearance = _clean_optional_text(
        meta.get("appearance_direction") or appearance_direction,
        max_len=80,
    )
    filename = _clean_optional_text(meta.get("download_filename"), max_len=160)
    if not filename:
        filename = _model_library_download_filename(
            image_id=image_id,
            mime=(image_out.mime if image_out is not None else meta.get("mime")),
            age_segment=resolved_age,
            gender=resolved_gender,
            appearance_direction=resolved_appearance,
            style_tags=resolved_tags,
        )
    is_dual_race_bonus = bool(
        meta.get("is_dual_race_bonus")
        or (
            getattr(image_out, "is_dual_race_bonus", False)
            if image_out is not None
            else False
        )
    )
    billing_label = _clean_optional_text(
        meta.get("billing_label")
        or (
            getattr(image_out, "billing_label", None) if image_out is not None else None
        ),
        max_len=32,
    )
    billing_free = bool(
        meta.get("billing_free")
        or (
            getattr(image_out, "billing_free", False)
            if image_out is not None
            else False
        )
        or is_dual_race_bonus
        or billing_label == "free"
    )
    if billing_free and not billing_label:
        billing_label = "free"
    billing_exempt_reason = _clean_optional_text(
        meta.get("billing_exempt_reason")
        or (
            getattr(image_out, "billing_exempt_reason", None)
            if image_out is not None
            else None
        ),
        max_len=80,
    )
    return ApparelModelLibraryJobItemOut(
        image_id=image_id,
        image_url=image_url,
        display_url=display_url,
        thumb_url=thumb_url,
        saved_item_id=saved_item_id,
        style_tags=resolved_tags,
        appearance_direction=resolved_appearance,
        gender=resolved_gender,
        download_filename=filename,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )


@facade_entry
def _extract_bonus_ids(
    step: WorkflowStep | None, image_ids: Iterable[str]
) -> list[str]:
    """从 step.output_json 提取 dual_race_bonus 图片 ids，去除已在 image_ids 里的重叠"""
    if step is None:
        return []
    output = step.output_json or {}
    raw = output.get("dual_race_bonus_image_ids") or []
    if not isinstance(raw, list):
        return []
    seen = set(image_ids)
    return [bid for bid in raw if isinstance(bid, str) and bid not in seen]


@facade_entry
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


@facade_entry
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


@facade_entry
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


@facade_entry
async def _enqueue_model_library_generate_tasks(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    run: WorkflowRun,
    step: WorkflowStep,
    body: ApparelModelLibraryGenerateIn,
    reference_image_id: str | None = None,
) -> tuple[list[_PublishBundle], list[str]]:
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    genders = _model_library_generate_genders(body)
    task_index = 0
    for gender in genders:
        for idx in range(1, int(body.count) + 1):
            task_index += 1
            prompt_candidate_index = 1 if reference_image_id else idx
            prompt = _model_library_generate_prompt(
                age_segment=body.age_segment or "young_adult",
                gender=gender,
                appearance_direction=body.appearance_direction,
                extra_requirements=body.extra_requirements,
                style_tags=body.style_tags,
                candidate_index=prompt_candidate_index,
                reference_mode=reference_image_id is not None,
            )
            bundle, _, gen_ids = await _create_workflow_task(
                db=db,
                user=user,
                conv=conv,
                intent=Intent.IMAGE_TO_IMAGE
                if reference_image_id
                else Intent.TEXT_TO_IMAGE,
                text=prompt,
                attachment_ids=[reference_image_id] if reference_image_id else [],
                idempotency_key=f"mlib:{run.id[:24]}:{gender}:{idx}",
                workflow_run_id=run.id,
                workflow_step_key=MODEL_LIBRARY_GENERATE_STEP_KEY,
                image_params=_model_library_generate_image_params(),
                workflow_meta={
                    "workflow_action": MODEL_LIBRARY_GENERATE_WORKER_ACTION,
                    "workflow_candidate_index": task_index,
                    "workflow_model_library_mode": (
                        "reference_image" if reference_image_id else "text"
                    ),
                    "workflow_model_library_reference_image_id": reference_image_id
                    or "",
                    "workflow_model_library_age_segment": body.age_segment,
                    "workflow_model_library_gender": gender,
                    "workflow_model_library_appearance_direction": (
                        body.appearance_direction or ""
                    ),
                    "workflow_model_library_style_tags": _clean_style_tags(
                        body.style_tags
                    ),
                    "workflow_model_library_auto_tag": bool(body.auto_tag),
                },
            )
            task_ids.extend(gen_ids)
            bundles.append(bundle)
    step.task_ids = task_ids
    return bundles, task_ids


@facade_entry
def _model_library_explicit_genders(
    body: ApparelModelLibraryGenerateIn,
) -> list[str]:
    raw = getattr(body, "genders", None)
    genders = _dedupe_nonempty(raw or [])
    if not genders and body.gender:
        genders = [body.gender]
    return [gender for gender in genders if gender in {"female", "male"}]


@facade_entry
def _reference_profile_has_required_text_fields(
    body: ApparelModelLibraryGenerateIn,
    extracted: ReferenceProfile | None,
) -> bool:
    age = body.age_segment or (extracted.age_segment if extracted else None)
    gender = _model_library_explicit_genders(body) or (
        [extracted.gender] if extracted and extracted.gender else []
    )
    return bool(age and gender)


@facade_entry
def _merge_reference_overrides(
    body: ApparelModelLibraryGenerateIn,
    extracted: ReferenceProfile | None,
) -> ApparelModelLibraryGenerateIn:
    explicit_genders = _model_library_explicit_genders(body)
    extracted_tags = extracted.style_tags if extracted else []
    merged_tags = _clean_style_tags([*(body.style_tags or []), *extracted_tags])
    genders = explicit_genders
    if not genders and extracted and extracted.gender in {"female", "male"}:
        genders = [extracted.gender]
    if not genders:
        genders = ["female"]
    return body.model_copy(
        update={
            "age_segment": body.age_segment
            or (extracted.age_segment if extracted else None)
            or "young_adult",
            "gender": genders[0],
            "genders": genders,
            "appearance_direction": (
                body.appearance_direction
                or (extracted.appearance_direction if extracted else None)
            ),
            "style_tags": merged_tags,
        }
    )


@router.post(
    "/apparel-model-library/generate",
    response_model=ApparelModelLibraryJobOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def generate_apparel_model_library_job(
    body: ApparelModelLibraryGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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


@router.get(
    "/apparel-model-library/jobs",
    response_model=ApparelModelLibraryJobsOut,
)
@facade_entry
async def list_apparel_model_library_jobs(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
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


@router.delete(
    "/apparel-model-library/jobs/{workflow_run_id}",
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def delete_apparel_model_library_job(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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


@router.delete(
    "/apparel-model-library/jobs",
    response_model=ApparelModelLibraryJobsClearOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def clear_apparel_model_library_jobs(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
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


@router.post(
    "/apparel-model-library/jobs/{workflow_run_id}/items/{image_id}/save",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def save_apparel_model_library_job_item(
    workflow_run_id: str,
    image_id: str,
    body: ApparelModelLibrarySaveJobItemIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
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
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


@facade_entry
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


_AGE_ALIASES_API = _tagging_helpers.AGE_ALIASES_API


@facade_entry
def _normalize_tagged_age(value: Any) -> str | None:
    return _tagging_helpers.normalize_tagged_age(
        value,
        age_segments=MODEL_LIBRARY_AGE_SEGMENTS,
    )


@facade_entry
def _normalize_tagged_gender(value: Any) -> str | None:
    return _tagging_helpers.normalize_tagged_gender(value)


@facade_entry
async def _auto_tag_library_item(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
) -> ApparelModelLibraryAutoTagOut:
    return await _tagging_helpers.auto_tag_library_item(
        db=db,
        user_id=user_id,
        item_id=item_id,
        hooks=_tagging_helpers.AutoTagHooks(
            ensure_legacy_user_library_migrated=(_ensure_legacy_user_library_migrated),
            api_call_tagging_upstream=_api_call_tagging_upstream,
            http_error=_http,
            clean_style_tags=_clean_style_tags,
            clean_optional_text=_clean_optional_text,
            normalize_tagged_age=_normalize_tagged_age,
            normalize_tagged_gender=_normalize_tagged_gender,
            normalize_age_segment=_normalize_age_segment,
            model_library_folder_for_age=_model_library_folder_for_age,
            now=_now,
        ),
    )


@facade_entry
async def _run_auto_tag_in_background(user_id: str, item_id: str) -> None:
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
    except HTTPException as exc:
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


@router.post(
    "/apparel-model-library/items/{item_id:path}/auto-tag",
    response_model=ApparelModelLibraryAutoTagOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def auto_tag_apparel_model_library_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryAutoTagOut:
    """同步触发 vision 自动识别，并把结果写回 library index。"""
    return await _auto_tag_library_item(db=db, user_id=user.id, item_id=item_id)


FACADE_EXPORTS = (
    "_MODEL_LIBRARY_TITLE_AGE_LABELS",
    "_model_library_generate_genders",
    "_model_library_gender_label",
    "_model_library_run_title",
    "_model_library_generate_prompt",
    "_model_library_generate_image_params",
    "_model_library_run_inputs",
    "_saved_image_id_set",
    "_model_library_job_status",
    "_gather_job_image_outs",
    "_model_library_image_meta_by_id",
    "_job_item_out",
    "_extract_bonus_ids",
    "_workflow_produced_model_image_ids",
    "_job_from_library_run",
    "_job_from_project_candidate_step",
    "_enqueue_model_library_generate_tasks",
    "_model_library_explicit_genders",
    "_reference_profile_has_required_text_fields",
    "_merge_reference_overrides",
    "generate_apparel_model_library_job",
    "list_apparel_model_library_jobs",
    "delete_apparel_model_library_job",
    "clear_apparel_model_library_jobs",
    "save_apparel_model_library_job_item",
    "_api_call_tagging_upstream",
    "_AGE_ALIASES_API",
    "_normalize_tagged_age",
    "_normalize_tagged_gender",
    "_auto_tag_library_item",
    "_run_auto_tag_in_background",
    "auto_tag_apparel_model_library_item",
)


def export_to_facade(facade: Any) -> None:
    _ROUTE_FACADE.export(facade, FACADE_EXPORTS)
