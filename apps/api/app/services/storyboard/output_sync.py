"""Reconcile storyboard steps with completed image and video generations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import GenerationStatus, VideoGenerationStatus
from lumen_core.models import (
    Generation,
    Image,
    Video,
    VideoGeneration,
    WorkflowRun,
    WorkflowStep,
)

from .assembly import storyboard_video_submission_fingerprint
from .common import (
    STORYBOARD_WORKFLOW_TYPE,
    clean_string_list,
    rank_status,
    step_kind,
)


async def recover_storyboard_video_generations(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    steps: list[WorkflowStep],
) -> dict[str, VideoGeneration]:
    expected_fingerprints: dict[str, str] = {}
    for step in steps:
        if step_kind(step) != "shot":
            continue
        output = dict(step.output_json or {})
        if isinstance(output.get("video_generation_id"), str):
            continue
        keyframe_image_id = output.get("keyframe_image_id")
        if not isinstance(keyframe_image_id, str) or not keyframe_image_id:
            continue
        expected_fingerprints[step.step_key] = storyboard_video_submission_fingerprint(
            step=step,
            keyframe_image_id=keyframe_image_id,
        )
    if not expected_fingerprints:
        return {}
    rows = (
        await db.execute(
            select(VideoGeneration)
            .where(
                VideoGeneration.user_id == run.user_id,
                VideoGeneration.upstream_request["workflow_type"].as_string()
                == STORYBOARD_WORKFLOW_TYPE,
                VideoGeneration.upstream_request["workflow_run_id"].as_string()
                == run.id,
            )
            .order_by(desc(VideoGeneration.created_at), desc(VideoGeneration.id))
        )
    ).scalars()
    recovered: dict[str, VideoGeneration] = {}
    for row in rows.all():
        request = row.upstream_request if isinstance(row.upstream_request, dict) else {}
        step_key = request.get("workflow_step_key")
        expected_fingerprint = (
            expected_fingerprints.get(step_key) if isinstance(step_key, str) else None
        )
        if (
            isinstance(step_key, str)
            and expected_fingerprint is not None
            and request.get("storyboard_video_submission_fingerprint")
            == expected_fingerprint
            and step_key not in recovered
        ):
            recovered[step_key] = row
    return recovered


async def sync_storyboard_outputs(
    db: AsyncSession,
    run: WorkflowRun,
    *,
    load_steps: Callable[..., Awaitable[list[WorkflowStep]]],
    recover_fn: Callable[..., Awaitable[dict[str, VideoGeneration]]] | None = None,
) -> None:
    recover = recover_fn or recover_storyboard_video_generations
    steps = await load_steps(db, run.id, lock=True)
    generation_ids = {
        task_id
        for step in steps
        for task_id in (step.task_ids or [])
        if isinstance(task_id, str) and task_id
    }
    video_generation_ids = {
        output.get("video_generation_id")
        for output in (dict(step.output_json or {}) for step in steps)
        if isinstance(output.get("video_generation_id"), str)
    }
    recovered = await recover(
        db,
        run=run,
        steps=steps,
    )
    changed = _attach_recovered_video_generations(
        steps,
        recovered,
        video_generation_ids,
    )
    generations = await _load_generations(db, run.user_id, generation_ids)
    images_by_generation = await _load_images_by_generation(
        db,
        run.user_id,
        generation_ids,
    )
    video_generations = await _load_video_generations(
        db,
        run.user_id,
        video_generation_ids,
    )
    videos_by_generation = await _load_videos_by_generation(
        db,
        video_generation_ids,
    )
    changed = (
        _reconcile_steps(
            steps,
            generations=generations,
            images_by_generation=images_by_generation,
            video_generations=video_generations,
            videos_by_generation=videos_by_generation,
        )
        or changed
    )
    if changed:
        await db.flush()


def _attach_recovered_video_generations(
    steps: list[WorkflowStep],
    recovered: dict[str, VideoGeneration],
    video_generation_ids: set[str],
) -> bool:
    changed = False
    for step in steps:
        if step_kind(step) != "shot":
            continue
        output = dict(step.output_json or {})
        if isinstance(output.get("video_generation_id"), str):
            continue
        generation = recovered.get(step.step_key)
        if generation is None:
            continue
        output["video_generation_id"] = generation.id
        step.output_json = output
        step.task_ids = clean_string_list([*(step.task_ids or []), generation.id])
        if (
            rank_status("keyframe_approved")
            <= rank_status(step.status)
            < rank_status("generating")
        ):
            step.status = "generating"
        video_generation_ids.add(generation.id)
        changed = True
    return changed


async def _load_generations(
    db: AsyncSession,
    user_id: str,
    generation_ids: set[str],
) -> dict[str, Generation]:
    if not generation_ids:
        return {}
    rows = (
        await db.execute(
            select(Generation).where(
                Generation.id.in_(generation_ids),
                Generation.user_id == user_id,
            )
        )
    ).scalars()
    return {row.id: row for row in rows.all()}


async def _load_images_by_generation(
    db: AsyncSession,
    user_id: str,
    generation_ids: set[str],
) -> dict[str, Image]:
    if not generation_ids:
        return {}
    rows = (
        await db.execute(
            select(Image).where(
                Image.owner_generation_id.in_(generation_ids),
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars()
    images_by_generation: dict[str, Image] = {}
    for image in rows.all():
        if (
            image.owner_generation_id
            and image.owner_generation_id not in images_by_generation
        ):
            images_by_generation[image.owner_generation_id] = image
    return images_by_generation


async def _load_video_generations(
    db: AsyncSession,
    user_id: str,
    video_generation_ids: set[str],
) -> dict[str, VideoGeneration]:
    if not video_generation_ids:
        return {}
    rows = (
        await db.execute(
            select(VideoGeneration).where(
                VideoGeneration.id.in_(video_generation_ids),
                VideoGeneration.user_id == user_id,
            )
        )
    ).scalars()
    return {row.id: row for row in rows.all()}


async def _load_videos_by_generation(
    db: AsyncSession,
    video_generation_ids: set[str],
) -> dict[str, Video]:
    if not video_generation_ids:
        return {}
    rows = (
        await db.execute(
            select(Video).where(
                Video.owner_generation_id.in_(video_generation_ids),
                Video.deleted_at.is_(None),
            )
        )
    ).scalars()
    return {
        row.owner_generation_id: row
        for row in rows.all()
        if row.owner_generation_id is not None
    }


def _reconcile_steps(
    steps: list[WorkflowStep],
    *,
    generations: dict[str, Generation],
    images_by_generation: dict[str, Image],
    video_generations: dict[str, VideoGeneration],
    videos_by_generation: dict[str, Video],
) -> bool:
    changed = False
    for step in steps:
        kind = step_kind(step)
        output = dict(step.output_json or {})
        if kind == "asset":
            changed = (
                _reconcile_asset(
                    step,
                    output,
                    generations=generations,
                    images_by_generation=images_by_generation,
                )
                or changed
            )
        elif kind == "shot":
            changed = (
                _reconcile_shot(
                    step,
                    output,
                    generations=generations,
                    images_by_generation=images_by_generation,
                    video_generations=video_generations,
                    videos_by_generation=videos_by_generation,
                )
                or changed
            )
    return changed


def _reconcile_asset(
    step: WorkflowStep,
    output: dict[str, Any],
    *,
    generations: dict[str, Generation],
    images_by_generation: dict[str, Image],
) -> bool:
    generation_id = output.get("generation_id")
    generation = (
        generations.get(generation_id) if isinstance(generation_id, str) else None
    )
    if generation is None:
        return False
    image = images_by_generation.get(generation.id)
    if generation.status == GenerationStatus.SUCCEEDED.value and image is not None:
        if output.get("image_id") == image.id and step.status != "generating":
            return False
        output.update({"image_id": image.id, "error_code": None, "error_message": None})
        step.output_json = output
        step.image_ids = [image.id]
        if step.status == "generating":
            step.status = "ready"
        return True
    if (
        generation.status
        in {GenerationStatus.FAILED.value, GenerationStatus.CANCELED.value}
        and step.status == "generating"
    ):
        output.update(
            {
                "error_code": generation.error_code or generation.status,
                "error_message": generation.error_message or "asset generation failed",
            }
        )
        step.output_json = output
        step.status = "waiting_input"
        return True
    return False


def _reconcile_shot(
    step: WorkflowStep,
    output: dict[str, Any],
    *,
    generations: dict[str, Generation],
    images_by_generation: dict[str, Image],
    video_generations: dict[str, VideoGeneration],
    videos_by_generation: dict[str, Video],
) -> bool:
    changed = _reconcile_keyframe(
        step,
        output,
        generations=generations,
        images_by_generation=images_by_generation,
    )
    video_generation_id = output.get("video_generation_id")
    video_generation = (
        video_generations.get(video_generation_id)
        if isinstance(video_generation_id, str)
        else None
    )
    if video_generation is None:
        return changed
    video = videos_by_generation.get(video_generation.id)
    if (
        video_generation.status == VideoGenerationStatus.SUCCEEDED.value
        and video is not None
    ):
        if step.status == "done":
            return changed
        step.status = "done"
        output.pop("video_submission", None)
        output.update({"error_code": None, "error_message": None})
        step.output_json = output
        return True
    if (
        video_generation.status
        in {
            VideoGenerationStatus.FAILED.value,
            VideoGenerationStatus.CANCELED.value,
            VideoGenerationStatus.EXPIRED.value,
        }
        and step.status == "generating"
    ):
        output.update(
            {
                "error_code": video_generation.error_code or video_generation.status,
                "error_message": video_generation.error_message
                or "video generation failed",
            }
        )
        output.pop("video_submission", None)
        step.output_json = output
        step.status = "keyframe_approved"
        return True
    return changed


def _reconcile_keyframe(
    step: WorkflowStep,
    output: dict[str, Any],
    *,
    generations: dict[str, Generation],
    images_by_generation: dict[str, Image],
) -> bool:
    keyframe_generation_id = output.get("keyframe_generation_id")
    generation = (
        generations.get(keyframe_generation_id)
        if isinstance(keyframe_generation_id, str)
        else None
    )
    if generation is None:
        return False
    image = images_by_generation.get(generation.id)
    if generation.status == GenerationStatus.SUCCEEDED.value and image is not None:
        if output.get("keyframe_image_id") == image.id:
            return False
        output.update(
            {
                "keyframe_image_id": image.id,
                "error_code": None,
                "error_message": None,
            }
        )
        step.output_json = output
        step.image_ids = [image.id]
        if step.status == "keyframe_generating":
            step.status = "keyframe_ready"
        return True
    if (
        generation.status
        in {GenerationStatus.FAILED.value, GenerationStatus.CANCELED.value}
        and step.status == "keyframe_generating"
    ):
        output.update(
            {
                "error_code": generation.error_code or generation.status,
                "error_message": generation.error_message
                or "keyframe generation failed",
            }
        )
        step.output_json = output
        step.status = "approved"
        return True
    return False
