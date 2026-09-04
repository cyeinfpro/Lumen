"""Apparel workflow creation, private library CRUD, and candidate setup routes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from collections.abc import Sequence
from typing import Any, cast

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
    ApparelModelLibrarySyncStateOut,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ChatParamsIn,
    ModelCandidatesCreateIn,
    ProductAnalysisApproveIn,
    WorkflowRunOut,
)
from ....deps import CurrentUser
from ...application.apparel_library import (
    DeleteApparelModelLibraryItems,
    ListApparelModelLibrary,
    SyncApparelModelLibraryPresets,
)
from ...application.apparel_workflow_rules import (
    approve_product_analysis_state,
    ensure_product_analysis_ready,
    metadata_model_profile_from_prompt as _metadata_model_profile_from_prompt,
    resolve_accessory_plan,
    resolve_style_prompt,
)
from ...application.errors import WorkflowRequestError
from ...application.runtime_state import WorkflowRuntimeState
from ...application.values import dedupe_nonempty as _dedupe_nonempty
from ..paid_idempotency import record_current_paid_operation
from ...domain.apparel_library import (
    model_library_folder_for_age as _model_library_folder_for_age,
    normalize_age_segment as _normalize_age_segment,
)
from ...domain.showcase_model_policy import (
    height_requirement as _height_requirement,
)
from ...domain.showcase_model_policy import (
    infer_model_height_cm as _infer_model_height_cm,
)
from ...domain.workflow_contracts import PublishBundle as _PublishBundle
from ...ports.apparel_library import ApparelLibraryUser
from ...ports.runtime_state import AsyncLockPort
from ..delivery import workflow_binary_file
from ..library_items import can_sync_library as _can_sync_library
from ..library_items import combined_library_items as _combined_library_items
from ..library_items import (
    ensure_legacy_user_library_migrated as _ensure_legacy_user_library_migrated,
)
from ..library_items import filter_library_items as _filter_library_items
from ..library_items import find_library_item as _find_library_item
from ..library_items import github_contents_url as _github_contents_url
from ..library_items import model_library_item_out as _model_library_item_out
from ..library_items import model_library_row_to_dict as _model_library_row_to_dict
from ..library_items import (
    resolve_model_library_sync_proxy as _resolve_model_library_sync_proxy,
)
from ..library_items import sync_state_out as _sync_state_out
from ..library_materialization import add_user_library_item as _add_user_library_item
from ..library_materialization import (
    create_user_image_from_preset as _create_user_image_from_preset,
)
from ..library_materialization import owned_image as _owned_image
from ..library_storage import (
    hide_preset_in_legacy_user_library_index as _hide_preset_in_legacy_user_library_index,
)
from ..library_storage import (
    remove_user_library_item_from_legacy_index as _remove_user_library_item_from_legacy_index,
)
from ..library_sync import (
    ApparelLibrarySyncDependencies as _ApparelLibrarySyncDependencies,
)
from ..library_sync import (
    sync_library_presets_from_github_folder as _sync_library_presets_from_github_folder,
)
from ..output_sync import sync_workflow_outputs as _sync_workflow_outputs
from ..serialization import clean_optional_text as _clean_optional_text
from ..serialization import clean_style_tags as _clean_style_tags
from ..showcase_inputs import candidate_prompt as _candidate_prompt
from ..showcase_inputs import product_analysis_prompt as _product_analysis_prompt
from ..showcase_inputs import seed_steps as _seed_steps
from ..showcase_inputs import validate_owned_images as _validate_owned_images
from ..workflow_runtime import build_run_out as _build_run_out
from ..workflow_runtime import candidate_image_params as _candidate_image_params
from ..workflow_runtime import create_workflow_task as _create_workflow_task
from ..workflow_runtime import (
    get_or_create_workflow_conversation as _get_or_create_workflow_conversation,
)
from ..workflow_runtime import get_owned_conversation as _get_owned_conversation
from ..workflow_runtime import get_run as _get_run
from ..workflow_runtime import publish_bundles as _publish_bundles
from ..workflow_runtime import step as _step
from .model_library import (
    run_auto_tag_in_background as _run_auto_tag_in_background,
)

logger = logging.getLogger("app.routes.workflows.apparel")
WORKFLOW_TYPE = "apparel_model_showcase"


def _http(
    code: str,
    message: str,
    status_code: int = 400,
) -> WorkflowRequestError:
    return WorkflowRequestError(
        status_code=status_code,
        code=code,
        message=message,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _SQLAlchemyApparelLibraryAdapter:
    db: AsyncSession

    async def combined_items(
        self,
        *,
        user_id: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        return await _combined_library_items(self.db, user_id)

    def filter_items(
        self,
        items: Sequence[dict[str, Any]],
        *,
        source: str,
        age_segment: str,
        appearance: str,
        query: str,
    ) -> list[dict[str, Any]]:
        return _filter_library_items(
            items,
            source=source,
            age_segment=age_segment,
            appearance=appearance,
            q=query,
        )

    async def usage_counts(
        self,
        *,
        user_id: str,
        item_ids: Sequence[str],
    ) -> dict[str, int]:
        wanted = {item_id for item_id in item_ids if item_id}
        if not wanted:
            return {}
        rows = (
            await self.db.execute(
                select(
                    ModelCandidate.workflow_run_id,
                    ModelCandidate.model_brief_json,
                )
                .join(WorkflowRun, WorkflowRun.id == ModelCandidate.workflow_run_id)
                .where(
                    WorkflowRun.user_id == user_id,
                    WorkflowRun.deleted_at.is_(None),
                )
            )
        ).all()
        runs_by_item: dict[str, set[str]] = {}
        for run_id, raw_brief in rows:
            brief = raw_brief if isinstance(raw_brief, dict) else {}
            item_id = brief.get("library_item_id")
            if not isinstance(item_id, str) or item_id not in wanted:
                continue
            runs_by_item.setdefault(item_id, set()).add(run_id)
        return {item_id: len(run_ids) for item_id, run_ids in runs_by_item.items()}

    def item_out(self, item: dict[str, Any]) -> ApparelModelLibraryItemOut:
        return _model_library_item_out(item)

    def sync_state_out(
        self,
        user: ApparelLibraryUser,
    ) -> ApparelModelLibrarySyncStateOut:
        return _sync_state_out(cast(Any, user))

    def can_sync(self, user: ApparelLibraryUser) -> bool:
        return _can_sync_library(cast(Any, user))

    def github_contents_url(self) -> str:
        return _github_contents_url()

    async def resolve_sync_proxy(self) -> str | None:
        _, proxy_url = await _resolve_model_library_sync_proxy(self.db)
        return proxy_url

    async def close_request_transaction(self) -> None:
        await self.db.rollback()

    async def sync_presets(
        self,
        *,
        contents_url: str,
        sync_lock: AsyncLockPort,
        proxy_url: str | None,
    ) -> ApparelModelLibrarySyncOut:
        return await _sync_library_presets_from_github_folder(
            contents_url,
            dependencies=_ApparelLibrarySyncDependencies(sync_lock),
            proxy_url=proxy_url,
        )

    async def ensure_legacy_migrated(self, *, user_id: str) -> None:
        await _ensure_legacy_user_library_migrated(self.db, user_id)

    def remove_legacy_private_item(self, *, user_id: str, item_id: str) -> bool:
        return _remove_user_library_item_from_legacy_index(user_id, item_id)

    async def delete_private_row(self, *, user_id: str, item_id: str) -> bool:
        row = (
            await self.db.execute(
                select(ModelLibraryItem).where(
                    ModelLibraryItem.id == item_id,
                    ModelLibraryItem.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        await self.db.delete(row)
        return True

    async def find_item(
        self,
        *,
        user_id: str,
        item_id: str,
    ) -> dict[str, Any] | None:
        return await _find_library_item(
            self.db,
            user_id=user_id,
            item_id=item_id,
        )

    async def hide_preset(self, *, user_id: str, item_id: str) -> None:
        existing = (
            await self.db.execute(
                select(ModelLibraryHiddenPreset).where(
                    ModelLibraryHiddenPreset.user_id == user_id,
                    ModelLibraryHiddenPreset.preset_id == item_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            self.db.add(
                ModelLibraryHiddenPreset(
                    user_id=user_id,
                    preset_id=item_id,
                )
            )
        _hide_preset_in_legacy_user_library_index(user_id, item_id)

    async def commit(self) -> None:
        await self.db.commit()


def _apparel_library_adapter(
    db: AsyncSession,
) -> _SQLAlchemyApparelLibraryAdapter:
    return _SQLAlchemyApparelLibraryAdapter(db)


async def create_apparel_model_showcase(
    body: ApparelWorkflowCreateIn,
    user: CurrentUser,
    db: AsyncSession,
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
    record_current_paid_operation(db, run)
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


async def list_apparel_model_library(
    user: CurrentUser,
    db: AsyncSession,
    age_segment: AgeSegment = "all",
    source: str = "all",
    appearance: str = "all",
    q: str = "",
) -> ApparelModelLibraryListOut:
    return await ListApparelModelLibrary(_apparel_library_adapter(db)).execute(
        user=cast(ApparelLibraryUser, user),
        age_segment=age_segment,
        source=source,
        appearance=appearance,
        query=q,
    )


async def sync_apparel_model_library_presets(
    user: CurrentUser,
    db: AsyncSession,
    *,
    runtime: WorkflowRuntimeState,
) -> ApparelModelLibrarySyncOut:
    return await SyncApparelModelLibraryPresets(_apparel_library_adapter(db)).execute(
        user=cast(ApparelLibraryUser, user),
        sync_lock=runtime.library_sync_lock,
    )


async def get_apparel_model_library_item_binary(
    item_id: str,
    request: Any,
    user: CurrentUser,
    db: AsyncSession,
) -> Any:
    item = await _find_library_item(db, user_id=user.id, item_id=item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    if item.get("image_id"):
        raise _http("use_image_api", "user library image is served by image API", 400)
    storage_key = str(item.get("image_storage_key") or "").strip()
    return workflow_binary_file(storage_key)


async def get_apparel_model_library_item_thumb(
    item_id: str,
    request: Any,
    user: CurrentUser,
    db: AsyncSession,
) -> Any:
    item = await _find_library_item(db, user_id=user.id, item_id=item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    if item.get("image_id"):
        raise _http("use_image_api", "user library image is served by image API", 400)
    storage_key = str(
        item.get("thumb_storage_key") or item.get("image_storage_key") or ""
    ).strip()
    return workflow_binary_file(storage_key)


async def create_apparel_model_library_item(
    body: ApparelModelLibraryItemCreateIn,
    user: CurrentUser,
    db: AsyncSession,
    background_tasks: Any,
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


async def patch_apparel_model_library_item(
    item_id: str,
    body: ApparelModelLibraryItemPatchIn,
    user: CurrentUser,
    db: AsyncSession,
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


async def delete_apparel_model_library_item(
    item_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> dict[str, bool]:
    return await DeleteApparelModelLibraryItems(
        _apparel_library_adapter(db)
    ).delete_one(
        user_id=user.id,
        item_id=item_id,
    )


async def batch_delete_apparel_model_library_items(
    body: ApparelModelLibraryBatchDeleteIn,
    user: CurrentUser,
    db: AsyncSession,
) -> ApparelModelLibraryBatchDeleteOut:
    result = await DeleteApparelModelLibraryItems(
        _apparel_library_adapter(db)
    ).delete_many(
        user_id=user.id,
        item_ids=body.item_ids,
    )
    return ApparelModelLibraryBatchDeleteOut(
        deleted=result.deleted,
        not_found=list(result.not_found),
    )


async def approve_product_analysis(
    workflow_run_id: str,
    body: ProductAnalysisApproveIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    ensure_product_analysis_ready(product_step)
    model_settings = await _step(db, run.id, "model_settings")
    approve_product_analysis_state(
        run=run,
        product_step=product_step,
        model_settings_step=model_settings,
        corrections=body.corrections,
        user_id=user.id,
        confirmed_at=_now(),
        approved_at=_now(),
    )
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def create_model_candidates(
    workflow_run_id: str,
    body: ModelCandidatesCreateIn,
    user: CurrentUser,
    db: AsyncSession,
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
    record_current_paid_operation(db, run)
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


async def select_apparel_model_library_item(
    workflow_run_id: str,
    body: ApparelModelLibrarySelectIn,
    user: CurrentUser,
    db: AsyncSession,
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
    except WorkflowRequestError:
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
    accessory_plan = resolve_accessory_plan(
        requested=requested_accessory_plan,
        model_settings_output=model_settings.output_json,
        model_settings_input=model_settings.input_json,
        product_analysis=product_step.output_json,
    )
    style_prompt = resolve_style_prompt(
        requested=body.style_prompt,
        model_settings_output=model_settings.output_json,
        model_settings_input=model_settings.input_json,
        fallback=run.user_prompt,
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
