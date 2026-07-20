"""Storyboard query loading and response serialization."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Video, VideoGeneration, WorkflowRun, WorkflowStep
from lumen_core.schemas import VideoOut

from .assembly import assembly_status_for_response
from .common import (
    MAX_PROMPT_CHARS,
    STORYBOARD_DEFAULT_ASPECT_RATIO,
    STORYBOARD_DEFAULT_MODEL,
    STORYBOARD_DEFAULT_RESOLUTION,
    clean_string_list,
    clean_text,
    default_storyboard_metadata,
    image_display_url,
    image_url,
    iso_datetime,
    run_metadata,
    step_kind,
    video_poster_url,
    video_url,
)
from .contracts import (
    StoryboardAssemblyOut,
    StoryboardAssetOut,
    StoryboardRunListItemOut,
    StoryboardRunOut,
    StoryboardShotOut,
)


def asset_out(step: WorkflowStep) -> StoryboardAssetOut:
    input_data = dict(step.input_json or {})
    output = dict(step.output_json or {})
    image_id = (
        output.get("image_id") if isinstance(output.get("image_id"), str) else None
    )
    generation_id = (
        output.get("generation_id")
        if isinstance(output.get("generation_id"), str)
        else None
    )
    return StoryboardAssetOut(
        id=step.id,
        kind=clean_text(
            str(input_data.get("kind") or "character"),
            max_len=32,
        ),
        name=clean_text(
            str(input_data.get("name") or ""),
            max_len=120,
            default="未命名设定",
        ),
        role=clean_text(str(input_data.get("role") or ""), max_len=160),
        description=clean_text(
            str(input_data.get("description") or ""),
            max_len=2000,
        ),
        continuity=clean_text(
            str(input_data.get("continuity") or ""),
            max_len=2000,
        ),
        revision=int(input_data.get("revision") or 1),
        status=step.status,
        prompt=clean_text(
            str(output.get("prompt") or ""),
            max_len=MAX_PROMPT_CHARS,
        ),
        image_id=image_id,
        image_url=image_url(image_id),
        display_url=image_display_url(image_id),
        generation_id=generation_id,
        approved_at=(
            str(output.get("approved_at"))
            if isinstance(output.get("approved_at"), str)
            else iso_datetime(step.approved_at)
        ),
        error_code=(
            output.get("error_code")
            if isinstance(output.get("error_code"), str)
            else None
        ),
        error_message=(
            output.get("error_message")
            if isinstance(output.get("error_message"), str)
            else None
        ),
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


def shot_out(
    step: WorkflowStep,
    *,
    assets_by_id: dict[str, WorkflowStep],
    video_generations: dict[str, VideoGeneration],
    videos_by_generation: dict[str, Video],
    video_out_fn: Callable[[Video], VideoOut],
    shot_source_hash_fn: Callable[[WorkflowStep, dict[str, WorkflowStep]], str],
) -> StoryboardShotOut:
    input_data = dict(step.input_json or {})
    output = dict(step.output_json or {})
    keyframe_image_id = _string_value(output, "keyframe_image_id")
    video_generation_id = _string_value(output, "video_generation_id")
    current_source_hash = shot_source_hash_fn(step, assets_by_id)
    stored_source_hash = _string_value(input_data, "keyframe_source_hash")
    keyframe_stale = bool(
        keyframe_image_id and stored_source_hash != current_source_hash
    )
    video_generation = (
        video_generations.get(video_generation_id) if video_generation_id else None
    )
    video = (
        videos_by_generation.get(video_generation_id) if video_generation_id else None
    )
    return StoryboardShotOut(
        id=step.id,
        index=int(input_data.get("index") or 0),
        title=clean_text(
            str(input_data.get("title") or ""),
            max_len=160,
            default="未命名分镜",
        ),
        purpose=clean_text(str(input_data.get("purpose") or ""), max_len=1000),
        narration=clean_text(str(input_data.get("narration") or ""), max_len=2000),
        visual=clean_text(str(input_data.get("visual") or ""), max_len=2000),
        shot_type=clean_text(str(input_data.get("shot_type") or ""), max_len=80),
        camera_move=clean_text(str(input_data.get("camera_move") or ""), max_len=80),
        transition=clean_text(str(input_data.get("transition") or ""), max_len=80),
        reference_notes=clean_text(
            str(input_data.get("reference_notes") or ""),
            max_len=2000,
        ),
        duration_s=int(input_data.get("duration_s") or 5),
        asset_ids=clean_string_list(input_data.get("asset_ids") or []),
        keyframe_prompt=clean_text(
            str(input_data.get("keyframe_prompt") or ""),
            max_len=MAX_PROMPT_CHARS,
        ),
        keyframe_source_hash=stored_source_hash,
        current_source_hash=current_source_hash,
        keyframe_stale=keyframe_stale,
        status=step.status,
        keyframe_image_id=keyframe_image_id,
        keyframe_image_url=image_url(keyframe_image_id),
        keyframe_display_url=image_display_url(keyframe_image_id),
        keyframe_generation_id=_string_value(output, "keyframe_generation_id"),
        keyframe_approved_at=_string_value(output, "keyframe_approved_at"),
        video_generation_id=video_generation_id,
        video=video_out_fn(video) if video is not None else None,
        video_status=video_generation.status if video_generation is not None else None,
        video_progress_stage=(
            video_generation.progress_stage if video_generation is not None else None
        ),
        video_progress_pct=(
            video_generation.progress_pct if video_generation is not None else None
        ),
        error_code=_string_value(output, "error_code"),
        error_message=_string_value(output, "error_message"),
        created_at=step.created_at,
        updated_at=step.updated_at,
    )


def _string_value(values: dict[str, Any], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None


async def build_run_out(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    sync_outputs: Callable[[AsyncSession, WorkflowRun], Awaitable[None]],
    load_steps: Callable[..., Awaitable[list[WorkflowStep]]],
    video_out_fn: Callable[[Video], VideoOut],
    shot_source_hash_fn: Callable[[WorkflowStep, dict[str, WorkflowStep]], str],
) -> StoryboardRunOut:
    await sync_outputs(db, run)
    await db.flush()
    await db.refresh(run)
    steps = await load_steps(db, run.id)
    assets = [step for step in steps if step_kind(step) == "asset"]
    shots = [step for step in steps if step_kind(step) == "shot"]
    assembly = next((step for step in steps if step.step_key == "assembly"), None)
    assets_by_id = {step.id: step for step in assets}
    video_generation_ids = [
        output.get("video_generation_id")
        for output in (dict(step.output_json or {}) for step in shots)
        if isinstance(output.get("video_generation_id"), str)
    ]
    video_generations = await _load_video_generations(
        db,
        run.user_id,
        video_generation_ids,
    )
    videos_by_generation = await _load_videos_by_generation(db, video_generation_ids)
    shot_outs = sorted(
        [
            shot_out(
                step,
                assets_by_id=assets_by_id,
                video_generations=video_generations,
                videos_by_generation=videos_by_generation,
                video_out_fn=video_out_fn,
                shot_source_hash_fn=shot_source_hash_fn,
            )
            for step in shots
        ],
        key=lambda item: (item.index, item.created_at, item.id),
    )
    asset_outs = sorted(
        [asset_out(step) for step in assets],
        key=lambda item: (item.created_at, item.id),
    )
    assembly_out = await _assembly_out(db, run, assembly)
    thumbnail_url = _thumbnail_url(assembly_out, shot_outs, asset_outs)
    metadata = default_storyboard_metadata()
    metadata.update(run_metadata(run))
    return StoryboardRunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        title=run.title,
        idea=run.user_prompt,
        style=str(metadata.get("style") or ""),
        script=str(metadata.get("script") or ""),
        script_confirmed=bool(metadata.get("script_confirmed")),
        script_revision=int(metadata.get("script_revision") or 0),
        aspect_ratio=str(
            metadata.get("aspect_ratio") or STORYBOARD_DEFAULT_ASPECT_RATIO
        ),
        resolution=str(metadata.get("resolution") or STORYBOARD_DEFAULT_RESOLUTION),
        model=str(metadata.get("model") or STORYBOARD_DEFAULT_MODEL),
        generate_audio=bool(metadata.get("generate_audio", True)),
        seed=metadata.get("seed") if isinstance(metadata.get("seed"), int) else None,
        status=run.status,
        current_stage=run.current_step,
        assets=asset_outs,
        shots=shot_outs,
        assembly=assembly_out,
        thumbnail_url=thumbnail_url,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _load_video_generations(
    db: AsyncSession,
    user_id: str,
    generation_ids: list[str | None],
) -> dict[str, VideoGeneration]:
    ids = [value for value in generation_ids if isinstance(value, str)]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.id.in_(ids),
                VideoGeneration.user_id == user_id,
            )
        )
    ).scalars()
    return {row.id: row for row in rows.all()}


async def _load_videos_by_generation(
    db: AsyncSession,
    generation_ids: list[str | None],
) -> dict[str, Video]:
    ids = [value for value in generation_ids if isinstance(value, str)]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Video).where(
                Video.owner_generation_id.in_(ids),
                Video.deleted_at.is_(None),
            )
        )
    ).scalars()
    return {
        row.owner_generation_id: row
        for row in rows.all()
        if row.owner_generation_id is not None
    }


async def _assembly_out(
    db: AsyncSession,
    run: WorkflowRun,
    assembly: WorkflowStep | None,
) -> StoryboardAssemblyOut | None:
    if assembly is None:
        return None
    output = dict(assembly.output_json or {})
    video_id = _string_value(output, "video_id")
    video = None
    if video_id:
        video = (
            await db.execute(
                select(Video).where(
                    Video.id == video_id,
                    Video.user_id == run.user_id,
                    Video.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    segment_ids = [
        item for item in (output.get("segment_ids") or []) if isinstance(item, str)
    ]
    return StoryboardAssemblyOut(
        status=assembly_status_for_response(assembly, output),
        video_id=video_id,
        video_url=video_url(video_id),
        poster_url=video_poster_url(
            video_id,
            video.poster_storage_key if video else None,
        ),
        segment_count=len(segment_ids),
        segment_ids=segment_ids,
        error_code=_string_value(output, "error_code"),
        error_message=_string_value(output, "error_message"),
        updated_at=assembly.updated_at,
    )


def _thumbnail_url(
    assembly: StoryboardAssemblyOut | None,
    shots: list[StoryboardShotOut],
    assets: list[StoryboardAssetOut],
) -> str | None:
    if assembly and assembly.poster_url:
        return assembly.poster_url
    for shot in shots:
        if shot.keyframe_image_id:
            return shot.keyframe_display_url or shot.keyframe_image_url
    for asset in assets:
        if asset.image_id:
            return asset.display_url or asset.image_url
    return None


async def list_item_out(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    build_out: Callable[[AsyncSession, WorkflowRun], Awaitable[StoryboardRunOut]],
) -> StoryboardRunListItemOut:
    output = await build_out(db, run)
    return StoryboardRunListItemOut(
        id=output.id,
        title=output.title,
        idea=output.idea,
        status=output.status,
        current_stage=output.current_stage,
        asset_count=len(output.assets),
        approved_asset_count=sum(
            1 for item in output.assets if item.status == "approved"
        ),
        shot_count=len(output.shots),
        done_shot_count=sum(1 for item in output.shots if item.status == "done"),
        thumbnail_url=output.thumbnail_url,
        created_at=output.created_at,
        updated_at=output.updated_at,
    )
