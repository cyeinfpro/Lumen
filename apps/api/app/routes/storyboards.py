"""Storyboard project routes.

This module keeps the redesigned storyboard workflow out of the monolithic
``/video`` page and out of the existing apparel/poster workflow routes.  The
state is still stored in WorkflowRun/WorkflowStep so no new table is required.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.arq_jobs import arq_job_id
from lumen_core.constants import (
    Intent,
    MAX_PROMPT_CHARS,
    Role,
)
from lumen_core.models import (
    Conversation,
    Message,
    OutboxEvent,
    User,
    Video,
    VideoGeneration,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.schemas import ChatParamsIn, ImageParamsIn, VideoCreateIn

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..redis_client import get_redis
from ..sse_publish import publish_sse_event
from .messages import _create_assistant_task, _publish_message_appended
from .videos import _create_video_generation_record, _video_out
from ..services.storyboard import assembly as storyboard_assembly
from ..services.storyboard import output_sync as storyboard_output_sync
from ..services.storyboard import patching as storyboard_patching
from ..services.storyboard import repository as storyboard_repository
from ..services.storyboard import serialization as storyboard_serialization
from ..services.storyboard import tasks as storyboard_tasks
from ..services.storyboard.common import (
    STORYBOARD_ASSEMBLY_WAITING_LEASE_S,
    STORYBOARD_DEFAULT_ASPECT_RATIO,
    STORYBOARD_DEFAULT_DURATION_S,
    STORYBOARD_DEFAULT_MODEL,
    STORYBOARD_DEFAULT_RESOLUTION,
    STORYBOARD_WORKFLOW_TYPE,
    asset_step_key,
    clear_shot_video_output,
    clean_string_list,
    default_storyboard_metadata,
    http_error,
    merge_run_metadata,
    normalize_shot_indexes,
    rank_status,
    run_metadata,
    short_hash,
    shot_source_hash,
    shot_step_key,
    step_kind,
    storyboard_channel,
    utc_now,
)
from ..services.storyboard.contracts import (
    StoryboardAssetCreateIn,
    StoryboardAssetPatchIn,
    StoryboardCreateIn,
    StoryboardGenerateIn,
    StoryboardImageTask,
    StoryboardPatchIn,
    StoryboardRunListItemOut,
    StoryboardRunListOut,
    StoryboardRunOut,
    StoryboardShotCreateIn,
    StoryboardShotMoveIn,
    StoryboardShotOut,
    StoryboardShotPatchIn,
    StoryboardShotsRebuildIn,
    StoryboardSubmitShotIn,
)


router = APIRouter(prefix="/storyboards", tags=["storyboards"])
logger = logging.getLogger(__name__)

STORYBOARD_CHANNEL_PREFIX = "storyboard:"
STORYBOARD_ASSET_KINDS = {"character", "scene", "prop"}
STORYBOARD_ASSEMBLY_WORKER_LEASE_S = (
    storyboard_assembly.STORYBOARD_ASSEMBLY_WORKER_LEASE_S
)
STORYBOARD_KEYFRAME_PARALLELISM = storyboard_tasks.STORYBOARD_KEYFRAME_PARALLELISM


def _http(code: str, msg: str, http: int = 400, **details: Any) -> HTTPException:
    return http_error(code, msg, http, **details)


_now = utc_now
_clean_string_list = clean_string_list
_short_hash = short_hash
_asset_step_key = asset_step_key
_shot_step_key = shot_step_key
_step_kind = step_kind
_clear_shot_video_output = clear_shot_video_output
_run_metadata = run_metadata
_default_storyboard_metadata = default_storyboard_metadata
_merge_run_metadata = merge_run_metadata


async def _publish_storyboard_event(
    user_id: str,
    run_id: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    try:
        await publish_sse_event(
            get_redis(),
            user_id=user_id,
            channel=storyboard_channel(run_id),
            event_name=event_name,
            data={"storyboard_id": run_id, **data},
        )
    except Exception:
        logger.warning(
            "storyboard SSE publish failed user=%s run=%s event=%s",
            user_id,
            run_id,
            event_name,
            exc_info=True,
        )


async def _get_owned_conversation(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    return await storyboard_repository.get_owned_conversation(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
    )


async def _get_or_create_storyboard_conversation(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
) -> Conversation:
    return await storyboard_repository.get_or_create_storyboard_conversation(
        db,
        user=user,
        run=run,
    )


async def _get_run(
    db: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    lock: bool = False,
) -> WorkflowRun:
    return await storyboard_repository.get_run(
        db,
        user_id=user_id,
        run_id=run_id,
        lock=lock,
    )


async def _load_steps(
    db: AsyncSession,
    run_id: str,
    *,
    lock: bool = False,
) -> list[WorkflowStep]:
    return await storyboard_repository.load_steps(db, run_id, lock=lock)


async def _get_step(
    db: AsyncSession,
    run: WorkflowRun,
    step_id: str,
    *,
    kind: Literal["asset", "shot"] | None = None,
    lock: bool = False,
) -> WorkflowStep:
    return await storyboard_repository.get_step(
        db,
        run,
        step_id,
        kind=kind,
        lock=lock,
    )


async def _assembly_step(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    lock: bool = False,
) -> WorkflowStep:
    return await storyboard_repository.assembly_step(db, run, lock=lock)


_asset_out = storyboard_serialization.asset_out
_shot_source_hash = shot_source_hash


_storyboard_video_submission_fingerprint = (
    storyboard_assembly.storyboard_video_submission_fingerprint
)


def _new_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step_id: str,
    submission_fingerprint: str,
) -> str:
    return storyboard_assembly.new_storyboard_video_idempotency_key(
        run_id=run_id,
        step_id=step_id,
        submission_fingerprint=submission_fingerprint,
        nonce_factory=new_uuid7,
    )


def _resolve_storyboard_video_idempotency_key(
    *,
    run_id: str,
    step: WorkflowStep,
    keyframe_image_id: str,
    requested_key: str | None,
) -> tuple[str, str]:
    return storyboard_assembly.resolve_storyboard_video_idempotency_key(
        run_id=run_id,
        step=step,
        keyframe_image_id=keyframe_image_id,
        requested_key=requested_key,
        nonce_factory=new_uuid7,
    )


_storyboard_assembly_fingerprint = storyboard_assembly.storyboard_assembly_fingerprint
_storyboard_assembly_idempotency_key = (
    storyboard_assembly.storyboard_assembly_idempotency_key
)
_parse_assembly_datetime = storyboard_assembly.parse_assembly_datetime
_assembly_lease_expiry = storyboard_assembly.assembly_lease_expiry
_assembly_attempt_is_stale = storyboard_assembly.assembly_attempt_is_stale
_assembly_request_is_replay = storyboard_assembly.assembly_request_is_replay
_assembly_status_for_response = storyboard_assembly.assembly_status_for_response


def _shot_out(
    step: WorkflowStep,
    *,
    assets_by_id: dict[str, WorkflowStep],
    video_generations: dict[str, VideoGeneration],
    videos_by_generation: dict[str, Video],
) -> StoryboardShotOut:
    return storyboard_serialization.shot_out(
        step,
        assets_by_id=assets_by_id,
        video_generations=video_generations,
        videos_by_generation=videos_by_generation,
        video_out_fn=_video_out,
        shot_source_hash_fn=_shot_source_hash,
    )


async def _sync_storyboard_outputs(db: AsyncSession, run: WorkflowRun) -> None:
    await storyboard_output_sync.sync_storyboard_outputs(
        db,
        run,
        load_steps=_load_steps,
        recover_fn=_recover_storyboard_video_generations,
    )


_recover_storyboard_video_generations = (
    storyboard_output_sync.recover_storyboard_video_generations
)


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> StoryboardRunOut:
    return await storyboard_serialization.build_run_out(
        db,
        run,
        sync_outputs=_sync_storyboard_outputs,
        load_steps=_load_steps,
        video_out_fn=_video_out,
        shot_source_hash_fn=_shot_source_hash,
    )


async def _list_item_out(
    db: AsyncSession, run: WorkflowRun
) -> StoryboardRunListItemOut:
    return await storyboard_serialization.list_item_out(
        db,
        run,
        build_out=_build_run_out,
    )


_decode_cursor = storyboard_patching.decode_cursor
_encode_cursor = storyboard_patching.encode_cursor
_asset_prompt = storyboard_patching.asset_prompt
_shot_keyframe_prompt = storyboard_patching.shot_keyframe_prompt


async def _create_storyboard_image_task(
    *,
    db: AsyncSession,
    user: User,
    run: WorkflowRun,
    step: WorkflowStep,
    prompt: str,
    attachment_ids: list[str],
    purpose: Literal["asset", "keyframe"],
) -> StoryboardImageTask:
    conv = await _get_or_create_storyboard_conversation(db, user=user, run=run)
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": prompt,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_type": STORYBOARD_WORKFLOW_TYPE,
            "workflow_run_id": run.id,
            "workflow_step_key": step.step_key,
            "storyboard_purpose": purpose,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()
    md = _run_metadata(run)
    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=Intent.IMAGE_TO_IMAGE if attachment_ids else Intent.TEXT_TO_IMAGE,
        idempotency_key=f"storyboard:{run.id}:{step.id}:{purpose}:{new_uuid7()}",
        image_params=ImageParamsIn.model_validate(
            {
                "aspect_ratio": str(
                    md.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
                ),
                "count": 1,
                "quality": "2k",
                "render_quality": "medium",
            }
        ),
        chat_params=ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=prompt,
        user_email=getattr(user, "email", None),
        request_metadata={
            "source": "storyboard",
            "action_source": f"storyboard_{purpose}",
            "workflow_type": STORYBOARD_WORKFLOW_TYPE,
            "workflow_run_id": run.id,
            "workflow_step_key": step.step_key,
            "storyboard_purpose": purpose,
            "input_images": [
                {"image_id": image_id, "role": "reference"}
                for image_id in attachment_ids
            ],
            "primary_input_image_id": attachment_ids[0] if attachment_ids else None,
        },
    )
    if not result.generation_ids:
        raise _http("task_not_created", "image generation task was not created", 500)
    generation_id = result.generation_ids[0]
    return StoryboardImageTask(
        generation_id=generation_id,
        conversation_id=conv.id,
        user_message_id=user_msg.id,
        assistant_message_id=result.assistant_msg.id,
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )


async def _publish_storyboard_image_task(
    *,
    db: AsyncSession,
    user_id: str,
    task: StoryboardImageTask,
) -> None:
    await storyboard_tasks.publish_storyboard_image_task(
        db=db,
        user_id=user_id,
        task=task,
        enqueue_fn=_enqueue_storyboard_image_task,
        mark_published_fn=_mark_storyboard_image_tasks_published,
    )


async def _publish_storyboard_image_tasks(
    *,
    db: AsyncSession,
    user_id: str,
    tasks: list[StoryboardImageTask],
) -> None:
    # The service owns the bounded Semaphore(STORYBOARD_KEYFRAME_PARALLELISM).
    await storyboard_tasks.publish_storyboard_image_tasks(
        db=db,
        user_id=user_id,
        tasks=tasks,
        enqueue_fn=_enqueue_storyboard_image_task,
        mark_published_fn=_mark_storyboard_image_tasks_published,
    )


async def _enqueue_storyboard_image_task(
    *,
    user_id: str,
    task: StoryboardImageTask,
) -> bool:
    return await storyboard_tasks.enqueue_storyboard_image_task(
        user_id=user_id,
        task=task,
        redis_factory=get_redis,
        pool_factory=get_arq_pool,
        publish_message_fn=_publish_message_appended,
        publish_sse_fn=publish_sse_event,
    )


async def _mark_storyboard_image_tasks_published(
    db: AsyncSession,
    tasks: list[StoryboardImageTask],
) -> None:
    await storyboard_tasks.mark_storyboard_image_tasks_published(
        db,
        tasks,
        now_fn=_now,
    )


_rank_status = rank_status
_normalize_shot_indexes = normalize_shot_indexes


async def _validate_asset_ids(
    db: AsyncSession,
    run: WorkflowRun,
    asset_ids: list[str],
    *,
    require_approved: bool = False,
) -> list[str]:
    return await storyboard_patching.validate_asset_ids(
        db,
        run,
        asset_ids,
        require_approved=require_approved,
    )


def _shot_input_from_body(
    body: StoryboardShotCreateIn | StoryboardShotPatchIn,
    *,
    index: int | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return storyboard_patching.shot_input_from_body(
        body,
        index=index,
        existing=existing,
    )


def _shots_from_script(script: str) -> list[StoryboardShotCreateIn]:
    return storyboard_patching.shots_from_script(script)


@router.post("", response_model=StoryboardRunOut, dependencies=[Depends(verify_csrf)])
async def create_storyboard(
    body: StoryboardCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    title = body.title.strip()
    run = WorkflowRun(
        user_id=user.id,
        type=STORYBOARD_WORKFLOW_TYPE,
        status="draft",
        title=title,
        user_prompt=body.idea.strip(),
        product_image_ids=[],
        current_step="idea",
        quality_mode="premium",
        metadata_jsonb={
            **_default_storyboard_metadata(),
            "style": body.style.strip(),
            "aspect_ratio": body.aspect_ratio.strip()
            or STORYBOARD_DEFAULT_ASPECT_RATIO,
            "resolution": body.resolution.strip() or STORYBOARD_DEFAULT_RESOLUTION,
            "model": body.model.strip() or STORYBOARD_DEFAULT_MODEL,
            "generate_audio": body.generate_audio,
            "seed": body.seed,
        },
    )
    db.add(run)
    await db.flush()
    await _assembly_step(db, run)
    conv = await _get_or_create_storyboard_conversation(db, user=user, run=run)
    run.conversation_id = conv.id
    await db.commit()
    await db.refresh(run)
    return await _build_run_out(db, run)


@router.get("", response_model=StoryboardRunListOut)
async def list_storyboards(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=24, ge=1, le=60),
) -> StoryboardRunListOut:
    stmt = select(WorkflowRun).where(
        WorkflowRun.user_id == user.id,
        WorkflowRun.type == STORYBOARD_WORKFLOW_TYPE,
        WorkflowRun.deleted_at.is_(None),
    )
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        updated_at, row_id = decoded
        stmt = stmt.where(
            (WorkflowRun.updated_at < updated_at)
            | ((WorkflowRun.updated_at == updated_at) & (WorkflowRun.id < row_id))
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id)).limit(
                    limit + 1
                )
            )
        )
        .scalars()
        .all()
    )
    page = rows[:limit]
    items = [await _list_item_out(db, row) for row in page]
    next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
    await db.commit()
    return StoryboardRunListOut(items=items, next_cursor=next_cursor)


@router.get("/{run_id}", response_model=StoryboardRunOut)
async def get_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{run_id}", response_model=StoryboardRunOut, dependencies=[Depends(verify_csrf)]
)
async def patch_storyboard(
    run_id: str,
    body: StoryboardPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    patch = storyboard_patching.apply_storyboard_patch(
        run,
        body,
        now_fn=_now,
    )
    if patch:
        _merge_run_metadata(run, patch)
    if run.status == "draft" and (run.user_prompt or patch.get("script")):
        run.status = "in_progress"
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id, run.id, "storyboard.updated", {"run": out.model_dump(mode="json")}
    )
    return out


@router.delete("/{run_id}", dependencies=[Depends(verify_csrf)])
async def delete_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    deleted_at = _now()
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
    await _publish_storyboard_event(user.id, run.id, "storyboard.deleted", {})
    return {"ok": True}


@router.post(
    "/{run_id}/assets",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def add_asset(
    run_id: str,
    body: StoryboardAssetCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=_asset_step_key(new_uuid7()),
        status="waiting_input",
        input_json={
            "kind": body.kind,
            "name": body.name.strip(),
            "role": body.role.strip(),
            "description": body.description.strip(),
            "continuity": body.continuity.strip(),
            "revision": 1,
        },
        output_json={},
    )
    db.add(step)
    run.current_step = "assets"
    run.status = "in_progress"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{run_id}/assets/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_asset(
    run_id: str,
    step_id: str,
    body: StoryboardAssetPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    data = dict(step.input_json or {})
    changed = False
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        clean = value.strip() if isinstance(value, str) else value
        if data.get(key) != clean:
            data[key] = clean
            changed = True
    if changed:
        data["revision"] = int(data.get("revision") or 1) + 1
        step.input_json = data
        if step.status == "approved":
            step.status = (
                "ready" if (step.output_json or {}).get("image_id") else "waiting_input"
            )
            step.approved_at = None
            out_json = dict(step.output_json or {})
            out_json.pop("approved_at", None)
            step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/assets/{step_id}/generate",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_asset(
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    prompt = _asset_prompt(run, step, body.prompt)
    task = await _create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=[],
        purpose="asset",
    )
    step.status = "generating"
    step.task_ids = [task.generation_id]
    step.output_json = {
        **(step.output_json or {}),
        "prompt": prompt,
        "generation_id": task.generation_id,
        "image_id": None,
        "approved_at": None,
        "error_code": None,
        "error_message": None,
    }
    step.approved_at = None
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_image_task(db=db, user_id=user.id, task=task)
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.asset_generating",
        {"asset_id": step.id, "generation_id": task.generation_id},
    )
    return out


@router.post(
    "/{run_id}/assets/{step_id}/approve",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_asset(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    await _sync_storyboard_outputs(db, run)
    out_json = dict(step.output_json or {})
    if not out_json.get("image_id"):
        raise _http(
            "asset_image_required", "generate an asset image before approval", 422
        )
    now = _now()
    step.status = "approved"
    step.approved_at = now
    step.approved_by = user.id
    out_json["approved_at"] = now.isoformat()
    step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id, run.id, "storyboard.asset_ready", {"asset_id": step.id}
    )
    return out


@router.delete(
    "/{run_id}/assets/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def delete_asset(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="asset", lock=True)
    await db.delete(step)
    shots = [
        s for s in await _load_steps(db, run.id, lock=True) if _step_kind(s) == "shot"
    ]
    for shot in shots:
        data = dict(shot.input_json or {})
        asset_ids = [
            asset_id for asset_id in data.get("asset_ids", []) if asset_id != step_id
        ]
        if asset_ids != data.get("asset_ids", []):
            data["asset_ids"] = asset_ids
            shot.input_json = data
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/rebuild",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def rebuild_shots(
    run_id: str,
    body: StoryboardShotsRebuildIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    existing = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    if body.replace:
        for shot in existing:
            await db.delete(shot)
        await db.flush()
        existing = []
    md = _run_metadata(run)
    shots = (
        body.shots
        if body.shots is not None
        else _shots_from_script(str(md.get("script") or run.user_prompt))
    )
    offset = len(existing)
    for idx, item in enumerate(shots, start=1 + offset):
        data = _shot_input_from_body(item, index=idx)
        data["asset_ids"] = await _validate_asset_ids(
            db, run, data.get("asset_ids") or []
        )
        step = WorkflowStep(
            workflow_run_id=run.id,
            step_key=_shot_step_key(new_uuid7()),
            status="draft",
            input_json=data,
            output_json={},
        )
        db.add(step)
    run.current_step = "shots"
    run.status = "in_progress"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def add_shot(
    run_id: str,
    body: StoryboardShotCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    shots = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    data = _shot_input_from_body(body, index=len(shots) + 1)
    data["asset_ids"] = await _validate_asset_ids(db, run, data.get("asset_ids") or [])
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=_shot_step_key(new_uuid7()),
        status="draft",
        input_json=data,
        output_json={},
    )
    db.add(step)
    run.current_step = "shots"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{run_id}/shots/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_shot(
    run_id: str,
    step_id: str,
    body: StoryboardShotPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    data = _shot_input_from_body(body, existing=dict(step.input_json or {}))
    data["asset_ids"] = await _validate_asset_ids(db, run, data.get("asset_ids") or [])
    before_hash = _short_hash(step.input_json or {})
    after_hash = _short_hash(data)
    step.input_json = data
    if before_hash != after_hash and step.status in {
        "keyframe_ready",
        "keyframe_approved",
        "generating",
        "done",
    }:
        step.status = "approved"
        out_json = _clear_shot_video_output(dict(step.output_json or {}))
        out_json.pop("keyframe_approved_at", None)
        step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/{step_id}/approve",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_shot(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    if _rank_status(step.status) < _rank_status("approved"):
        step.status = "approved"
    step.approved_at = _now()
    step.approved_by = user.id
    run.current_step = "shots"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/{step_id}/keyframe",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_shot_keyframe(
    run_id: str,
    step_id: str,
    body: StoryboardGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    if _rank_status(step.status) < _rank_status("approved"):
        raise _http(
            "shot_not_approved", "approve the shot before keyframe generation", 422
        )
    asset_ids = await _validate_asset_ids(
        db,
        run,
        (step.input_json or {}).get("asset_ids") or [],
        require_approved=True,
    )
    assets = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "asset"]
    assets_by_id = {asset.id: asset for asset in assets}
    attachment_ids = _clean_string_list(
        [
            dict(assets_by_id[asset_id].output_json or {}).get("image_id")
            for asset_id in asset_ids
            if asset_id in assets_by_id
        ]
    )
    source_hash = _shot_source_hash(step, assets_by_id)
    prompt = _shot_keyframe_prompt(run, step, assets_by_id, body.prompt)
    task = await _create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=attachment_ids,
        purpose="keyframe",
    )
    inp = dict(step.input_json or {})
    inp["keyframe_prompt"] = prompt
    inp["keyframe_source_hash"] = source_hash
    step.input_json = inp
    step.status = "keyframe_generating"
    step.task_ids = [task.generation_id]
    step.output_json = {
        **_clear_shot_video_output(dict(step.output_json or {})),
        "keyframe_generation_id": task.generation_id,
        "keyframe_image_id": None,
        "keyframe_approved_at": None,
        "error_code": None,
        "error_message": None,
    }
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_image_task(db=db, user_id=user.id, task=task)
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.keyframe_generating",
        {"shot_id": step.id, "generation_id": task.generation_id},
    )
    return out


@router.post(
    "/{run_id}/shots/keyframes/generate-all",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_all_keyframes(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    await _sync_storyboard_outputs(db, run)
    steps = await _load_steps(db, run.id, lock=True)
    assets = [step for step in steps if _step_kind(step) == "asset"]
    shots = [step for step in steps if _step_kind(step) == "shot"]
    assets_by_id = {asset.id: asset for asset in assets}
    not_approved = [
        shot.id
        for shot in shots
        if _rank_status(shot.status) < _rank_status("approved")
        or shot.status in {"keyframe_generating", "generating"}
    ]
    if not_approved:
        raise _http(
            "shots_not_approved",
            "all shots must be approved and idle before batch keyframe generation",
            422,
            shot_ids=not_approved,
        )
    candidates = [
        shot
        for shot in shots
        if (
            not dict(shot.output_json or {}).get("keyframe_image_id")
            or dict(shot.input_json or {}).get("keyframe_source_hash")
            != _shot_source_hash(shot, assets_by_id)
        )
    ]
    planned: list[tuple[WorkflowStep, list[str], str, str]] = []
    for shot in candidates:
        asset_ids = await _validate_asset_ids(
            db,
            run,
            (shot.input_json or {}).get("asset_ids") or [],
            require_approved=True,
        )
        attachment_ids = _clean_string_list(
            [
                dict(assets_by_id[asset_id].output_json or {}).get("image_id")
                for asset_id in asset_ids
                if asset_id in assets_by_id
            ]
        )
        source_hash = _shot_source_hash(shot, assets_by_id)
        prompt = _shot_keyframe_prompt(run, shot, assets_by_id, None)
        planned.append((shot, attachment_ids, source_hash, prompt))
    tasks: list[tuple[WorkflowStep, StoryboardImageTask]] = []
    for shot, attachment_ids, source_hash, prompt in planned:
        task = await _create_storyboard_image_task(
            db=db,
            user=user,
            run=run,
            step=shot,
            prompt=prompt,
            attachment_ids=attachment_ids,
            purpose="keyframe",
        )
        inp = dict(shot.input_json or {})
        inp["keyframe_prompt"] = prompt
        inp["keyframe_source_hash"] = source_hash
        shot.input_json = inp
        shot.status = "keyframe_generating"
        shot.task_ids = [task.generation_id]
        shot.output_json = {
            **_clear_shot_video_output(dict(shot.output_json or {})),
            "keyframe_generation_id": task.generation_id,
            "keyframe_image_id": None,
            "keyframe_approved_at": None,
            "error_code": None,
            "error_message": None,
        }
        tasks.append((shot, task))
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_image_tasks(
        db=db,
        user_id=user.id,
        tasks=[task for _shot, task in tasks],
    )
    for shot, task in tasks:
        await _publish_storyboard_event(
            user.id,
            run.id,
            "storyboard.keyframe_generating",
            {"shot_id": shot.id, "generation_id": task.generation_id},
        )
    return out


@router.post(
    "/{run_id}/shots/{step_id}/keyframe/approve",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_keyframe(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    await _sync_storyboard_outputs(db, run)
    assets = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "asset"]
    source_hash = _shot_source_hash(step, {asset.id: asset for asset in assets})
    inp = dict(step.input_json or {})
    out_json = dict(step.output_json or {})
    if not out_json.get("keyframe_image_id"):
        raise _http("keyframe_required", "generate a keyframe before approval", 422)
    if inp.get("keyframe_source_hash") != source_hash:
        raise _http(
            "keyframe_stale", "keyframe is stale; regenerate before approval", 422
        )
    now = _now()
    step.status = "keyframe_approved"
    step.approved_at = now
    step.approved_by = user.id
    out_json["keyframe_approved_at"] = now.isoformat()
    step.output_json = out_json
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id, run.id, "storyboard.keyframe_ready", {"shot_id": step.id}
    )
    return out


@router.post(
    "/{run_id}/shots/{step_id}/submit",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def submit_shot(
    run_id: str,
    step_id: str,
    body: StoryboardSubmitShotIn,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    if getattr(user, "account_mode", "wallet") != "wallet":
        raise _http(
            "account_mode_forbidden", "video generation requires wallet mode", 403
        )
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    await _sync_storyboard_outputs(db, run)
    assets = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "asset"]
    source_hash = _shot_source_hash(step, {asset.id: asset for asset in assets})
    inp = dict(step.input_json or {})
    out_json = dict(step.output_json or {})
    keyframe_id = out_json.get("keyframe_image_id")
    if step.status != "keyframe_approved" or not keyframe_id:
        raise _http(
            "keyframe_not_approved", "approve the keyframe before video submission", 422
        )
    if inp.get("keyframe_source_hash") != source_hash:
        raise _http(
            "keyframe_stale", "keyframe is stale; regenerate before submission", 422
        )
    md = _run_metadata(run)
    prompt = (
        body.prompt
        or inp.get("keyframe_prompt")
        or inp.get("visual")
        or run.user_prompt
    )
    idempotency_key, submission_fingerprint = _resolve_storyboard_video_idempotency_key(
        run_id=run.id,
        step=step,
        keyframe_image_id=str(keyframe_id),
        requested_key=body.idempotency_key,
    )
    out_json["video_submission"] = {
        "fingerprint": submission_fingerprint,
        "idempotency_key": idempotency_key,
        "created_at": _now().isoformat(),
    }
    out_json.update({"error_code": None, "error_message": None})
    step.output_json = out_json
    step.status = "generating"
    run.current_step = "videos"
    await db.flush()
    video_body = VideoCreateIn.model_validate(
        {
            "action": "i2v",
            "model": str(md.get("model") or STORYBOARD_DEFAULT_MODEL),
            "prompt": str(prompt)[:MAX_PROMPT_CHARS],
            "input_image_id": str(keyframe_id),
            "duration_s": body.duration_s
            or int(inp.get("duration_s") or STORYBOARD_DEFAULT_DURATION_S),
            "resolution": str(md.get("resolution") or STORYBOARD_DEFAULT_RESOLUTION),
            "aspect_ratio": str(
                md.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
            ),
            "generate_audio": bool(md.get("generate_audio", True)),
            "seed": md.get("seed") if isinstance(md.get("seed"), int) else None,
            "watermark": False,
            "idempotency_key": idempotency_key,
        }
    )
    video_out = await _create_video_generation_record(
        db,
        video_body,
        user,
        request=request,
        workflow_metadata={
            "workflow_type": STORYBOARD_WORKFLOW_TYPE,
            "workflow_run_id": run.id,
            "workflow_step_key": step.step_key,
            "storyboard_purpose": "shot_video",
            "storyboard_keyframe_image_id": str(keyframe_id),
            "storyboard_video_submission_fingerprint": submission_fingerprint,
            "source": "storyboard",
            "action_source": "storyboard_video",
        },
    )
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    step.status = "generating"
    step.task_ids = [video_out.id]
    step.output_json = {
        **(step.output_json or {}),
        "video_generation_id": video_out.id,
        "error_code": None,
        "error_message": None,
    }
    run.current_step = "videos"
    out = await _build_run_out(db, run)
    await db.commit()
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.shot_submitted",
        {"shot_id": step.id, "video_generation_id": video_out.id},
    )
    return out


@router.post(
    "/{run_id}/shots/submit-all",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def submit_all_shots(
    run_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    candidates = [
        shot
        for shot in out.shots
        if shot.status == "keyframe_approved"
        and shot.keyframe_image_id
        and not shot.keyframe_stale
    ]
    for shot in candidates:
        await submit_shot(run_id, shot.id, StoryboardSubmitShotIn(), request, user, db)
    run = await _get_run(db, user_id=user.id, run_id=run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/assemble",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def assemble_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    await _sync_storyboard_outputs(db, run)
    steps = await _load_steps(db, run.id, lock=True)
    shots = [step for step in steps if _step_kind(step) == "shot"]
    _normalize_shot_indexes(shots)
    if not shots:
        raise _http("shots_required", "create shots before assembly", 422)
    not_done = [shot.id for shot in shots if shot.status != "done"]
    if not_done:
        raise _http(
            "shots_not_done",
            "all shots must be done before assembly",
            422,
            shot_ids=not_done,
        )
    ordered = sorted(shots, key=lambda s: int((s.input_json or {}).get("index") or 0))
    segment_ids = [
        str((shot.output_json or {}).get("video_generation_id"))
        for shot in ordered
        if (shot.output_json or {}).get("video_generation_id")
    ]
    if len(segment_ids) != len(ordered):
        raise _http(
            "segment_missing", "one or more shots are missing generated video ids", 422
        )
    assembly = await _assembly_step(db, run, lock=True)
    assembly_fingerprint = _storyboard_assembly_fingerprint(segment_ids)
    assembly_idempotency_key = _storyboard_assembly_idempotency_key(
        run_id=run.id,
        fingerprint=assembly_fingerprint,
    )
    current_output = dict(assembly.output_json or {})
    attempt_now = _now()
    if _assembly_request_is_replay(
        assembly,
        current_output,
        assembly_fingerprint,
        now=attempt_now,
    ):
        out = await _build_run_out(db, run)
        await db.commit()
        return out

    stale_recovery = current_output.get(
        "assembly_fingerprint"
    ) == assembly_fingerprint and _assembly_attempt_is_stale(
        assembly,
        current_output,
        now=attempt_now,
    )
    previous_attempt_token = current_output.get("assembly_attempt_token")
    if not isinstance(previous_attempt_token, str) or not previous_attempt_token:
        previous_attempt_token = None
    raw_recovery_count = current_output.get("assembly_recovery_count")
    recovery_count = (
        raw_recovery_count
        if isinstance(raw_recovery_count, int) and raw_recovery_count >= 0
        else 0
    )
    if stale_recovery:
        recovery_count += 1

    attempt_token = new_uuid7()
    if assembly.status != "compositing":
        assembly.status = "waiting"
    lease_expires_at = attempt_now + timedelta(
        seconds=STORYBOARD_ASSEMBLY_WAITING_LEASE_S
    )
    assembly.output_json = {
        **current_output,
        "segment_ids": segment_ids,
        "assembly_fingerprint": assembly_fingerprint,
        "assembly_idempotency_key": assembly_idempotency_key,
        "assembly_attempt_token": attempt_token,
        "assembly_enqueued_at": attempt_now.isoformat(),
        "assembly_claimed_at": None,
        "assembly_heartbeat_at": None,
        "assembly_lease_expires_at": lease_expires_at.isoformat(),
        "assembly_completed_at": None,
        "assembly_recovery_count": recovery_count,
        "assembly_recovery_reason": "lease_expired" if stale_recovery else None,
        "assembly_superseded_attempt_token": (
            previous_attempt_token if stale_recovery else None
        ),
        "video_id": None,
        "error_code": None,
        "error_message": None,
    }
    payload: dict[str, Any] = {
        "task_id": run.id,
        "run_id": run.id,
        "user_id": user.id,
        "kind": "storyboard_assembly",
        "assembly_fingerprint": assembly_fingerprint,
        "assembly_idempotency_key": assembly_idempotency_key,
        "assembly_attempt_token": attempt_token,
        "assembly_lease_expires_at": lease_expires_at.isoformat(),
        "assembly_recovered": stale_recovery,
    }
    outbox = OutboxEvent(kind="storyboard_assembly", payload=payload, published_at=None)
    db.add(outbox)
    await db.flush()
    outbox_id = str(outbox.id)
    payload["outbox_id"] = outbox_id
    outbox.payload = dict(payload)
    assembly.task_ids = [outbox.id]
    run.current_step = "assembly"
    out = await _build_run_out(db, run)
    await db.commit()
    try:
        pool = await get_arq_pool()
        await pool.enqueue_job(
            "run_storyboard_assembly",
            run.id,
            attempt_token,
            _job_id=arq_job_id("storyboard_assembly", run.id, outbox_id),
        )
    except Exception:
        logger.warning(
            "storyboard assembly enqueue failed run=%s", run.id, exc_info=True
        )
    await _publish_storyboard_event(
        user.id,
        run.id,
        "storyboard.assembling",
        {
            "segment_ids": segment_ids,
            "assembly_fingerprint": assembly_fingerprint,
            "assembly_attempt_token": attempt_token,
            "recovered": stale_recovery,
        },
    )
    return out


@router.delete(
    "/{run_id}/shots/{step_id}",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def delete_shot(
    run_id: str,
    step_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    step = await _get_step(db, run, step_id, kind="shot", lock=True)
    await db.delete(step)
    await db.flush()
    shots = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    _normalize_shot_indexes(shots)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{run_id}/shots/{step_id}/move",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def move_shot(
    run_id: str,
    step_id: str,
    body: StoryboardShotMoveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    run = await _get_run(db, user_id=user.id, run_id=run_id, lock=True)
    target = await _get_step(db, run, step_id, kind="shot", lock=True)
    shots = [s for s in await _load_steps(db, run.id) if _step_kind(s) == "shot"]
    ordered = sorted(
        shots,
        key=lambda s: (int((s.input_json or {}).get("index") or 0), s.created_at, s.id),
    )
    pos = next((idx for idx, shot in enumerate(ordered) if shot.id == target.id), -1)
    new_pos = pos + body.direction
    if pos < 0 or new_pos < 0 or new_pos >= len(ordered):
        out = await _build_run_out(db, run)
        await db.commit()
        return out
    ordered[pos], ordered[new_pos] = ordered[new_pos], ordered[pos]
    for index, shot in enumerate(ordered, start=1):
        data = dict(shot.input_json or {})
        data["index"] = index
        shot.input_json = data
    out = await _build_run_out(db, run)
    await db.commit()
    return out
