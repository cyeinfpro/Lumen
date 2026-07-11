"""Apparel workflow creation, private library CRUD, and candidate setup routes."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import Intent
from lumen_core.models import (
    ModelCandidate,
    ModelLibraryHiddenPreset,
    ModelLibraryItem,
    WorkflowRun,
)
from lumen_core.schemas import (
    AgeSegment,
    ApparelModelLibraryBatchDeleteIn,
    ApparelModelLibraryBatchDeleteOut,
    ApparelModelLibraryItemCreateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryItemPatchIn,
    ApparelModelLibraryListOut,
    ApparelModelLibrarySelectIn,
    ApparelModelLibrarySyncOut,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ChatParamsIn,
    ModelCandidatesCreateIn,
    ProductAnalysisApproveIn,
    WorkflowRunOut,
)

from ...db import get_db
from ...deps import CurrentUser, verify_csrf
from .._apparel_library import (
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_APPEARANCES,
    MODEL_LIBRARY_SOURCES,
)
from ._facade import RouteFacade, _PublishBundle


entry_router = APIRouter()
project_router = APIRouter()
logger = logging.getLogger("app.routes.workflows")
_ROUTE_FACADE = RouteFacade(__name__)
FACADE_RUNTIME = _ROUTE_FACADE.runtime
facade_entry = _ROUTE_FACADE.entry
WORKFLOW_TYPE = "apparel_model_showcase"

_accessory_plan_from_product_analysis = _ROUTE_FACADE.sync_hook(
    "_accessory_plan_from_product_analysis"
)
_candidate_image_params = _ROUTE_FACADE.sync_hook("_candidate_image_params")
_candidate_prompt = _ROUTE_FACADE.sync_hook("_candidate_prompt")
_can_sync_library = _ROUTE_FACADE.sync_hook("_can_sync_library")
_clean_optional_text = _ROUTE_FACADE.sync_hook("_clean_optional_text")
_clean_style_tags = _ROUTE_FACADE.sync_hook("_clean_style_tags")
_coerce_accessory_plan_payload = _ROUTE_FACADE.sync_hook(
    "_coerce_accessory_plan_payload"
)
_dedupe_nonempty = _ROUTE_FACADE.sync_hook("_dedupe_nonempty")
_filter_library_items = _ROUTE_FACADE.sync_hook("_filter_library_items")
_github_contents_url = _ROUTE_FACADE.sync_hook("_github_contents_url")
_height_requirement = _ROUTE_FACADE.sync_hook("_height_requirement")
_hide_preset_in_legacy_user_library_index = _ROUTE_FACADE.sync_hook(
    "_hide_preset_in_legacy_user_library_index"
)
_http = _ROUTE_FACADE.sync_hook("_http")
_infer_model_height_cm = _ROUTE_FACADE.sync_hook("_infer_model_height_cm")
_library_binary_response = _ROUTE_FACADE.sync_hook("_library_binary_response")
_merge_product_corrections = _ROUTE_FACADE.sync_hook("_merge_product_corrections")
_metadata_model_profile_from_prompt = _ROUTE_FACADE.sync_hook(
    "_metadata_model_profile_from_prompt"
)
_model_library_folder_for_age = _ROUTE_FACADE.sync_hook("_model_library_folder_for_age")
_model_library_item_out = _ROUTE_FACADE.sync_hook("_model_library_item_out")
_model_library_row_to_dict = _ROUTE_FACADE.sync_hook("_model_library_row_to_dict")
_normalize_age_segment = _ROUTE_FACADE.sync_hook("_normalize_age_segment")
_now = _ROUTE_FACADE.sync_hook("_now")
_product_analysis_prompt = _ROUTE_FACADE.sync_hook("_product_analysis_prompt")
_remove_user_library_item_from_legacy_index = _ROUTE_FACADE.sync_hook(
    "_remove_user_library_item_from_legacy_index"
)
_seed_steps = _ROUTE_FACADE.sync_hook("_seed_steps")
_sync_state_out = _ROUTE_FACADE.sync_hook("_sync_state_out")

_add_user_library_item = _ROUTE_FACADE.async_hook("_add_user_library_item")
_build_run_out = _ROUTE_FACADE.async_hook("_build_run_out")
_combined_library_items = _ROUTE_FACADE.async_hook("_combined_library_items")
_create_user_image_from_preset = _ROUTE_FACADE.async_hook(
    "_create_user_image_from_preset"
)
_create_workflow_task = _ROUTE_FACADE.async_hook("_create_workflow_task")
_ensure_legacy_user_library_migrated = _ROUTE_FACADE.async_hook(
    "_ensure_legacy_user_library_migrated"
)
_find_library_item = _ROUTE_FACADE.async_hook("_find_library_item")
_get_or_create_workflow_conversation = _ROUTE_FACADE.async_hook(
    "_get_or_create_workflow_conversation"
)
_get_owned_conversation = _ROUTE_FACADE.async_hook("_get_owned_conversation")
_get_run = _ROUTE_FACADE.async_hook("_get_run")
_owned_image = _ROUTE_FACADE.async_hook("_owned_image")
_publish_bundles = _ROUTE_FACADE.async_hook("_publish_bundles")
_resolve_model_library_sync_proxy = _ROUTE_FACADE.async_hook(
    "_resolve_model_library_sync_proxy"
)
_run_auto_tag_in_background = _ROUTE_FACADE.async_hook("_run_auto_tag_in_background")
_step = _ROUTE_FACADE.async_hook("_step")
_sync_library_presets_from_github_folder = _ROUTE_FACADE.async_hook(
    "_sync_library_presets_from_github_folder"
)
_sync_workflow_outputs = _ROUTE_FACADE.async_hook("_sync_workflow_outputs")
_validate_owned_images = _ROUTE_FACADE.async_hook("_validate_owned_images")


@entry_router.post(
    "/apparel-model-showcase",
    response_model=ApparelWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def create_apparel_model_showcase(
    body: ApparelWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelWorkflowCreateOut:
    image_ids = await _validate_owned_images(
        db,
        user_id=user.id,
        image_ids=body.product_image_ids,
        min_count=1,
        max_count=3,
    )
    title = (body.title or "").strip() or "服饰模特展示图"
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        # Workflow task messages need a backing conversation, but it should not
        # attach to a user-visible chat session.
        conversation_id=None,
        title=title,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=WORKFLOW_TYPE,
        status="running",
        title=title,
        user_prompt=body.user_prompt,
        product_image_ids=image_ids,
        current_step="product_analysis",
        quality_mode=body.quality_mode,
        metadata_jsonb={
            "template": WORKFLOW_TYPE,
            "mvp_scope": "adult_daily_apparel",
            "priority": ["model_consistency", "product_fidelity", "premium_aesthetic"],
            "model_profile": _metadata_model_profile_from_prompt(body.user_prompt),
        },
    )
    db.add(run)
    await db.flush()
    for step in _seed_steps(run, user_prompt=body.user_prompt):
        db.add(step)
    product_step = await _step(db, run.id, "product_analysis")

    bundle, completion_id, _ = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.VISION_QA,
        text=_product_analysis_prompt(body.user_prompt),
        attachment_ids=image_ids,
        idempotency_key=f"wf:{run.id}:analysis",
        workflow_run_id=run.id,
        workflow_step_key="product_analysis",
        chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
        workflow_meta={"workflow_action": "product_analysis"},
    )
    product_step.task_ids = [completion_id] if completion_id else []
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    return ApparelWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


@entry_router.get(
    "/apparel-model-library",
    response_model=ApparelModelLibraryListOut,
)
@facade_entry
async def list_apparel_model_library(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    age_segment: AgeSegment = Query(default="all"),
    source: str = Query(default="all"),
    appearance: str = Query(default="all"),
    q: str = Query(default=""),
) -> ApparelModelLibraryListOut:
    source = source.strip() or "all"
    if source not in MODEL_LIBRARY_SOURCES:
        raise _http("invalid_source", "invalid model library source", 422)
    age = str(age_segment)
    if age not in MODEL_LIBRARY_AGE_SEGMENTS:
        raise _http("invalid_age_segment", "invalid model library age segment", 422)
    appearance = appearance.strip() or "all"
    if appearance not in MODEL_LIBRARY_APPEARANCES:
        raise _http("invalid_appearance", "invalid model library appearance", 422)
    combined_items, migrated_legacy = await _combined_library_items(db, user.id)
    items = _filter_library_items(
        combined_items,
        source=source,
        age_segment=age,
        appearance=appearance,
        q=q,
    )
    if migrated_legacy:
        await db.commit()
    return ApparelModelLibraryListOut(
        items=[_model_library_item_out(item) for item in items],
        sync=_sync_state_out(user),
    )


@entry_router.post(
    "/apparel-model-library/sync-presets",
    response_model=ApparelModelLibrarySyncOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def sync_apparel_model_library_presets(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibrarySyncOut:
    if not _can_sync_library(user):
        raise _http("forbidden", "model library preset sync is not allowed", 403)
    _, proxy_url = await _resolve_model_library_sync_proxy(db)
    # Auth and proxy-setting reads start a request transaction. End it before
    # GitHub/network and storage I/O so no database snapshot or row lock lingers.
    await db.rollback()
    return await _sync_library_presets_from_github_folder(
        _github_contents_url(),
        proxy_url=proxy_url,
    )


@entry_router.get("/apparel-model-library/items/{item_id:path}/binary")
@facade_entry
async def get_apparel_model_library_item_binary(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    item = await _find_library_item(db, user_id=user.id, item_id=item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    if item.get("image_id"):
        raise _http("use_image_api", "user library image is served by image API", 400)
    storage_key = str(item.get("image_storage_key") or "").strip()
    return _library_binary_response(storage_key, request)


@entry_router.get("/apparel-model-library/items/{item_id:path}/thumb")
@facade_entry
async def get_apparel_model_library_item_thumb(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    item = await _find_library_item(db, user_id=user.id, item_id=item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    if item.get("image_id"):
        raise _http("use_image_api", "user library image is served by image API", 400)
    storage_key = str(
        item.get("thumb_storage_key") or item.get("image_storage_key") or ""
    ).strip()
    return _library_binary_response(storage_key, request)


@entry_router.post(
    "/apparel-model-library/items",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def create_apparel_model_library_item(
    body: ApparelModelLibraryItemCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source=body.source,
        image_id=body.image_id,
        title=body.title,
        age_segment=body.age_segment,
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    await db.commit()
    item_id = str(item.get("id") or "")
    if body.auto_tag and item_id:
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


@entry_router.patch(
    "/apparel-model-library/items/{item_id:path}",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def patch_apparel_model_library_item(
    item_id: str,
    body: ApparelModelLibraryItemPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryItemOut:
    await _ensure_legacy_user_library_migrated(db, user.id)
    row = (
        await db.execute(
            select(ModelLibraryItem).where(
                ModelLibraryItem.id == item_id,
                ModelLibraryItem.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "model library item not found", 404)
    if body.title is not None:
        row.title = body.title.strip()[:120]
    if body.age_segment is not None:
        row.age_segment = _normalize_age_segment(body.age_segment)
        row.library_folder = _model_library_folder_for_age(row.age_segment, row.gender)
    if body.gender is not None:
        row.gender = _clean_optional_text(body.gender, max_len=40)
        row.library_folder = _model_library_folder_for_age(row.age_segment, row.gender)
    if body.appearance_direction is not None:
        row.appearance_direction = _clean_optional_text(
            body.appearance_direction, max_len=80
        )
    if body.style_tags is not None:
        row.style_tags = _clean_style_tags(body.style_tags)
    await db.commit()
    await db.refresh(row)
    return _model_library_item_out(_model_library_row_to_dict(row))


@facade_entry
async def _delete_apparel_model_library_item_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> bool:
    """Delete a private item or hide a global preset for one user."""
    if item_id.startswith("user:"):
        removed_legacy = _remove_user_library_item_from_legacy_index(user_id, item_id)
        row = (
            await db.execute(
                select(ModelLibraryItem).where(
                    ModelLibraryItem.id == item_id,
                    ModelLibraryItem.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return removed_legacy
        await db.delete(row)
        return True

    item = await _find_library_item(db, user_id=user_id, item_id=item_id)
    if item is None or item.get("source") != "preset":
        return False
    existing = (
        await db.execute(
            select(ModelLibraryHiddenPreset).where(
                ModelLibraryHiddenPreset.user_id == user_id,
                ModelLibraryHiddenPreset.preset_id == item_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(ModelLibraryHiddenPreset(user_id=user_id, preset_id=item_id))
    _hide_preset_in_legacy_user_library_index(user_id, item_id)
    return True


@entry_router.delete(
    "/apparel-model-library/items/{item_id:path}",
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def delete_apparel_model_library_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    await _ensure_legacy_user_library_migrated(db, user.id)
    deleted = await _delete_apparel_model_library_item_for_user(
        db,
        user_id=user.id,
        item_id=item_id,
    )
    if not deleted:
        raise _http("not_found", "model library item not found", 404)
    await db.commit()
    return {"ok": True}


@entry_router.post(
    "/apparel-model-library/items/batch-delete",
    response_model=ApparelModelLibraryBatchDeleteOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def batch_delete_apparel_model_library_items(
    body: ApparelModelLibraryBatchDeleteIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryBatchDeleteOut:
    await _ensure_legacy_user_library_migrated(db, user.id)
    item_ids = _dedupe_nonempty(body.item_ids)
    deleted = 0
    not_found: list[str] = []
    for item_id in item_ids:
        if await _delete_apparel_model_library_item_for_user(
            db,
            user_id=user.id,
            item_id=item_id,
        ):
            deleted += 1
        else:
            not_found.append(item_id)
    await db.commit()
    return ApparelModelLibraryBatchDeleteOut(deleted=deleted, not_found=not_found)


@project_router.post(
    "/{workflow_run_id}/steps/product-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def approve_product_analysis(
    workflow_run_id: str,
    body: ProductAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status not in {"needs_review", "approved"}:
        raise _http("step_not_ready", "product analysis is not ready to approve", 409)
    product_step.output_json = _merge_product_corrections(
        product_step.output_json or {},
        body.corrections or {},
    )
    product_step.status = "approved"
    product_step.approved_at = _now()
    product_step.approved_by = user.id
    model_settings = await _step(db, run.id, "model_settings")
    if model_settings.status == "waiting_input":
        model_settings.status = "needs_review"
        model_settings.input_json = {
            "style_prompt": run.user_prompt,
            "avoid": ["过度网红感", "夸张姿势", "强烈妆容"],
        }
    run.current_step = "model_settings"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@project_router.post(
    "/{workflow_run_id}/model-candidates",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def create_model_candidates(
    workflow_run_id: str,
    body: ModelCandidatesCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    existing_candidates = (
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

    model_settings = await _step(db, run.id, "model_settings")
    candidate_step = await _step(db, run.id, "model_candidates")
    if candidate_step.status == "running":
        raise _http(
            "already_running", "model candidates are already being generated", 409
        )
    if any(candidate.status == "selected" for candidate in existing_candidates):
        raise _http(
            "model_already_selected",
            "reopen model selection before generating new candidates",
            409,
        )
    model_settings.status = "approved"
    model_settings.approved_at = _now()
    model_settings.approved_by = user.id
    model_settings.output_json = {
        "style_prompt": body.style_prompt or run.user_prompt,
        "avoid": body.avoid,
        "candidate_count": body.candidate_count,
        "accessory_plan": body.accessory_plan.model_dump(),
    }
    candidate_step.status = "running"
    candidate_step.input_json = model_settings.output_json
    run.current_step = "model_candidates"
    run.status = "running"

    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    model_direction = (
        body.style_prompt or run.user_prompt or "premium ecommerce synthetic model"
    )
    height_cm = _infer_model_height_cm(model_direction)
    height_requirement = _height_requirement(model_direction)
    existing_count = len(existing_candidates)
    for idx in range(1, body.candidate_count + 1):
        candidate_index = existing_count + idx
        candidate = ModelCandidate(
            workflow_run_id=run.id,
            candidate_index=candidate_index,
            status="generating",
            model_brief_json={
                "summary": model_direction,
                "candidate_index": candidate_index,
                "height_cm": height_cm,
                "height_label": f"身高 {height_cm}cm",
                "height_requirement": height_requirement,
                "product_context": product_step.output_json,
                "note": "未试穿商品，仅用于确认模特形象",
            },
        )
        db.add(candidate)
        await db.flush()
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.TEXT_TO_IMAGE,
            text=_candidate_prompt(
                style_prompt=body.style_prompt or run.user_prompt,
                product_analysis=product_step.output_json or {},
                candidate_index=candidate_index,
                avoid=body.avoid,
            ),
            attachment_ids=[],
            idempotency_key=f"wf:{run.id[:24]}:cand:{candidate_index}",
            workflow_run_id=run.id,
            workflow_step_key="model_candidates",
            image_params=_candidate_image_params(),
            workflow_meta={
                "workflow_action": "model_candidate",
                "workflow_candidate_id": candidate.id,
                "workflow_candidate_index": candidate_index,
                "workflow_candidate_view": "concept_sheet",
            },
        )
        candidate.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    candidate_step.task_ids = task_ids
    approval = await _step(db, run.id, "model_approval")
    approval.input_json = {
        **(approval.input_json or {}),
        "accessory_plan": body.accessory_plan.model_dump(),
        "style_prompt": body.style_prompt or run.user_prompt,
    }
    if body.accessory_plan.enabled:
        approval.status = "waiting_input"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@project_router.post(
    "/{workflow_run_id}/model-library/select",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
@facade_entry
async def select_apparel_model_library_item(
    workflow_run_id: str,
    body: ApparelModelLibrarySelectIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    item = await _find_library_item(db, user_id=user.id, item_id=body.library_item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    try:
        if item.get("source") == "preset":
            image = await _create_user_image_from_preset(db, user_id=user.id, item=item)
        else:
            image_id = str(item.get("image_id") or "").strip()
            image = await _owned_image(db, user_id=user.id, image_id=image_id)
    except HTTPException:
        # 已是结构化错误（404/400/...），让 _get_run 的 row lock 在事务回滚时自动释放
        await db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.exception("select_apparel_model_library_item: image materialize failed")
        raise _http(
            "library_image_failed",
            f"failed to materialize library image: {exc}",
            500,
        ) from exc

    model_settings = await _step(db, run.id, "model_settings")
    now = _now()
    requested_accessory_plan = (
        body.accessory_plan.model_dump() if body.accessory_plan is not None else None
    )
    existing_accessory_plan = _coerce_accessory_plan_payload(
        (model_settings.output_json or {}).get("accessory_plan")
    ) or _coerce_accessory_plan_payload(
        (model_settings.input_json or {}).get("accessory_plan")
    )
    accessory_plan = (
        requested_accessory_plan
        or existing_accessory_plan
        or _accessory_plan_from_product_analysis(product_step.output_json or {})
    )
    style_prompt = (
        body.style_prompt.strip()
        or str((model_settings.output_json or {}).get("style_prompt") or "").strip()
        or str((model_settings.input_json or {}).get("style_prompt") or "").strip()
        or run.user_prompt
    )
    existing_count = (
        (
            await db.execute(
                select(ModelCandidate.id).where(
                    ModelCandidate.workflow_run_id == run.id
                )
            )
        )
        .scalars()
        .all()
    )
    candidate = ModelCandidate(
        workflow_run_id=run.id,
        candidate_index=len(existing_count) + 1,
        contact_sheet_image_id=image.id,
        portrait_image_id=image.id,
        status="ready",
        selected_at=None,
        model_brief_json={
            "summary": item.get("title") or "库内模特",
            "source": "model_library",
            "library_item_id": body.library_item_id,
            "age_segment": _normalize_age_segment(item.get("age_segment")),
            "gender": item.get("gender"),
            "appearance_direction": item.get("appearance_direction"),
            "style_tags": _clean_style_tags(item.get("style_tags") or []),
            "prompt_hint": item.get("prompt_hint"),
            "candidate_image_ids": [image.id],
            "note": "来自模特库，未试穿商品",
        },
    )
    db.add(candidate)
    await db.flush()
    model_settings.status = "approved"
    model_settings.approved_at = now
    model_settings.approved_by = user.id
    model_settings.output_json = {
        **(model_settings.output_json or {}),
        "style_prompt": style_prompt,
        "accessory_plan": accessory_plan,
        "selected_library_item_id": body.library_item_id,
        "selected_library_image_id": image.id,
    }
    candidate_step = await _step(db, run.id, "model_candidates")
    candidate_step.status = "needs_review"
    candidate_step.image_ids = _dedupe_nonempty(
        [*(candidate_step.image_ids or []), image.id]
    )
    candidate_step.input_json = {
        **(candidate_step.input_json or {}),
        "source": "model_library",
        "library_item_id": body.library_item_id,
        "style_prompt": style_prompt,
        "accessory_plan": accessory_plan,
    }
    candidate_step.output_json = {
        **(candidate_step.output_json or {}),
        "library_candidate_id": candidate.id,
        "library_candidate_image_id": image.id,
    }
    approval = await _step(db, run.id, "model_approval")
    if approval.status == "waiting_input":
        approval.status = "needs_review"
    approval.input_json = {
        **(approval.input_json or {}),
        "source": "model_library",
        "library_item_id": body.library_item_id,
        "style_prompt": style_prompt,
        "accessory_plan": accessory_plan,
    }
    run.current_step = "model_candidates"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


FACADE_EXPORTS = (
    "create_apparel_model_showcase",
    "list_apparel_model_library",
    "sync_apparel_model_library_presets",
    "get_apparel_model_library_item_binary",
    "get_apparel_model_library_item_thumb",
    "create_apparel_model_library_item",
    "patch_apparel_model_library_item",
    "_delete_apparel_model_library_item_for_user",
    "delete_apparel_model_library_item",
    "batch_delete_apparel_model_library_items",
    "approve_product_analysis",
    "create_model_candidates",
    "select_apparel_model_library_item",
)


def export_to_facade(facade: Any) -> None:
    _ROUTE_FACADE.export(facade, FACADE_EXPORTS)
