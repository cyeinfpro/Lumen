"""Storyboard HTTP transport and compatibility composition root."""

from __future__ import annotations

import sys
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import (
    User,
    Video,
    VideoGeneration,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)

from ..arq_pool import get_arq_pool
from ..db import get_db
from ..deps import CurrentUser, durable_session_id, verify_csrf
from ..redis_client import get_redis
from ..services.storyboard import assembly as storyboard_assembly
from ..services.storyboard import common as storyboard_common
from ..services.storyboard import idempotency as storyboard_idempotency
from ..services.storyboard import output_sync as storyboard_output_sync
from ..services.storyboard import patching as storyboard_patching
from ..services.storyboard import repository as storyboard_repository
from ..services.storyboard import serialization as storyboard_serialization
from ..services.storyboard import tasks as storyboard_tasks
from ..services.storyboard.common import (
    asset_step_key,
    clean_string_list,
    clear_shot_video_output,
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
from ..sse_publish import publish_sse_event
from .messages import publish_message_appended
from .storyboard_parts import (
    assembly,
    commands,
    image_submission,
    publish,
    queries,
    video_submission,
)
from .storyboard_parts.runtime import StoryboardRuntimeAdapter
from .videos import video_out


router = APIRouter(prefix="/storyboards", tags=["storyboards"])

STORYBOARD_CHANNEL_PREFIX = "storyboard:"
STORYBOARD_ASSET_KINDS = frozenset({"character", "scene", "prop"})
STORYBOARD_DEFAULT_DURATION_S = storyboard_common.STORYBOARD_DEFAULT_DURATION_S
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
_rank_status = rank_status
_normalize_shot_indexes = normalize_shot_indexes
_asset_out = storyboard_serialization.asset_out
_shot_source_hash = shot_source_hash
_decode_cursor = storyboard_patching.decode_cursor
_encode_cursor = storyboard_patching.encode_cursor
_asset_prompt = storyboard_patching.asset_prompt
_shot_keyframe_prompt = storyboard_patching.shot_keyframe_prompt
_recover_storyboard_video_generations = (
    storyboard_output_sync.recover_storyboard_video_generations
)
_storyboard_video_submission_fingerprint = (
    storyboard_assembly.storyboard_video_submission_fingerprint
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
_canonical_storyboard_request_fingerprint = (
    storyboard_idempotency.canonical_request_fingerprint
)
_storyboard_child_task_idempotency_key = (
    storyboard_idempotency.child_task_idempotency_key
)
_resolve_storyboard_client_idempotency_key = (
    storyboard_idempotency.resolve_client_idempotency_key
)
_get_owned_conversation = storyboard_repository.get_owned_conversation
_get_or_create_storyboard_conversation = (
    storyboard_repository.get_or_create_storyboard_conversation
)
_get_run = storyboard_repository.get_run
_load_steps = storyboard_repository.load_steps
_get_step = storyboard_repository.get_step
_assembly_step = storyboard_repository.assembly_step
_validate_asset_ids = storyboard_patching.validate_asset_ids
_shot_input_from_body = storyboard_patching.shot_input_from_body
_shots_from_script = storyboard_patching.shots_from_script


async def _publish_storyboard_event(
    user_id: str,
    run_id: str,
    event_name: str,
    data: dict[str, Any],
) -> None:
    await publish.publish_storyboard_event(user_id, run_id, event_name, data)


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
        video_out_fn=video_out,
        shot_source_hash_fn=_shot_source_hash,
    )


async def _sync_storyboard_outputs(db: AsyncSession, run: WorkflowRun) -> None:
    await storyboard_output_sync.sync_storyboard_outputs(
        db,
        run,
        load_steps=_load_steps,
        recover_fn=_recover_storyboard_video_generations,
    )


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> StoryboardRunOut:
    return await storyboard_serialization.build_run_out(
        db,
        run,
        sync_outputs=_sync_storyboard_outputs,
        load_steps=_load_steps,
        video_out_fn=video_out,
        shot_source_hash_fn=_shot_source_hash,
    )


async def _list_item_out(
    db: AsyncSession,
    run: WorkflowRun,
) -> StoryboardRunListItemOut:
    return await storyboard_serialization.list_item_out(
        db,
        run,
        build_out=_build_run_out,
    )


async def _create_storyboard_image_task(
    *,
    db: AsyncSession,
    user: User,
    run: WorkflowRun,
    step: WorkflowStep,
    prompt: str,
    attachment_ids: list[str],
    purpose: Literal["asset", "keyframe"],
    task_idempotency_key: str,
    idempotency_metadata: dict[str, str],
) -> StoryboardImageTask:
    return await image_submission.create_storyboard_image_task(
        db=db,
        user=user,
        run=run,
        step=step,
        prompt=prompt,
        attachment_ids=attachment_ids,
        purpose=purpose,
        task_idempotency_key=task_idempotency_key,
        idempotency_metadata=idempotency_metadata,
        runtime=_runtime,
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
        publish_message_fn=publish_message_appended,
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
    await storyboard_tasks.publish_storyboard_image_tasks(
        db=db,
        user_id=user_id,
        tasks=tasks,
        enqueue_fn=_enqueue_storyboard_image_task,
        mark_published_fn=_mark_storyboard_image_tasks_published,
    )


_runtime = StoryboardRuntimeAdapter(sys.modules[__name__])


@router.post("", response_model=StoryboardRunOut, dependencies=[Depends(verify_csrf)])
async def create_storyboard(
    body: StoryboardCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    return await commands.create_storyboard(
        db=db, user=user, body=body, runtime=_runtime
    )


@router.get("", response_model=StoryboardRunListOut)
async def list_storyboards(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=24, ge=1, le=60),
) -> StoryboardRunListOut:
    return await queries.list_storyboards(
        db=db,
        user_id=user.id,
        cursor=cursor,
        limit=limit,
        runtime=_runtime,
    )


@router.get("/{run_id}", response_model=StoryboardRunOut)
async def get_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    return await queries.get_storyboard(
        db=db,
        user_id=user.id,
        run_id=run_id,
        runtime=_runtime,
    )


@router.patch(
    "/{run_id}", response_model=StoryboardRunOut, dependencies=[Depends(verify_csrf)]
)
async def patch_storyboard(
    run_id: str,
    body: StoryboardPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StoryboardRunOut:
    return await commands.patch_storyboard(
        db=db,
        user_id=user.id,
        run_id=run_id,
        body=body,
        runtime=_runtime,
    )


@router.delete("/{run_id}", dependencies=[Depends(verify_csrf)])
async def delete_storyboard(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    return await commands.delete_storyboard(
        db=db,
        user_id=user.id,
        run_id=run_id,
        runtime=_runtime,
    )


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
    return await commands.add_asset(
        db=db,
        user_id=user.id,
        run_id=run_id,
        body=body,
        runtime=_runtime,
    )


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
    return await commands.patch_asset(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        body=body,
        runtime=_runtime,
    )


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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
    request: Request = None,
) -> StoryboardRunOut:
    return await image_submission.generate_asset(
        db=db,
        user=user,
        run_id=run_id,
        step_id=step_id,
        body=body,
        idempotency_key=idempotency_key,
        runtime=_runtime,
        session_id=durable_session_id(request),
    )


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
    return await commands.approve_asset(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        runtime=_runtime,
    )


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
    return await commands.delete_asset(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        runtime=_runtime,
    )


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
    return await commands.rebuild_shots(
        db=db,
        user_id=user.id,
        run_id=run_id,
        body=body,
        runtime=_runtime,
    )


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
    return await commands.add_shot(
        db=db,
        user_id=user.id,
        run_id=run_id,
        body=body,
        runtime=_runtime,
    )


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
    return await commands.patch_shot(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        body=body,
        runtime=_runtime,
    )


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
    return await commands.approve_shot(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        runtime=_runtime,
    )


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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
    request: Request = None,
) -> StoryboardRunOut:
    return await image_submission.generate_shot_keyframe(
        db=db,
        user=user,
        run_id=run_id,
        step_id=step_id,
        body=body,
        idempotency_key=idempotency_key,
        runtime=_runtime,
        session_id=durable_session_id(request),
    )


@router.post(
    "/{run_id}/shots/keyframes/generate-all",
    response_model=StoryboardRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_all_keyframes(
    run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
    request: Request = None,
) -> StoryboardRunOut:
    return await image_submission.generate_all_keyframes(
        db=db,
        user=user,
        run_id=run_id,
        idempotency_key=idempotency_key,
        runtime=_runtime,
        session_id=durable_session_id(request),
    )


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
    return await commands.approve_keyframe(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        runtime=_runtime,
    )


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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> StoryboardRunOut:
    return await video_submission.submit_shot(
        db=db,
        user=user,
        run_id=run_id,
        step_id=step_id,
        body=body,
        request=request,
        idempotency_key=idempotency_key,
        runtime=_runtime,
    )


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
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key"),
    ] = None,
) -> StoryboardRunOut:
    return await video_submission.submit_all_shots(
        db=db,
        user=user,
        run_id=run_id,
        request=request,
        idempotency_key=idempotency_key,
        runtime=_runtime,
    )


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
    return await assembly.assemble_storyboard(
        db=db,
        user_id=user.id,
        run_id=run_id,
        runtime=_runtime,
    )


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
    return await commands.delete_shot(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        runtime=_runtime,
    )


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
    return await commands.move_shot(
        db=db,
        user_id=user.id,
        run_id=run_id,
        step_id=step_id,
        body=body,
        runtime=_runtime,
    )
