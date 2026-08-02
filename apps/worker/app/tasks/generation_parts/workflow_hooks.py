"""Post-generation recording hooks for model and poster workflows."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Protocol

from sqlalchemy import select

from lumen_core.models import (
    Image,
    PosterMaster,
    PosterRender,
    PosterStyleItem,
    WorkflowRun,
    WorkflowStep,
    new_uuid7,
)
from lumen_core.vision_tagging import AutoTagResult, PosterStyleAutoTagResult

from .active_user_fence import lock_active_generation_user


logger = logging.getLogger(__name__)


class ModelLibraryTagger(Protocol):
    async def __call__(
        self,
        image_record: Any,
    ) -> AutoTagResult: ...


class PosterStyleTagger(Protocol):
    async def __call__(
        self,
        image_record: Any,
    ) -> PosterStyleAutoTagResult: ...


@dataclass(frozen=True, slots=True)
class WorkflowHookServices:
    model_library_tagger: ModelLibraryTagger
    poster_style_tagger: PosterStyleTagger


@dataclass(frozen=True)
class PosterStyleInput:
    title: str
    category: str
    mood: str | None
    prompt_template: str | None
    prompt: str
    palette: list[str]
    recommended_aspects: list[str]
    style_tags: list[str]
    auto_tag: bool


def model_library_requested_count_from_step(step: Any) -> int:
    task_ids = [task_id for task_id in (step.task_ids or []) if task_id]
    if task_ids:
        return len(task_ids)

    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    try:
        count = int(input_json.get("count_per_gender") or input_json.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    genders = input_json.get("genders")
    gender_count = (
        len([gender for gender in genders if gender in {"female", "male"}])
        if isinstance(genders, list)
        else 1
    )
    return count * max(1, gender_count)


async def maybe_record_model_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Any,
    image_id: str,
    services: WorkflowHookServices,
) -> None:
    req = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if req.get("workflow_action") != "model_library_generate":
        return
    if req.get("workflow_step_key") != "model_library_generate":
        return
    run_id = req.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run_model = WorkflowRun
    step_model = WorkflowStep
    run = (
        await session.execute(
            select(run_model).where(
                run_model.id == run_id,
                run_model.user_id == user_id,
                run_model.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return
    step = (
        await session.execute(
            select(step_model)
            .where(
                step_model.workflow_run_id == run.id,
                step_model.step_key == "model_library_generate",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if step is None:
        return

    image_ids = list(step.image_ids or [])
    if image_id not in image_ids:
        image_ids.append(image_id)
    step.image_ids = list(dict.fromkeys(image_ids))

    requested = model_library_requested_count_from_step(step)

    output_json = dict(step.output_json or {})

    finished_count = len(step.image_ids or [])
    if finished_count >= requested and requested > 0 and step.status == "running":
        step.status = "succeeded"
        run.status = "completed"
        run.current_step = "model_library_generate"
    step.output_json = output_json


async def maybe_record_poster_style_library_generate_image(
    *,
    session: Any,
    user_id: str,
    generation: Any,
    image_id: str,
    services: WorkflowHookServices,
) -> None:
    run_id = _poster_style_run_id(generation)
    if run_id is None:
        return
    loaded = await _load_poster_style_run_step(
        session,
        user_id=user_id,
        run_id=run_id,
        services=services,
    )
    if loaded is None:
        return
    run, step = loaded
    _append_step_image(step, image_id)
    input_value = _poster_style_input(step)
    await _find_or_create_poster_style_item(
        session,
        user_id=user_id,
        image_id=image_id,
        run=run,
        input_value=input_value,
        services=services,
    )
    _complete_poster_style_step(run, step, input_value)


def _poster_style_run_id(generation: Any) -> str | None:
    request = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if request.get("workflow_action") != "poster_style_library_generate":
        return None
    if request.get("workflow_step_key") != "poster_style_library_generate":
        return None
    run_id = request.get("workflow_run_id")
    return run_id if isinstance(run_id, str) and run_id else None


async def _load_poster_style_run_step(
    session: Any,
    *,
    user_id: str,
    run_id: str,
    services: WorkflowHookServices,
) -> tuple[Any, Any] | None:
    run_model = WorkflowRun
    run = (
        await session.execute(
            select(run_model).where(
                run_model.id == run_id,
                run_model.user_id == user_id,
                run_model.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    step_model = WorkflowStep
    step = (
        await session.execute(
            select(step_model)
            .where(
                step_model.workflow_run_id == run.id,
                step_model.step_key == "poster_style_library_generate",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    return (run, step) if step is not None else None


def _append_step_image(step: Any, image_id: str) -> None:
    image_ids = list(step.image_ids or [])
    if image_id not in image_ids:
        image_ids.append(image_id)
    step.image_ids = list(dict.fromkeys(image_ids))


def _poster_style_input(step: Any) -> PosterStyleInput:
    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    category = str(input_json.get("category") or "user_favorites")
    mood_raw = input_json.get("mood")
    mood = (
        str(mood_raw)[:128] if isinstance(mood_raw, str) and mood_raw.strip() else None
    )
    prompt = str(input_json.get("prompt") or "")[:4000]
    return PosterStyleInput(
        title=str(input_json.get("title") or "未命名风格")[:255],
        category=category or "user_favorites",
        mood=mood,
        prompt_template=_poster_style_prompt_template(input_json, prompt),
        prompt=prompt,
        palette=_string_list(input_json.get("palette"), limit=8),
        recommended_aspects=_string_list(
            input_json.get("recommended_aspects"),
            limit=8,
        ),
        style_tags=_string_list(input_json.get("style_tags"), limit=8),
        auto_tag=bool(input_json.get("auto_tag", False)),
    )


def _poster_style_prompt_template(
    input_json: dict[str, Any],
    prompt: str,
) -> str | None:
    raw = input_json.get("prompt_template")
    if isinstance(raw, str) and raw.strip():
        return raw[:2000]
    return prompt[:2000] if prompt else None


def _string_list(value: Any, *, limit: int) -> list[str]:
    return [item for item in (value or []) if isinstance(item, str)][:limit]


async def _find_or_create_poster_style_item(
    session: Any,
    *,
    user_id: str,
    image_id: str,
    run: Any,
    input_value: PosterStyleInput,
    services: WorkflowHookServices,
) -> Any:
    item_model = PosterStyleItem
    existing = (
        await session.execute(
            select(item_model)
            .where(
                item_model.user_id == user_id,
                item_model.cover_image_id == image_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    item = item_model(
        id=f"user:{new_uuid7()}",
        user_id=user_id,
        source="generated",
        cover_image_id=image_id,
        sample_image_ids=[image_id],
        title=input_value.title,
        category=input_value.category,
        mood=input_value.mood,
        prompt_template=input_value.prompt_template,
        palette=list(input_value.palette),
        recommended_aspects=list(input_value.recommended_aspects)
        or ["1:1", "9:16", "16:9", "3:4"],
        style_tags=list(input_value.style_tags),
        library_folder=None,
        metadata_jsonb={
            "workflow_run_id": run.id,
            "prompt": input_value.prompt,
        },
    )
    session.add(item)
    await session.flush()
    return item


def _apply_poster_style_tag_result(
    target_item: PosterStyleItem,
    result: PosterStyleAutoTagResult,
) -> None:
    if result.category and target_item.category in (None, "", "user_favorites"):
        target_item.category = result.category
    if result.mood and not target_item.mood:
        target_item.mood = result.mood[:128]
    if result.style_tags:
        target_item.style_tags = list(
            dict.fromkeys([*target_item.style_tags, *result.style_tags])
        )[:8]
    if result.palette and not target_item.palette:
        target_item.palette = list(result.palette)[:8]
    target_item.auto_tagged_at = datetime.now(timezone.utc)
    target_item.auto_tag_notes = result.notes
    metadata = dict(target_item.metadata_jsonb or {})
    metadata["auto_tag_raw"] = {
        "category": result.category,
        "mood": result.mood,
        "style_tags": list(result.style_tags or []),
        "palette": list(result.palette or []),
        "notes": result.notes,
    }
    target_item.metadata_jsonb = metadata


def _workflow_request(generation: Any) -> dict[str, Any]:
    request = getattr(generation, "upstream_request", None)
    return dict(request) if isinstance(request, dict) else {}


async def _load_taggable_image(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    user_id: str,
    image_id: str,
) -> Any | None:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Image).where(
                    Image.id == image_id,
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return SimpleNamespace(
            id=str(row.id),
            storage_key=row.storage_key,
            mime=row.mime,
        )


async def _model_library_auto_tag_enabled(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    run_id: str,
    image_id: str,
) -> bool:
    async with session_factory() as session:
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_run_id == run_id,
                    WorkflowStep.step_key == "model_library_generate",
                )
            )
        ).scalar_one_or_none()
        input_json = step.input_json if step is not None else None
        return bool(
            step is not None
            and image_id in list(step.image_ids or [])
            and isinstance(input_json, dict)
            and input_json.get("auto_tag") is True
        )


async def _apply_model_library_tag_result(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    user_id: str,
    run_id: str,
    image_id: str,
    result: AutoTagResult,
) -> None:
    async with session_factory() as session:
        if not await lock_active_generation_user(session, user_id=user_id):
            return
        step = (
            await session.execute(
                select(WorkflowStep)
                .where(
                    WorkflowStep.workflow_run_id == run_id,
                    WorkflowStep.step_key == "model_library_generate",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if step is None or image_id not in list(step.image_ids or []):
            return
        output_json = dict(step.output_json or {})
        tagging_results = output_json.get("tagging_results")
        if not isinstance(tagging_results, dict):
            tagging_results = {}
        tagging_results[image_id] = {
            "style_tags": list(result.style_tags or []),
            "appearance_direction": result.appearance_direction,
            "age_segment": result.age_segment,
            "gender": result.gender,
            "notes": result.notes,
        }
        output_json["tagging_results"] = tagging_results
        step.output_json = output_json
        await session.commit()


async def _poster_style_auto_tag_enabled(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    run_id: str,
    image_id: str,
) -> bool:
    async with session_factory() as session:
        step = (
            await session.execute(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_run_id == run_id,
                    WorkflowStep.step_key == "poster_style_library_generate",
                )
            )
        ).scalar_one_or_none()
        input_json = step.input_json if step is not None else None
        return bool(
            step is not None
            and image_id in list(step.image_ids or [])
            and isinstance(input_json, dict)
            and input_json.get("auto_tag") is True
        )


async def _apply_poster_style_post_commit_tag(
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    *,
    user_id: str,
    image_id: str,
    result: PosterStyleAutoTagResult,
) -> None:
    async with session_factory() as session:
        if not await lock_active_generation_user(session, user_id=user_id):
            return
        target_item = (
            await session.execute(
                select(PosterStyleItem)
                .where(
                    PosterStyleItem.user_id == user_id,
                    PosterStyleItem.cover_image_id == image_id,
                )
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if target_item is None:
            return
        _apply_poster_style_tag_result(target_item, result)
        await session.commit()


async def maybe_auto_tag_generated_workflow_image(
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[Any]],
    user_id: str,
    generation: Any,
    image_id: str,
    services: WorkflowHookServices,
) -> None:
    request = _workflow_request(generation)
    run_id = request.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return
    action = request.get("workflow_action")
    if action == "model_library_generate":
        if not await _model_library_auto_tag_enabled(
            session_factory,
            run_id=run_id,
            image_id=image_id,
        ):
            return
        image_record = await _load_taggable_image(
            session_factory,
            user_id=user_id,
            image_id=image_id,
        )
        if image_record is None:
            return
        result = await services.model_library_tagger(image_record)
        await _apply_model_library_tag_result(
            session_factory,
            user_id=user_id,
            run_id=run_id,
            image_id=image_id,
            result=result,
        )
        return
    if action != "poster_style_library_generate":
        return
    if not await _poster_style_auto_tag_enabled(
        session_factory,
        run_id=run_id,
        image_id=image_id,
    ):
        return
    image_record = await _load_taggable_image(
        session_factory,
        user_id=user_id,
        image_id=image_id,
    )
    if image_record is None:
        return
    result = await services.poster_style_tagger(image_record)
    await _apply_poster_style_post_commit_tag(
        session_factory,
        user_id=user_id,
        image_id=image_id,
        result=result,
    )


def _complete_poster_style_step(
    run: Any,
    step: Any,
    input_value: PosterStyleInput,
) -> None:
    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    requested = int(input_json.get("count") or 0)
    if requested <= 0:
        requested = max(len(step.task_ids or []), len(step.image_ids or []))
    finished_count = len(step.image_ids or [])
    if finished_count >= requested and requested > 0 and step.status == "running":
        step.status = "succeeded"
        run.status = "completed"
        run.current_step = "poster_style_library_generate"


async def maybe_record_model_library_candidate_image(
    *,
    session: Any,
    user_id: str,
    parent_upstream_request: dict[str, Any],
    bonus_image_id: str,
    services: WorkflowHookServices,
) -> None:
    if parent_upstream_request.get("workflow_action") != "model_library_generate":
        return
    if parent_upstream_request.get("workflow_step_key") != "model_library_generate":
        return
    run_id = parent_upstream_request.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run_model = WorkflowRun
    step_model = WorkflowStep
    run = (
        await session.execute(
            select(run_model).where(
                run_model.id == run_id,
                run_model.user_id == user_id,
                run_model.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return
    step = (
        await session.execute(
            select(step_model)
            .where(
                step_model.workflow_run_id == run.id,
                step_model.step_key == "model_library_generate",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if step is None:
        return

    output_json = dict(step.output_json or {})
    bonus_ids = list(output_json.get("dual_race_bonus_image_ids") or [])
    if bonus_image_id not in bonus_ids:
        bonus_ids.append(bonus_image_id)
    output_json["dual_race_bonus_image_ids"] = bonus_ids
    step.output_json = output_json


async def maybe_record_poster_workflow_image(
    *,
    session: Any,
    user_id: str,
    generation: Any,
    image_id: str,
    services: WorkflowHookServices,
) -> None:
    req = (
        generation.upstream_request
        if isinstance(generation.upstream_request, dict)
        else {}
    )
    if req.get("workflow_type") != "poster_design":
        return
    action = req.get("workflow_action")
    if action not in {
        "poster_master",
        "poster_render",
        "poster_revise",
        "poster_inpaint",
    }:
        return
    run_id = req.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run_model = WorkflowRun
    run = (
        await session.execute(
            select(run_model).where(
                run_model.id == run_id,
                run_model.user_id == user_id,
                run_model.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return

    if action == "poster_master":
        master_id = req.get("workflow_master_id")
        if isinstance(master_id, str) and master_id:
            master = await session.get(PosterMaster, master_id)
            if master is not None and master.workflow_run_id == run.id:
                if not master.image_id:
                    master.image_id = image_id
                if master.status == "generating":
                    master.status = "ready"
    else:
        render_id = req.get("workflow_render_id")
        if isinstance(render_id, str) and render_id:
            render = await session.get(PosterRender, render_id)
            if render is not None and render.workflow_run_id == run.id:
                render.image_id = image_id
                if render.status in {"generating", "revising"}:
                    render.status = "ready"
