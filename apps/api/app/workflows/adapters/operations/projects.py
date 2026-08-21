"""Workflow project application endpoints.

This module owns workflow project orchestration. The public route module only
assembles the router and re-exports stable HTTP endpoint callables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    Intent,
)
from lumen_core.models import (
    Conversation,
    Image,
    ModelCandidate,
    new_uuid7,
    QualityReport,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schemas import (
    AccessoryPreviewCreateIn,
    AccessorySelectionIn,
    ApparelModelLibraryItemOut,
    ImageRevisionIn,
    ModelCandidateApproveIn,
    ModelCandidateSaveToLibraryIn,
    ShowcaseImagesCreateIn,
    WorkflowRunOut,
)

from ....deps import CurrentUser
from ...application.apparel_workflow_rules import (
    infer_age_segment_from_workflow as _infer_age_segment_from_workflow,
    primary_candidate_image_id as _primary_candidate_image_id,
    resolve_accessory_plan,
    resolve_style_prompt,
)
from ...application.http_contracts import WorkflowAssetsAddIn
from ...application.project_candidate_rules import (
    accessory_preview_request_key as _accessory_preview_request_key,
    apply_accessory_selection_state,
    approve_model_candidate_state,
    ensure_model_candidate_ready,
    reopen_model_selection_state,
    revision_prompt,
    saved_library_item_ids,
)
from ..paid_idempotency import record_current_paid_operation
from ...application.project_lifecycle import ProjectLifecycle
from ...application.upsert_project import UpsertWorkflowProject
from ...application.runtime_state import WorkflowRuntimeState
from ...application.showcase_prompts import showcase_prompt as _showcase_prompt
from ...application.values import dedupe_nonempty as _dedupe_nonempty
from ...application.errors import WorkflowRequestError
from ...domain.workflow_contracts import PublishBundle as _PublishBundle
from ...ports.project_lifecycle import ProjectRunRecord
from ...domain.json_types import JsonValue
from ..apparel_scene_planner import scene_fingerprint as _scene_fingerprint
from ..library_items import model_library_item_out as _model_library_item_out
from ..library_materialization import add_user_library_item as _add_user_library_item
from ..output_sync import sync_workflow_outputs as _sync_workflow_outputs
from ..showcase_context import (
    prepare_durable_showcase_preflight as _prepare_durable_showcase_preflight,
)
from ..showcase_context import (
    showcase_generation_context as _showcase_generation_context,
)
from ..showcase_context import (
    showcase_request_input_json as _showcase_request_input_json,
)
from ..showcase_inputs import (
    validate_accessory_preview_image as _validate_accessory_preview_image,
)
from ..workflow_runtime import (
    accessory_preview_image_params as _accessory_preview_image_params,
)
from ..workflow_runtime import accessory_preview_prompt as _accessory_preview_prompt
from ..workflow_runtime import attach_workflow_assets as _attach_workflow_assets
from ..workflow_runtime import build_run_out as _build_run_out
from ..workflow_runtime import create_workflow_task as _create_workflow_task
from ..workflow_runtime import get_owned_conversation as _get_owned_conversation
from ..workflow_runtime import get_run as _get_run
from ..workflow_runtime import image_params as _image_params
from ..workflow_runtime import (
    post_commit_workflow_generated_cleanup as _post_commit_workflow_generated_cleanup,
)
from ..workflow_runtime import publish_bundles as _publish_bundles
from ..workflow_runtime import (
    soft_delete_workflow_generated_images as _soft_delete_workflow_generated_images,
)
from ..workflow_runtime import step as _step
from .model_library import (
    run_auto_tag_in_background as _run_auto_tag_in_background,
)
from .poster import (
    POSTER_WORKFLOW_TYPE,
    sync_poster_workflow_outputs as _sync_poster_workflow_outputs,
)

logger = logging.getLogger(__name__)


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


def _iso_now() -> str:
    return _now().isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class _SQLAlchemyProjectLifecycleAdapter:
    db: AsyncSession

    async def get_owned_run(
        self,
        *,
        user_id: str,
        run_id: str,
        for_update: bool = False,
    ) -> ProjectRunRecord:
        return await _get_run(
            self.db,
            user_id=user_id,
            run_id=run_id,
            lock=for_update,
        )

    async def rename_active_conversation(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str,
    ) -> None:
        conversation = (
            await self.db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conversation is not None:
            conversation.title = title

    async def mark_active_conversation_deleted(
        self,
        *,
        conversation_id: str,
        user_id: str,
        deleted_at: datetime,
    ) -> None:
        conversation = (
            await self.db.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user_id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conversation is not None:
            conversation.deleted_at = deleted_at

    async def sync_standard_outputs(self, run: ProjectRunRecord) -> None:
        await _sync_workflow_outputs(self.db, cast(WorkflowRun, run))

    async def sync_poster_outputs(self, run: ProjectRunRecord) -> None:
        await _sync_poster_workflow_outputs(self.db, cast(WorkflowRun, run))

    async def build_run_out(self, run: ProjectRunRecord) -> WorkflowRunOut:
        return await _build_run_out(self.db, cast(WorkflowRun, run))

    async def soft_delete_generated_images(
        self,
        *,
        run: ProjectRunRecord,
        deleted_at: datetime,
        cancel_message: str,
        account_mode: str,
    ) -> Mapping[str, JsonValue]:
        return cast(
            Mapping[str, JsonValue],
            await _soft_delete_workflow_generated_images(
                self.db,
                run=cast(WorkflowRun, run),
                deleted_at=deleted_at,
                cancel_message=cancel_message,
                account_mode=account_mode,
            ),
        )

    async def post_commit_generated_cleanup(
        self,
        *,
        user_id: str,
        cleanup: Mapping[str, JsonValue],
    ) -> None:
        await _post_commit_workflow_generated_cleanup(
            user_id=user_id,
            cleanup=cleanup,
        )

    async def attach_assets(
        self,
        *,
        run: ProjectRunRecord,
        user_id: str,
        image_ids: Sequence[str],
        asset_type: str,
        source_step_key: str,
        label: str | None,
    ) -> None:
        await _attach_workflow_assets(
            self.db,
            run=cast(WorkflowRun, run),
            user_id=user_id,
            image_ids=list(image_ids),
            asset_type=asset_type,
            source_step_key=source_step_key,
            label=label,
        )

    async def commit(self) -> None:
        await self.db.commit()


def build_project_lifecycle(db: AsyncSession) -> ProjectLifecycle:
    adapter = _SQLAlchemyProjectLifecycleAdapter(db)
    return ProjectLifecycle(
        repository=adapter,
        outputs=adapter,
        assets=adapter,
        now=_now,
    )


def build_upsert_workflow_project(db: AsyncSession) -> UpsertWorkflowProject:
    adapter = _SQLAlchemyProjectLifecycleAdapter(db)
    return UpsertWorkflowProject(repository=adapter, outputs=adapter)


async def get_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    return cast(
        WorkflowRunOut,
        await build_project_lifecycle(db).get(
            user_id=user.id,
            run_id=workflow_run_id,
        ),
    )


async def reconcile_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    return cast(
        WorkflowRunOut,
        await build_project_lifecycle(db).reconcile(
            user_id=user.id,
            run_id=workflow_run_id,
        ),
    )


async def add_workflow_assets(
    workflow_run_id: str,
    body: WorkflowAssetsAddIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    return cast(
        WorkflowRunOut,
        await build_project_lifecycle(db).add_assets(
            user_id=user.id,
            run_id=workflow_run_id,
            image_ids=body.image_ids,
            asset_type=body.asset_type,
            source_step_key=body.source_step_key,
            label=body.label,
        ),
    )


async def save_model_candidate_to_library(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateSaveToLibraryIn,
    user: CurrentUser,
    db: AsyncSession,
    background_tasks: Any,
) -> ApparelModelLibraryItemOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    image_id = _primary_candidate_image_id(candidate)
    if not image_id:
        raise _http("candidate_image_missing", "candidate has no image to save", 422)
    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source="favorite",
        image_id=image_id,
        title=body.title,
        age_segment=body.age_segment or _infer_age_segment_from_workflow(run),
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    brief = dict(candidate.model_brief_json or {})
    raw_saved_ids = brief.get("saved_library_item_ids")
    existing_saved_ids = (
        [value for value in raw_saved_ids if isinstance(value, str)]
        if isinstance(raw_saved_ids, list)
        else []
    )
    saved_ids = saved_library_item_ids(
        existing_saved_ids,
        item.get("id"),
    )
    brief["saved_library_item_ids"] = saved_ids
    candidate.model_brief_json = brief
    await db.commit()
    # 项目流程里收藏到模特库：用户已经在标注里填了字段，但仍后台触发一次 vision
    # 校正/补全（appearance_direction / style_tags 默认空时常见）。
    item_id = str(item.get("id") or "")
    if item_id:
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


async def approve_model_candidate(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateApproveIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    ensure_model_candidate_ready(candidate)
    selected_accessory_image_id = body.selected_accessory_image_id
    approval = await _step(db, run.id, "model_approval")
    if selected_accessory_image_id:
        await _validate_accessory_preview_image(
            db,
            user_id=user.id,
            run_id=run.id,
            approval_step=approval,
            image_id=selected_accessory_image_id,
        )
    all_candidates = (
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    showcase = await _step(db, run.id, "showcase_generation")
    approve_model_candidate_state(
        candidates=all_candidates,
        selected_candidate=candidate,
        approval_step=approval,
        showcase_step=showcase,
        run=run,
        user_id=user.id,
        now=now,
        adjustments=body.adjustments,
        accessory_plan=body.accessory_plan.model_dump(),
        selected_accessory_image_id=selected_accessory_image_id,
    )
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def reopen_model_selection(
    workflow_run_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidates = (
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if candidate.status in {"selected", "rejected"}:
            candidate.status = (
                "ready" if candidate.contact_sheet_image_id else "generating"
            )
            candidate.selected_at = None
    approval = await _step(db, run.id, "model_approval")
    previous_approval_input = dict(approval.input_json or {})
    candidate_step = await _step(db, run.id, "model_candidates")
    model_settings = await _step(db, run.id, "model_settings")
    product_step = await _step(db, run.id, "product_analysis")
    preserved_accessory_plan = resolve_accessory_plan(
        requested=previous_approval_input.get("accessory_plan"),
        model_settings_output=candidate_step.input_json,
        model_settings_input=model_settings.output_json,
        product_analysis=product_step.output_json,
    )
    preserved_style_prompt = resolve_style_prompt(
        requested=str(previous_approval_input.get("style_prompt") or ""),
        model_settings_output=candidate_step.input_json,
        model_settings_input=model_settings.output_json,
        fallback="",
    )
    showcase = await _step(db, run.id, "showcase_generation")
    quality = await _step(db, run.id, "quality_review")
    await db.execute(
        delete(QualityReport).where(QualityReport.workflow_run_id == run.id)
    )
    delivery = await _step(db, run.id, "delivery")
    reopen_model_selection_state(
        candidates=candidates,
        approval_step=approval,
        candidate_step=candidate_step,
        showcase_step=showcase,
        quality_step=quality,
        delivery_step=delivery,
        run=run,
        accessory_plan=preserved_accessory_plan,
        style_prompt=preserved_style_prompt,
    )
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def create_accessory_previews(
    workflow_run_id: str,
    body: AccessoryPreviewCreateIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == body.candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "selected" or not candidate.contact_sheet_image_id:
        raise _http(
            "model_not_selected",
            "select and approve a model candidate before generating accessory previews",
            409,
        )
    approval = await _step(db, run.id, "model_approval")
    accessory_plan_payload = body.accessory_plan.model_dump()
    preview_request_key = _accessory_preview_request_key(
        candidate_id=candidate.id,
        accessory_plan=accessory_plan_payload,
        style_prompt=body.style_prompt,
    )
    record_current_paid_operation(db, run)
    existing_task_ids = _dedupe_nonempty(approval.task_ids or [])
    existing_input = approval.input_json or {}
    if approval.status == "running" and existing_task_ids:
        if existing_input.get("accessory_preview_request_key") == preview_request_key:
            run.current_step = "model_approval"
            run.status = "running"
            out = await _build_run_out(db, run)
            await db.commit()
            return out
        raise _http(
            "already_running",
            "accessory preview generation already running",
            409,
        )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    brief = candidate.model_brief_json or {}
    age_context = " ".join(
        str(part)
        for part in (
            run.user_prompt,
            brief.get("summary") if isinstance(brief, dict) else None,
            body.style_prompt,
        )
        if part
    )
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_accessory_preview_prompt(
            accessory_plan=accessory_plan_payload,
            style_prompt=body.style_prompt,
            age_context=age_context,
        ),
        attachment_ids=[candidate.contact_sheet_image_id],
        idempotency_key=f"wf:{run.id[:12]}:acc:{candidate.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="model_approval",
        image_params=_accessory_preview_image_params(),
        workflow_meta={
            "workflow_action": "accessory_preview",
            "workflow_candidate_id": candidate.id,
        },
    )
    approval.status = "running"
    approval.task_ids = _dedupe_nonempty([*existing_task_ids, *gen_ids])
    approval.input_json = {
        **(approval.input_json or {}),
        "candidate_id": candidate.id,
        "accessory_plan": accessory_plan_payload,
        "style_prompt": body.style_prompt,
        "accessory_preview_request_key": preview_request_key,
        "accessory_preview_started_at": _iso_now(),
    }
    run.current_step = "model_approval"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def save_accessory_selection(
    workflow_run_id: str,
    body: AccessorySelectionIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    approval = await _step(db, run.id, "model_approval")
    selected_image_id = body.selected_accessory_image_id
    if selected_image_id:
        await _validate_accessory_preview_image(
            db,
            user_id=user.id,
            run_id=run.id,
            approval_step=approval,
            image_id=selected_image_id,
        )
    apply_accessory_selection_state(
        approval_step=approval,
        run=run,
        selected_accessory_image_id=selected_image_id,
    )
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _selected_candidate(db: AsyncSession, run_id: str) -> ModelCandidate:
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.workflow_run_id == run_id,
                ModelCandidate.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("model_not_approved", "approve a model candidate first", 409)
    return candidate


async def _dispatch_showcase_images_generation(
    *,
    db: AsyncSession,
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: User,
    runtime: WorkflowRuntimeState,
) -> WorkflowRun:
    context = await _showcase_generation_context(
        db=db,
        user=user,
        workflow_run_id=workflow_run_id,
        body=body,
    )
    run: WorkflowRun = context["run"]
    showcase: WorkflowStep = context["showcase"]
    record_current_paid_operation(db, run)
    if showcase.status == "running" and _dedupe_nonempty(showcase.task_ids or []):
        await db.commit()
        return run

    request_id = new_uuid7()
    preflight_started_at = _iso_now()
    preflight = await _prepare_durable_showcase_preflight(
        db=db,
        context=context,
        body=body,
        provider_runtime=runtime.scene_provider_round_robin,
    )
    product_step: WorkflowStep = context["product_step"]
    candidate: ModelCandidate = context["candidate"]
    conv: Conversation = context["conv"]
    scene_cards = list(preflight.get("scene_cards") or [])
    final_prompts = list(preflight.get("final_prompts") or [])
    existing_image_ids = _dedupe_nonempty(showcase.image_ids or [])
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    for idx, (shot_type, variant) in enumerate(context["shot_picks"], start=1):
        scene_card = scene_cards[idx - 1] if idx - 1 < len(scene_cards) else {}
        final_prompt = (
            str(final_prompts[idx - 1])
            if idx - 1 < len(final_prompts) and final_prompts[idx - 1]
            else _showcase_prompt(
                product_analysis=product_step.output_json or {},
                selected_candidate=candidate,
                accessory_plan=context["accessory_plan"],
                template=body.template,
                shot_type=shot_type,
                shot_variant=variant,
                age_segment=context["age_segment"],
                final_quality=body.final_quality,
                user_prompt=run.user_prompt,
                aspect_ratio=body.aspect_ratio,
                scene_environment=body.scene_environment,
                scene_card=scene_card,
                garment_lock=preflight.get("garment_lock"),
                allow_pet=body.allow_pet,
                allow_background_people=body.allow_background_people,
            )
        )
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.IMAGE_TO_IMAGE,
            text=final_prompt,
            attachment_ids=context["ref_ids"],
            idempotency_key=f"wf:{run.id[:12]}:show:{request_id[:12]}:{idx}",
            workflow_run_id=run.id,
            workflow_step_key="showcase_generation",
            image_params=_image_params(
                aspect_ratio=body.aspect_ratio,
                count=1,
                render_quality="high" if body.final_quality != "standard" else "medium",
                final_quality=body.final_quality,
            ),
            workflow_meta={
                "workflow_action": "showcase_image",
                "workflow_candidate_id": candidate.id,
                "workflow_shot_type": shot_type,
                "workflow_shot_variant": variant["label"],
                "workflow_shot_framing": variant["framing"],
                "workflow_template": body.template,
                "workflow_age_segment": context["age_segment"],
                "workflow_final_quality": body.final_quality,
                "workflow_scene_environment": body.scene_environment,
                "workflow_scene_strategy": body.scene_strategy,
                "workflow_scene_variety": body.scene_variety,
                "workflow_scene_planner": body.scene_planner,
                "workflow_scene_planner_effective": (
                    preflight.get("planning") or {}
                ).get("planner"),
                "workflow_scene_card_id": scene_card.get("id"),
                "workflow_scene_family": scene_card.get("scene_family"),
                "workflow_camera_angle": (scene_card.get("camera") or {}).get("angle")
                if isinstance(scene_card.get("camera"), dict)
                else None,
                "workflow_micro_event": scene_card.get("micro_event"),
                "workflow_scene_fingerprint": scene_card.get("fingerprint")
                or _scene_fingerprint(scene_card),
            },
        )
        task_ids.extend(gen_ids)
        bundles.append(bundle)

    if not task_ids:
        raise _http(
            "showcase_dispatch_failed",
            "showcase generation produced no durable tasks",
            500,
        )

    showcase.status = "running"
    showcase.task_ids = _dedupe_nonempty(task_ids)
    showcase.image_ids = existing_image_ids
    showcase.input_json = {
        **_showcase_request_input_json(
            body=body,
            request_id=request_id,
            shot_picks=context["shot_picks"],
            age_segment=context["age_segment"],
            ref_ids=context["ref_ids"],
            existing_image_ids=existing_image_ids,
            preflight_status="dispatched",
            active_task_ids=task_ids,
            preflight=preflight,
        ),
        "dispatch_mode": "transactional_outbox",
        "preflight_started_at": preflight_started_at,
        "preflight_completed_at": _iso_now(),
        "preflight_phase": "dispatching",
        "preflight_phase_detail": "生成任务已持久化，等待 worker 执行",
        "preflight_phase_current": len(task_ids),
        "preflight_phase_total": len(task_ids),
    }
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    try:
        await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "showcase fast-path publish failed; outbox will retry "
            "user=%s run=%s request=%s err=%s",
            user.id,
            workflow_run_id,
            request_id,
            exc,
            exc_info=True,
        )
    return run


async def create_showcase_images(
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: CurrentUser,
    db: AsyncSession,
    *,
    runtime: WorkflowRuntimeState,
) -> WorkflowRunOut:
    run = await _dispatch_showcase_images_generation(
        db=db,
        workflow_run_id=workflow_run_id,
        body=body,
        user=user,
        runtime=runtime,
    )
    return await _build_run_out(db, run)


async def revise_showcase_image(
    workflow_run_id: str,
    image_id: str,
    body: ImageRevisionIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if image_id not in set(showcase.image_ids or []):
        raise _http(
            "invalid_image", "image is not a showcase output for this workflow", 404
        )
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise _http("not_found", "image not found", 404)
    record_current_paid_operation(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    candidate = await _selected_candidate(db, run.id)
    refs = _dedupe_nonempty(
        [*run.product_image_ids, candidate.contact_sheet_image_id or "", image_id]
    )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    revision_index = len(showcase.task_ids or []) + 1
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=revision_prompt(
            instruction=body.instruction,
            product_analysis=product_step.output_json or {},
            selected_candidate_id=candidate.id,
        ),
        attachment_ids=refs,
        idempotency_key=f"wf:{run.id[:22]}:rev:{revision_index}",
        workflow_run_id=run.id,
        workflow_step_key="showcase_generation",
        image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
        workflow_meta={
            "workflow_action": "revision",
            "workflow_revision_source_image_id": image_id,
            "workflow_revision_scope": body.scope,
        },
    )
    showcase.task_ids = [*(showcase.task_ids or []), *gen_ids]
    showcase.status = "running"
    showcase.input_json = {
        **(showcase.input_json or {}),
        "active_task_ids": gen_ids,
        "active_output_count": len(gen_ids) or 1,
        "active_task_kind": "revision",
        "baseline_image_count": len(_dedupe_nonempty(showcase.image_ids or [])),
        "preflight_status": "dispatched",
    }
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {
        **(quality.input_json or {}),
        "latest_revision": {
            "source_image_id": image_id,
            "instruction": body.instruction,
            "scope": body.scope,
        },
    }
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def complete_delivery(
    workflow_run_id: str,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type == POSTER_WORKFLOW_TYPE:
        await _sync_poster_workflow_outputs(db, run)
        multi_step = await _step(db, run.id, "multi_size_generation")
        image_ids = _dedupe_nonempty(multi_step.image_ids or [])
        if not image_ids:
            raise _http("no_outputs", "generate poster renders before delivery", 409)
        delivery = await _step(db, run.id, "delivery")
        now = _now()
        multi_step.status = "completed"
        multi_step.approved_at = now
        multi_step.approved_by = user.id
        delivery.status = "completed"
        delivery.approved_at = now
        delivery.approved_by = user.id
        delivery.input_json = {
            **(delivery.input_json or {}),
            "final_image_ids": image_ids,
        }
        delivery.output_json = {
            **(delivery.output_json or {}),
            "download_image_ids": image_ids,
            "completed_at": now.isoformat(),
        }
        await _attach_workflow_assets(
            db,
            run=run,
            user_id=user.id,
            image_ids=image_ids,
            asset_type="poster_delivery",
            source_step_key="delivery",
            label="海报交付",
            added_at=now,
        )
        run.status = "completed"
        run.current_step = "delivery"
        out = await _build_run_out(db, run)
        await db.commit()
        return out

    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if not showcase.image_ids:
        raise _http("no_outputs", "generate showcase images before delivery", 409)
    quality = await _step(db, run.id, "quality_review")
    delivery = await _step(db, run.id, "delivery")
    now = _now()
    quality.status = "approved"
    quality.approved_at = now
    quality.approved_by = user.id
    delivery.status = "completed"
    delivery.approved_at = now
    delivery.approved_by = user.id
    delivery.input_json = {"final_image_ids": showcase.image_ids}
    delivery.output_json = {
        "download_image_ids": showcase.image_ids,
        "completed_at": now.isoformat(),
    }
    run.status = "completed"
    run.current_step = "delivery"
    out = await _build_run_out(db, run)
    await db.commit()
    return out
