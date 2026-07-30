"""Poster workflow helpers, state synchronization, and endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.constants import (
    Intent,
    Role,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Message,
    PosterMaster,
    PosterRender,
    User,
    WorkflowRun,
    new_uuid7,
)
from lumen_core.schemas import (
    ChatParamsIn,
    CopyAnalysisApproveIn,
    ImageParamsIn,
    PosterDesignWorkflowCreateIn,
    PosterDesignWorkflowCreateOut,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMastersCreateIn,
    PosterRendersCreateIn,
    PosterReviseIn,
    WorkflowRunOut,
)
from ....deps import CurrentUser
from ....services.message_submission import (
    create_assistant_task,
)
from ...application import poster_generation as poster_generation_application
from ...application.output_values import (
    failed_generation_output,
    generation_batch_outcome,
)
from ...application.poster_design import (
    POSTER_DEFAULT_TARGET_ASPECTS,
    POSTER_WORKFLOW_TYPE,
    merge_poster_copy_corrections,
    pending_poster_aspects,
    poster_copy_analysis_prompt as _poster_copy_analysis_prompt,
    poster_parse_copy_analysis_text as _poster_parse_copy_analysis_text,
    poster_revision_prompt as _poster_revision_prompt,
    poster_style_summary as _poster_style_summary,
)
from ...domain.workflow_contracts import PublishBundle
from ...ports.poster_generation import (
    PosterGenerationPort,
    PosterMasterTask,
    PosterRenderTask,
    PosterTaskResult,
)
from ..poster_sync import (
    PosterSyncHooks,
    sync_poster_workflow_outputs as run_poster_output_sync,
)
from ..serialization import (
    dedupe_nonempty as _dedupe_nonempty,
    http as _http,
    now as _now,
)
from ..showcase_inputs import validate_owned_images as _validate_owned_images
from ..workflow_runtime import (
    build_run_out as _build_run_out,
    get_or_create_workflow_conversation as _get_or_create_workflow_conversation,
    get_owned_conversation as _get_owned_conversation,
    get_run as _get_run,
    load_steps as _load_steps,
    publish_bundles as _publish_bundles,
    step as _step,
)
from .poster_helpers import (
    poster_brand_attachment_ids as _poster_brand_attachment_ids,
    poster_image_params as _poster_image_params,
    poster_load_style as _poster_load_style,
    poster_master_image_params as _poster_master_image_params,
    poster_seed_steps as _poster_seed_steps,
)


logger = logging.getLogger("app.routes.workflows.poster")


# ===========================================================================
# Poster Design Workflow（2026-05-12 起）
#
# 设计要点（与 apparel_model_showcase 同源蓝本）：
# 1. workflow_runs.type = "poster_design"；7 个 step：
#    copy_input → style_selection → copy_analysis → master_generation
#    → master_approval → multi_size_generation → delivery
#    （V1 删去 text_layer_editing + quality_review，全 AI 出图 + 文字直塞 prompt）
# 2. 文案分析走 Intent.VISION_QA（纯文本结构化，输出固定 schema JSON）
# 3. 母版生成无品牌图时走 TEXT_TO_IMAGE，有 logo/product 时走 IMAGE_TO_IMAGE；
#    N 个 candidate = N 个独立 Generation 任务，输出 1:1 母版
# 4. 多尺寸成品走 Intent.IMAGE_TO_IMAGE，把母版作为 reference，
#    每个 aspect = 独立 Generation 任务（不在单任务串行多尺寸，遵守 4K timeout 分层）
# 5. inpaint 返修走 Intent.IMAGE_TO_IMAGE + mask_image_id（用户传 mask），
#    prompt 在 worker 侧用 _wrap_inpaint_prompt 包装（OpenAI invariant 模板）
# 6. 风格 prompt 注入：从 PosterStyleItem.prompt_template 读，前缀化拼到母版 prompt
# 7. prompt cache friendly：所有 prompt 前缀稳定（风格 + 信息密度 + 母版指令固定），
#    用户具体文案在末尾
# ===========================================================================
@dataclass(frozen=True, slots=True)
class _PosterWorkflowTaskContext:
    db: AsyncSession
    user: User
    conversation: Conversation


@dataclass(frozen=True, slots=True)
class _PosterWorkflowTaskRequest:
    intent: Intent
    text: str
    attachment_ids: list[str]
    idempotency_key: str
    workflow_run_id: str
    workflow_step_key: str
    image_params: ImageParamsIn | None = None
    chat_params: ChatParamsIn | None = None
    workflow_meta: dict[str, Any] | None = None
    mask_image_id: str | None = None


async def _create_poster_workflow_task(
    *,
    context: _PosterWorkflowTaskContext,
    request: _PosterWorkflowTaskRequest,
) -> tuple[PublishBundle, str | None, list[str]]:
    """与 _create_workflow_task 同源；额外支持 mask_image_id（inpaint）+
    workflow_type=poster_design 标记。"""
    user_msg = Message(
        conversation_id=context.conversation.id,
        role=Role.USER.value,
        content={
            "text": request.text,
            "attachments": [
                {"image_id": image_id} for image_id in request.attachment_ids
            ],
            "workflow_run_id": request.workflow_run_id,
            "workflow_step_key": request.workflow_step_key,
        },
        intent=None,
        status=None,
    )
    context.db.add(user_msg)
    await context.db.flush()
    result = await create_assistant_task(
        db=context.db,
        user_id=context.user.id,
        account_mode=getattr(context.user, "account_mode", "wallet"),
        conv=context.conversation,
        user_msg=user_msg,
        intent=request.intent,
        idempotency_key=request.idempotency_key[:64],
        image_params=request.image_params or ImageParamsIn(),
        chat_params=request.chat_params or ChatParamsIn(),
        system_prompt=None,
        attachment_ids=request.attachment_ids,
        text=request.text,
        mask_image_id=request.mask_image_id,
    )
    meta = {
        "workflow_run_id": request.workflow_run_id,
        "workflow_type": POSTER_WORKFLOW_TYPE,
        "workflow_step_key": request.workflow_step_key,
        **(request.workflow_meta or {}),
    }
    if result.completion_id:
        comp = await context.db.get(Completion, result.completion_id)
        if comp is not None:
            req = dict(comp.upstream_request or {})
            req.update(meta)
            comp.upstream_request = req
    for generation_id in result.generation_ids:
        gen = await context.db.get(Generation, generation_id)
        if gen is not None:
            req = dict(gen.upstream_request or {})
            req.update(meta)
            gen.upstream_request = req
    bundle = PublishBundle(
        assistant_msg_id=result.assistant_msg.id,
        message_ids=[user_msg.id, result.assistant_msg.id],
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    return bundle, result.completion_id, result.generation_ids


def _poster_merge_copy_corrections(
    base: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    return merge_poster_copy_corrections(
        base,
        corrections,
        confirmed_at=_now(),
    )


async def sync_poster_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    await run_poster_output_sync(
        db,
        run,
        workflow_type=POSTER_WORKFLOW_TYPE,
        hooks=PosterSyncHooks(
            load_steps=_load_steps,
            parse_copy_analysis_text=_poster_parse_copy_analysis_text,
            generation_batch_outcome=generation_batch_outcome,
            failed_generation_output=failed_generation_output,
            dedupe_nonempty=_dedupe_nonempty,
        ),
    )


@dataclass(slots=True)
class _PosterGenerationAdapter(PosterGenerationPort):
    db: AsyncSession
    user: User
    conversation: Conversation
    run: WorkflowRun

    async def submit_master(self, task: PosterMasterTask) -> PosterTaskResult:
        master = PosterMaster(
            workflow_run_id=self.run.id,
            candidate_index=task.candidate_index,
            status="generating",
            style_summary_json={
                "style_summary": dict(task.style_summary),
                "copy_analysis": dict(task.copy_analysis),
                "candidate_index": task.candidate_index,
            },
        )
        self.db.add(master)
        await self.db.flush()
        image_params = _poster_master_image_params(task.quality_mode)
        if task.size_mode == "fixed" and task.size:
            image_params = image_params.model_copy(
                update={"size_mode": "fixed", "fixed_size": task.size}
            )
        bundle, _, generation_ids = await _create_poster_workflow_task(
            context=_PosterWorkflowTaskContext(
                self.db,
                self.user,
                self.conversation,
            ),
            request=_PosterWorkflowTaskRequest(
                intent=Intent(task.intent),
                text=task.prompt,
                attachment_ids=list(task.attachment_ids),
                idempotency_key=task.idempotency_key,
                workflow_run_id=self.run.id,
                workflow_step_key="master_generation",
                image_params=image_params,
                workflow_meta={
                    **dict(task.workflow_meta),
                    "workflow_master_id": master.id,
                },
            ),
        )
        master.task_ids = generation_ids
        return PosterTaskResult(
            bundle=bundle,
            generation_ids=tuple(generation_ids),
        )

    async def submit_render(self, task: PosterRenderTask) -> PosterTaskResult:
        image_params = _poster_image_params(
            aspect_ratio=task.aspect_ratio,
            quality_mode=task.quality_mode,
            count=1,
        )
        render = PosterRender(
            workflow_run_id=self.run.id,
            master_id=task.master_id,
            aspect_ratio=task.aspect_ratio,
            size=image_params.fixed_size or "auto",
            status="generating",
            metadata_jsonb={
                "quality_mode": task.quality_mode,
                "use_master_as_reference": task.use_master_as_reference,
            },
        )
        self.db.add(render)
        await self.db.flush()
        bundle, _, generation_ids = await _create_poster_workflow_task(
            context=_PosterWorkflowTaskContext(
                self.db,
                self.user,
                self.conversation,
            ),
            request=_PosterWorkflowTaskRequest(
                intent=Intent(task.intent),
                text=task.prompt,
                attachment_ids=list(task.attachment_ids),
                idempotency_key=task.idempotency_key,
                workflow_run_id=self.run.id,
                workflow_step_key="multi_size_generation",
                image_params=image_params,
                workflow_meta={
                    **dict(task.workflow_meta),
                    "workflow_render_id": render.id,
                },
            ),
        )
        render.task_ids = generation_ids
        return PosterTaskResult(
            bundle=bundle,
            generation_ids=tuple(generation_ids),
        )


# ---- endpoints -------------------------------------------------------------
async def create_poster_design_workflow(
    body: PosterDesignWorkflowCreateIn,
    user: CurrentUser,
    db: AsyncSession,
) -> PosterDesignWorkflowCreateOut:
    """创建海报工作流 + 触发文案分析。
    流程：
    1. 校验 copy_text 非空（pydantic 已校验 min_length=1）
    2. 校验 style_id 存在（_poster_load_style）
    3. 可选校验 brand_assets 中 logo/product image_id 归属当前用户
    4. 创建 WorkflowRun + 7 个 step（copy_input/style_selection 直接 approved）
    5. 入队 vision_qa 任务做文案切分
    """
    copy_text = (body.copy_text or "").strip()
    if not copy_text:
        raise _http("missing_copy_text", "copy_text is required", 422)
    style = await _poster_load_style(db, user_id=user.id, style_id=body.style_id)
    brand_image_ids: list[str] = []
    if body.brand_assets.logo_image_id:
        brand_image_ids.append(body.brand_assets.logo_image_id)
    if body.brand_assets.product_image_id:
        brand_image_ids.append(body.brand_assets.product_image_id)
    if brand_image_ids:
        brand_image_ids = await _validate_owned_images(
            db,
            user_id=user.id,
            image_ids=brand_image_ids,
            min_count=1,
            max_count=8,
        )
    title = (body.title or "").strip() or (copy_text[:24] or "海报设计")
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        conversation_id=body.conversation_id,
        title=title,
        workflow_type=POSTER_WORKFLOW_TYPE,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=POSTER_WORKFLOW_TYPE,
        status="running",
        title=title,
        user_prompt=copy_text,
        product_image_ids=brand_image_ids,  # 复用字段承载品牌资产图（前端按 type 解释）
        current_step="copy_analysis",
        quality_mode=body.quality_mode,
        metadata_jsonb={
            "template": POSTER_WORKFLOW_TYPE,
            "style_id": style.id,
            "style_summary": _poster_style_summary(style),
            "target_aspects": list(body.target_aspects),
            "brand_assets": body.brand_assets.model_dump(),
        },
    )
    db.add(run)
    await db.flush()
    for step in _poster_seed_steps(run):
        db.add(step)
    copy_step = await _step(db, run.id, "copy_analysis")
    bundle, completion_id, _ = await _create_poster_workflow_task(
        context=_PosterWorkflowTaskContext(db, user, conv),
        request=_PosterWorkflowTaskRequest(
            intent=Intent.VISION_QA,
            text=_poster_copy_analysis_prompt(copy_text),
            attachment_ids=[],
            idempotency_key=f"wf:{run.id}:copy",
            workflow_run_id=run.id,
            workflow_step_key="copy_analysis",
            chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
            workflow_meta={"workflow_action": "poster_copy_analysis"},
        ),
    )
    copy_step.task_ids = [completion_id] if completion_id else []
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    return PosterDesignWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


async def approve_copy_analysis(
    workflow_run_id: str,
    body: CopyAnalysisApproveIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    """用户确认（含手工修正）文案分析输出，推进到 master_generation 等待入参。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await sync_poster_workflow_outputs(db, run)
    copy_step = await _step(db, run.id, "copy_analysis")
    if copy_step.status not in {"needs_review", "approved"}:
        raise _http("step_not_ready", "copy analysis is not ready to approve", 409)
    copy_step.output_json = _poster_merge_copy_corrections(
        copy_step.output_json or {},
        body.corrections or {},
    )
    copy_step.status = "approved"
    copy_step.approved_at = _now()
    copy_step.approved_by = user.id
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "waiting_input":
        master_step.input_json = {
            "copy_analysis": copy_step.output_json,
            "style_summary": (run.metadata_jsonb or {}).get("style_summary") or {},
        }
    run.current_step = "master_generation"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def create_poster_masters(
    workflow_run_id: str,
    body: PosterMastersCreateIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    """生成 N 张母版候选（默认 4），每张 = 独立 Generation 任务。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await sync_poster_workflow_outputs(db, run)
    copy_step = await _step(db, run.id, "copy_analysis")
    if copy_step.status != "approved":
        raise _http("copy_not_approved", "approve copy analysis first", 409)
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "running":
        raise _http("already_running", "master generation already running", 409)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    brand_assets = (run.metadata_jsonb or {}).get("brand_assets") or {}
    brand_attachment_ids = _poster_brand_attachment_ids(run)
    copy_analysis = copy_step.output_json or {}
    candidate_count = max(1, min(8, body.candidate_count))
    # 已有 master 行：累加 candidate_index 避免唯一冲突。
    existing_masters = (
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
    existing_count = len(existing_masters)
    master_step.status = "running"
    master_step.task_ids = []
    master_step.image_ids = []
    master_step.output_json = {}
    master_step.input_json = {
        "candidate_count": candidate_count,
        "size_mode": body.size_mode,
        "size": body.size,
        "copy_analysis": copy_analysis,
        "style_summary": style_summary,
        "reference_image_ids": brand_attachment_ids,
    }
    run.current_step = "master_generation"
    run.status = "running"
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    batch = await poster_generation_application.generate_poster_masters(
        poster_generation_application.GeneratePosterMasters(
            run_id=run.id,
            existing_count=existing_count,
            candidate_count=candidate_count,
            style_summary=style_summary,
            copy_analysis=copy_analysis,
            brand_assets=brand_assets,
            brand_attachment_ids=brand_attachment_ids,
            quality_mode=run.quality_mode,
            size_mode=body.size_mode,
            size=body.size,
        ),
        port=_PosterGenerationAdapter(db, user, conv, run),
    )
    master_step.task_ids = _dedupe_nonempty(batch.generation_ids)
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(
        db,
        user_id=user.id,
        conv_id=conv.id,
        bundles=list(batch.bundles),
    )
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def approve_poster_master(
    workflow_run_id: str,
    master_id: str,
    body: PosterMasterApproveIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    """用户选定 1 张母版。其它候选 status 保留 ready，但 selected 字段只有 1 张。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await sync_poster_workflow_outputs(db, run)
    master = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.id == master_id,
                PosterMaster.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if master is None:
        raise _http("not_found", "poster master not found", 404)
    if master.status != "ready" or not master.image_id:
        raise _http("master_not_ready", "poster master is not ready to approve", 409)
    # 把其它已选的 master 切回 ready，保证只有 1 张 selected
    other_selected = (
        (
            await db.execute(
                select(PosterMaster).where(
                    PosterMaster.workflow_run_id == run.id,
                    PosterMaster.status == "selected",
                    PosterMaster.id != master.id,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in other_selected:
        row.status = "ready"
        row.selected_at = None
    adjustments = body.adjustments or ""
    master.status = "selected"
    master.selected_at = _now()
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "needs_review":
        master_step.status = "approved"
        master_step.approved_at = _now()
        master_step.approved_by = user.id
        master_step.output_json = {
            **(master_step.output_json or {}),
            "selected_master_id": master.id,
            "selected_master_image_id": master.image_id,
            "adjustments": adjustments,
        }
    approval_step = await _step(db, run.id, "master_approval")
    approval_step.status = "approved"
    approval_step.approved_at = _now()
    approval_step.approved_by = user.id
    approval_step.input_json = {
        **(approval_step.input_json or {}),
        "selected_master_id": master.id,
        "selected_master_image_id": master.image_id,
        "adjustments": adjustments,
    }
    approval_step.output_json = {
        "selected_master_id": master.id,
        "selected_master_image_id": master.image_id,
        "adjustments": adjustments,
    }
    multi_step = await _step(db, run.id, "multi_size_generation")
    if multi_step.status == "waiting_input":
        multi_step.input_json = {
            **(multi_step.input_json or {}),
            "selected_master_id": master.id,
            "selected_master_image_id": master.image_id,
            "target_aspects": (run.metadata_jsonb or {}).get("target_aspects")
            or list(POSTER_DEFAULT_TARGET_ASPECTS),
            "adjustments": adjustments,
        }
    run.current_step = "multi_size_generation"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _poster_selected_master(db: AsyncSession, run_id: str) -> PosterMaster:
    master = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.workflow_run_id == run_id,
                PosterMaster.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if master is None:
        raise _http("master_not_selected", "select a poster master first", 409)
    if not master.image_id:
        raise _http("master_missing_image", "selected master has no image", 409)
    return master


async def create_poster_renders(
    workflow_run_id: str,
    body: PosterRendersCreateIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    """按 aspect 批量生成多尺寸成品。每个 aspect = 独立 Generation 任务（stagger 入队）。
    复用现有 _create_assistant_task 内部的 stagger（i*5s, cap 30s），
    但因为每次都 count=1，stagger 跨调用不会触发——这与 apparel showcase 同。
    """
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await sync_poster_workflow_outputs(db, run)
    master = await _poster_selected_master(db, run.id)
    master_image_id = master.image_id
    if not master_image_id:
        raise _http("master_missing_image", "selected master has no image", 409)
    multi_step = await _step(db, run.id, "multi_size_generation")
    if multi_step.status == "running":
        raise _http("already_running", "multi-size generation already running", 409)
    aspects = list(dict.fromkeys(body.aspects))
    if not aspects:
        raise _http("missing_aspects", "at least one aspect ratio required", 422)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    copy_step = await _step(db, run.id, "copy_analysis")
    copy_analysis = copy_step.output_json or {}
    approval_step = await _step(db, run.id, "master_approval")
    approval_output = (
        approval_step.output_json if isinstance(approval_step.output_json, dict) else {}
    )
    approval_input = (
        approval_step.input_json if isinstance(approval_step.input_json, dict) else {}
    )
    adjustments = str(
        approval_output.get("adjustments") or approval_input.get("adjustments") or ""
    ).strip()
    brand_attachment_ids = _poster_brand_attachment_ids(run)
    reference_image_ids = _dedupe_nonempty(
        [
            master_image_id if body.use_master_as_reference else "",
            *brand_attachment_ids,
        ]
    )
    quality_mode = (
        body.quality_mode
        if body.quality_mode in {"standard", "premium"}
        else run.quality_mode
    )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 已有 render 行（同 aspect 已生成过则跳过，避免唯一冲突）
    existing_renders = (
        (
            await db.execute(
                select(PosterRender).where(PosterRender.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    requested_aspects, pending_aspect_values = pending_poster_aspects(
        aspects,
        (render.aspect_ratio for render in existing_renders),
    )
    aspects = list(requested_aspects)
    pending_aspects = list(pending_aspect_values)
    multi_step.status = "running"
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "aspects": aspects,
        "use_master_as_reference": body.use_master_as_reference,
        "quality_mode": quality_mode,
        "expected_render_count": len(pending_aspects),
        "active_aspects": pending_aspects,
        "active_task_ids": [],
        "reference_image_ids": reference_image_ids,
        "adjustments": adjustments,
    }
    run.current_step = "multi_size_generation"
    run.status = "running"
    if not pending_aspects:
        requested_image_ids = _dedupe_nonempty(
            r.image_id
            for r in existing_renders
            if r.aspect_ratio in aspects and isinstance(r.image_id, str)
        )
        if not requested_image_ids:
            raise _http(
                "renders_already_exist",
                "requested renders already exist but are not ready",
                409,
            )
        multi_step.status = "needs_review"
        multi_step.image_ids = requested_image_ids
        run.status = "needs_review"
        await db.commit()
        run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
        out = await _build_run_out(db, run)
        await db.commit()
        return out
    batch = await poster_generation_application.generate_poster_renders(
        poster_generation_application.GeneratePosterRenders(
            run_id=run.id,
            master_id=master.id,
            pending_aspects=pending_aspects,
            style_summary=style_summary,
            copy_analysis=copy_analysis,
            reference_image_ids=reference_image_ids,
            quality_mode=quality_mode,
            use_master_as_reference=body.use_master_as_reference,
            adjustments=adjustments,
        ),
        port=_PosterGenerationAdapter(db, user, conv, run),
    )
    task_ids = list(batch.generation_ids)
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *task_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "active_task_ids": _dedupe_nonempty(task_ids),
    }
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(
        db,
        user_id=user.id,
        conv_id=conv.id,
        bundles=list(batch.bundles),
    )
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def revise_poster_render(
    workflow_run_id: str,
    render_id: str,
    body: PosterReviseIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    """单张返修：scope=background/style 走整张 i2i；scope=inpaint 走 mask inpaint。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await sync_poster_workflow_outputs(db, run)
    render = (
        await db.execute(
            select(PosterRender).where(
                PosterRender.id == render_id,
                PosterRender.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if render is None:
        raise _http("not_found", "poster render not found", 404)
    if not render.image_id:
        raise _http("render_no_image", "render has no image yet", 409)
    if body.scope == "inpaint":
        # 走 inpaint 子端点同一逻辑；要求 mask
        return await _do_poster_inpaint(
            db,
            user=user,
            run=run,
            render=render,
            instruction=body.instruction,
            mask_image_id=body.mask_image_id or "",
        )
    master = await _poster_selected_master(db, run.id)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    copy_step = await _step(db, run.id, "copy_analysis")
    copy_analysis = copy_step.output_json or {}
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 参考图：母版 + 当前 render 图（让模型保持版式）
    ref_ids = _dedupe_nonempty([master.image_id or "", render.image_id])
    image_params = _poster_image_params(
        aspect_ratio=render.aspect_ratio,
        quality_mode=str(render.metadata_jsonb.get("quality_mode") or run.quality_mode),
        count=1,
    )
    bundle, _, gen_ids = await _create_poster_workflow_task(
        context=_PosterWorkflowTaskContext(db, user, conv),
        request=_PosterWorkflowTaskRequest(
            intent=Intent.IMAGE_TO_IMAGE,
            text=_poster_revision_prompt(
                style_summary=style_summary,
                copy_analysis=copy_analysis,
                target_aspect=render.aspect_ratio,
                instruction=body.instruction,
                scope=body.scope,
            ),
            attachment_ids=ref_ids,
            idempotency_key=(f"wf:{run.id[:18]}:rv:{render.id[:8]}:{new_uuid7()[:8]}"),
            workflow_run_id=run.id,
            workflow_step_key="multi_size_generation",
            image_params=image_params,
            workflow_meta={
                "workflow_action": "poster_revise",
                "workflow_render_id": render.id,
                "workflow_master_id": master.id,
                "workflow_revision_scope": body.scope,
                "workflow_revision_source_image_id": render.image_id,
            },
        ),
    )
    render.task_ids = [*(render.task_ids or []), *gen_ids]
    render.status = "revising"
    multi_step = await _step(db, run.id, "multi_size_generation")
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *gen_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "expected_render_count": 1,
        "active_render_id": render.id,
        "active_task_ids": _dedupe_nonempty(gen_ids),
    }
    if multi_step.status not in {"running"}:
        multi_step.status = "running"
    run.current_step = "multi_size_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _do_poster_inpaint(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
    render: PosterRender,
    instruction: str,
    mask_image_id: str,
) -> WorkflowRunOut:
    """执行 inpaint：mask + 用户编辑意图 → mask_image_id 透传给 worker，
    worker 侧用 _wrap_inpaint_prompt 包裹（OpenAI invariant 模板，2026-05-07 实测）。"""
    if not mask_image_id:
        raise _http("missing_mask", "inpaint requires mask_image_id", 422)
    # mask 校验：和 render 同一用户
    await _validate_owned_images(
        db,
        user_id=user.id,
        image_ids=[mask_image_id],
        min_count=1,
        max_count=1,
    )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 参考图：当前 render 图作为底图（mask 应用于其上）
    ref_ids = [render.image_id] if render.image_id else []
    quality_mode = str(render.metadata_jsonb.get("quality_mode") or run.quality_mode)
    image_params = _poster_image_params(
        aspect_ratio=render.aspect_ratio,
        quality_mode=quality_mode,
        count=1,
    )
    # prompt：只传用户原始编辑意图（短句），worker 侧会用 invariant 模板包装。
    bundle, _, gen_ids = await _create_poster_workflow_task(
        context=_PosterWorkflowTaskContext(db, user, conv),
        request=_PosterWorkflowTaskRequest(
            intent=Intent.IMAGE_TO_IMAGE,
            text=instruction.strip(),
            attachment_ids=ref_ids,
            idempotency_key=(f"wf:{run.id[:18]}:in:{render.id[:8]}:{new_uuid7()[:8]}"),
            workflow_run_id=run.id,
            workflow_step_key="multi_size_generation",
            image_params=image_params,
            workflow_meta={
                "workflow_action": "poster_inpaint",
                "workflow_render_id": render.id,
                "workflow_revision_source_image_id": render.image_id,
                "workflow_inpaint_mask_image_id": mask_image_id,
            },
            mask_image_id=mask_image_id,
        ),
    )
    render.task_ids = [*(render.task_ids or []), *gen_ids]
    render.status = "revising"
    multi_step = await _step(db, run.id, "multi_size_generation")
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *gen_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "expected_render_count": 1,
        "active_render_id": render.id,
        "active_task_ids": _dedupe_nonempty(gen_ids),
    }
    if multi_step.status not in {"running"}:
        multi_step.status = "running"
    run.current_step = "multi_size_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=run.id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def inpaint_poster_render(
    workflow_run_id: str,
    render_id: str,
    body: PosterInpaintIn,
    user: CurrentUser,
    db: AsyncSession,
) -> WorkflowRunOut:
    """局部 inpaint 单独端点；语义等价于 revise(scope="inpaint")，但 mask 必填。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await sync_poster_workflow_outputs(db, run)
    render = (
        await db.execute(
            select(PosterRender).where(
                PosterRender.id == render_id,
                PosterRender.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if render is None:
        raise _http("not_found", "poster render not found", 404)
    if not render.image_id:
        raise _http("render_no_image", "render has no image yet", 409)
    return await _do_poster_inpaint(
        db,
        user=user,
        run=run,
        render=render,
        instruction=body.instruction,
        mask_image_id=body.mask_image_id,
    )
