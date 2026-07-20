"""Poster-style generation workflow and job services."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus, Intent, Role
from lumen_core.models import (
    Conversation,
    Generation,
    Message,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.schemas import (
    ChatParamsIn,
    ImageParamsIn,
    PosterStyleGenerateIn,
    PosterStyleGenerateOut,
    PosterStyleJobOut,
    PosterStyleJobsOut,
)


def generate_image_params(aspect_ratio: str) -> ImageParamsIn:
    return ImageParamsIn(
        aspect_ratio=aspect_ratio,  # type: ignore[arg-type]
        size_mode="auto",
        count=1,
        fast=False,
        render_quality="high",
        output_format="jpeg",
        output_compression=100,
        background="opaque",
        moderation="low",
    )


async def get_or_create_workflow_conversation(
    runtime: Any,
    db: AsyncSession,
    *,
    user: Any,
    title: str,
    workflow_type: str,
) -> Conversation:
    conversation = Conversation(
        user_id=user.id,
        title=title,
        archived=True,
        default_params={
            "workflow_type": workflow_type,
            "hidden_from_conversations": True,
        },
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def create_user_message(
    runtime: Any,
    db: AsyncSession,
    *,
    conv: Conversation,
    text: str,
    attachment_ids: list[str],
    workflow_run_id: str,
    workflow_step_key: str,
) -> Message:
    message = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": text,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_run_id": workflow_run_id,
            "workflow_step_key": workflow_step_key,
        },
        intent=None,
        status=None,
    )
    db.add(message)
    await db.flush()
    return message


async def enqueue_generate_tasks(
    runtime: Any,
    *,
    db: AsyncSession,
    user: Any,
    conv: Conversation,
    run: WorkflowRun,
    step: WorkflowStep,
    body: PosterStyleGenerateIn,
    create_task_fn: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if create_task_fn is None:
        from ..message_submission import create_assistant_task

        create_task_fn = create_assistant_task

    task_ids: list[str] = []
    publish_jobs: list[dict[str, Any]] = []
    for index in range(1, int(body.count) + 1):
        prompt = runtime._poster_style_generate_prompt(
            body=body,
            candidate_index=index,
        )
        user_message = await runtime._create_user_message(
            db,
            conv=conv,
            text=prompt,
            attachment_ids=[],
            workflow_run_id=run.id,
            workflow_step_key=runtime.POSTER_STYLE_GENERATE_STEP_KEY,
        )
        result = await create_task_fn(
            db=db,
            user_id=user.id,
            account_mode=getattr(user, "account_mode", "wallet"),
            conv=conv,
            user_msg=user_message,
            intent=Intent.TEXT_TO_IMAGE,
            idempotency_key=f"pstyle:{run.id[:24]}:{index}"[:64],
            image_params=runtime._poster_style_generate_image_params(body.aspect_ratio),
            chat_params=ChatParamsIn(),
            system_prompt=None,
            attachment_ids=[],
            text=prompt,
        )
        metadata = {
            "workflow_run_id": run.id,
            "workflow_type": runtime.WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
            "workflow_step_key": runtime.POSTER_STYLE_GENERATE_STEP_KEY,
            "workflow_action": runtime.POSTER_STYLE_GENERATE_WORKER_ACTION,
            "workflow_candidate_index": index,
            "workflow_poster_style_title": body.title,
            "workflow_poster_style_category": runtime._normalize_category(
                body.category
            ),
            "workflow_poster_style_tags": runtime._normalize_style_tags(
                body.style_tags
            ),
            "workflow_poster_style_palette": runtime._normalize_palette(body.palette),
            "workflow_poster_style_auto_tag": bool(body.auto_tag),
        }
        for generation_id in result.generation_ids:
            generation = await db.get(Generation, generation_id)
            if generation is not None:
                request = dict(generation.upstream_request or {})
                request.update(metadata)
                generation.upstream_request = request
        task_ids.extend(result.generation_ids)
        publish_jobs.append(
            {
                "assistant_msg_id": result.assistant_msg.id,
                "outbox_payloads": result.outbox_payloads,
                "outbox_rows": result.outbox_rows,
            }
        )
    step.task_ids = task_ids
    return task_ids, publish_jobs


async def generate_poster_style_samples(
    runtime: Any,
    *,
    body: PosterStyleGenerateIn,
    user: Any,
    db: AsyncSession,
    enqueue_fn: Callable[..., Awaitable[Any]] | None = None,
    publish_fn: Callable[..., Awaitable[Any]] | None = None,
) -> PosterStyleGenerateOut:
    category = runtime._normalize_category(body.category)
    style_tags = runtime._normalize_style_tags(body.style_tags)
    palette = runtime._normalize_palette(body.palette)
    aspects = runtime._normalize_recommended_aspects(body.recommended_aspects)
    title = body.title.strip()[:120] or "未命名风格"

    conversation = await runtime._get_or_create_workflow_conversation(
        db,
        user=user,
        title=title,
        workflow_type=runtime.WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
    )
    run = WorkflowRun(
        conversation_id=conversation.id,
        user_id=user.id,
        type=runtime.WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
        status="running",
        title=title,
        user_prompt=body.prompt[:4000],
        product_image_ids=[],
        current_step=runtime.POSTER_STYLE_GENERATE_STEP_KEY,
        quality_mode="standard",
        metadata_jsonb={
            "template": runtime.WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
            "poster_style_profile": {
                "title": title,
                "category": category,
                "style_tags": style_tags,
                "palette": palette,
                "recommended_aspects": aspects,
                "mood": runtime._clean_optional_text(body.mood, max_len=120),
                "prompt": body.prompt,
            },
        },
    )
    db.add(run)
    await db.flush()
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=runtime.POSTER_STYLE_GENERATE_STEP_KEY,
        status="running",
        input_json={
            "title": title,
            "category": category,
            "style_tags": style_tags,
            "palette": palette,
            "recommended_aspects": aspects,
            "mood": runtime._clean_optional_text(body.mood, max_len=120),
            "prompt": body.prompt,
            "prompt_template": runtime._clean_optional_text(
                body.prompt_template,
                max_len=2000,
            ),
            "aspect_ratio": body.aspect_ratio,
            "count": int(body.count),
            "auto_tag": bool(body.auto_tag),
        },
        output_json={},
    )
    db.add(step)
    await db.flush()
    if enqueue_fn is None:
        enqueue_fn = runtime._enqueue_poster_style_generate_tasks
    task_ids, publish_jobs = await enqueue_fn(
        db=db,
        user=user,
        conv=conversation,
        run=run,
        step=step,
        body=body,
    )
    conversation.last_activity_at = runtime._now()
    await db.commit()
    if publish_jobs:
        if publish_fn is None:
            publish_fn = runtime._publish_poster_style_assistant_task
        for job in publish_jobs:
            await publish_fn(
                db=db,
                redis=runtime.get_redis(),
                user_id=user.id,
                conv_id=conversation.id,
                assistant_msg_id=str(job["assistant_msg_id"]),
                outbox_payloads=list(job["outbox_payloads"]),
                outbox_rows=list(job["outbox_rows"]),
            )
    return PosterStyleGenerateOut(
        job_id=run.id,
        workflow_run_id=run.id,
        status="running",
        requested_count=int(body.count),
        task_ids=task_ids,
        created_at=run.created_at,
    )


def job_status(
    *,
    step_status: str,
    requested_count: int,
    finished_count: int,
) -> str:
    if step_status == "failed":
        return "partial" if finished_count > 0 else "failed"
    if step_status in {"succeeded", "completed", "approved", "needs_review"}:
        if requested_count > 0 and finished_count >= requested_count:
            return "succeeded"
        if finished_count > 0:
            return "partial"
        return "succeeded" if step_status == "succeeded" else "failed"
    if step_status == "running":
        return "running"
    return "queued"


async def job_from_run(
    runtime: Any,
    db: AsyncSession,
    *,
    run: WorkflowRun,
) -> PosterStyleJobOut:
    step = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == runtime.POSTER_STYLE_GENERATE_STEP_KEY,
            )
        )
    ).scalar_one_or_none()
    inputs: dict[str, Any] = {}
    image_ids: list[str] = []
    requested = 0
    step_status = "queued"
    if step is not None:
        inputs = step.input_json if isinstance(step.input_json, dict) else {}
        image_ids = [
            value for value in (step.image_ids or []) if isinstance(value, str)
        ]
        requested = max(
            int(inputs.get("count") or 0),
            len(step.task_ids or []),
            len(image_ids),
        )
        step_status = step.status
    finished = len(image_ids)

    error_message: str | None = None
    if step is not None:
        output = step.output_json if isinstance(step.output_json, dict) else {}
        error_message = runtime._clean_optional_text(
            output.get("error_message"),
            max_len=400,
        )
        if step.task_ids:
            generations = list(
                (
                    await db.execute(
                        select(Generation).where(
                            Generation.id.in_(list(step.task_ids)),
                            Generation.user_id == run.user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
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
            if failed and not active and finished < requested:
                if step_status == "running":
                    step_status = "failed"
                if error_message is None:
                    messages = [
                        str(getattr(generation, "error_message", "") or "").strip()
                        for generation in failed
                    ]
                    error_message = (
                        "；".join(message for message in messages if message)[:400]
                        or "生成失败"
                    )

    status = runtime._poster_style_job_status(
        step_status=step_status,
        requested_count=requested,
        finished_count=finished,
    )
    saved_item_id: str | None = None
    if image_ids:
        saved_item_id = (
            await db.execute(
                select(runtime.PosterStyleItem.id)
                .where(
                    runtime.PosterStyleItem.user_id == run.user_id,
                    runtime.PosterStyleItem.cover_image_id.in_(image_ids),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if not isinstance(saved_item_id, str):
            saved_item_id = None
    return PosterStyleJobOut(
        job_id=run.id,
        workflow_run_id=run.id,
        title=str(run.title or "")[:120],
        category=runtime._normalize_category(inputs.get("category")),  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        requested_count=requested,
        finished_count=finished,
        prompt=runtime._clean_optional_text(inputs.get("prompt"), max_len=2000),
        style_tags=runtime._normalize_style_tags(inputs.get("style_tags") or []),
        image_ids=image_ids,
        saved_item_id=saved_item_id,
        error_message=error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def list_poster_style_jobs(
    runtime: Any,
    *,
    user: Any,
    db: AsyncSession,
    limit: int,
    offset: int,
) -> PosterStyleJobsOut:
    fetch_limit = offset + limit + 1
    runs = list(
        (
            await db.execute(
                select(WorkflowRun)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == runtime.WORKFLOW_TYPE_POSTER_STYLE_GENERATE,
                )
                .order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id))
                .limit(fetch_limit)
            )
        )
        .scalars()
        .all()
    )
    jobs = [await runtime._job_from_run(db, run=run) for run in runs]
    return PosterStyleJobsOut(
        items=jobs[offset : offset + limit],
        limit=limit,
        offset=offset,
        has_more=len(jobs) > offset + limit,
    )
