"""Post-generation recording hooks for model and poster workflows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


AsyncTagger = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class WorkflowHookDependencies:
    select: Callable[..., Any]
    workflow_run_model: Any
    workflow_step_model: Any
    poster_style_item_model: Any
    poster_master_model: Any
    poster_render_model: Any
    new_uuid7: Callable[[], Any]
    logger: logging.Logger
    utcnow: Callable[[], datetime]
    load_model_library_tagger: Callable[[], AsyncTagger]
    load_poster_style_tagger: Callable[[], AsyncTagger]
    model_library_requested_count: Callable[[Any], int]


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
    deps: WorkflowHookDependencies,
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

    run_model = deps.workflow_run_model
    step_model = deps.workflow_step_model
    run = (
        await session.execute(
            deps.select(run_model).where(
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
            deps.select(step_model)
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

    input_json = step.input_json if isinstance(step.input_json, dict) else {}
    auto_tag = bool(input_json.get("auto_tag", False))
    requested = deps.model_library_requested_count(step)

    output_json = dict(step.output_json or {})
    if auto_tag:
        try:
            result = await deps.load_model_library_tagger()(
                session,
                image_id=image_id,
                user_id=user_id,
            )
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
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            deps.logger.info(
                "model_library_generate tagging skipped run=%s image=%s err=%s",
                run.id,
                image_id,
                exc,
            )

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
    deps: WorkflowHookDependencies,
) -> None:
    run_id = _poster_style_run_id(generation)
    if run_id is None:
        return
    loaded = await _load_poster_style_run_step(
        session,
        user_id=user_id,
        run_id=run_id,
        deps=deps,
    )
    if loaded is None:
        return
    run, step = loaded
    _append_step_image(step, image_id)
    input_value = _poster_style_input(step)
    target_item = await _find_or_create_poster_style_item(
        session,
        user_id=user_id,
        image_id=image_id,
        run=run,
        input_value=input_value,
        deps=deps,
    )
    if input_value.auto_tag:
        await _auto_tag_poster_style_item(
            session,
            user_id=user_id,
            image_id=image_id,
            run=run,
            target_item=target_item,
            deps=deps,
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
    deps: WorkflowHookDependencies,
) -> tuple[Any, Any] | None:
    run_model = deps.workflow_run_model
    run = (
        await session.execute(
            deps.select(run_model).where(
                run_model.id == run_id,
                run_model.user_id == user_id,
                run_model.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return None
    step_model = deps.workflow_step_model
    step = (
        await session.execute(
            deps.select(step_model)
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
    deps: WorkflowHookDependencies,
) -> Any:
    item_model = deps.poster_style_item_model
    existing = (
        await session.execute(
            deps.select(item_model)
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
        id=f"user:{deps.new_uuid7()}",
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


async def _auto_tag_poster_style_item(
    session: Any,
    *,
    user_id: str,
    image_id: str,
    run: Any,
    target_item: Any,
    deps: WorkflowHookDependencies,
) -> None:
    try:
        result = await deps.load_poster_style_tagger()(
            session,
            image_id=image_id,
            user_id=user_id,
        )
        _apply_poster_style_tag_result(target_item, result, deps)
    except (TimeoutError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        deps.logger.info(
            "poster_style_library_generate tagging skipped run=%s image=%s err=%s",
            run.id,
            image_id,
            exc,
        )


def _apply_poster_style_tag_result(
    target_item: Any,
    result: Any,
    deps: WorkflowHookDependencies,
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
    target_item.auto_tagged_at = deps.utcnow()
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
    deps: WorkflowHookDependencies,
) -> None:
    if parent_upstream_request.get("workflow_action") != "model_library_generate":
        return
    if parent_upstream_request.get("workflow_step_key") != "model_library_generate":
        return
    run_id = parent_upstream_request.get("workflow_run_id")
    if not isinstance(run_id, str) or not run_id:
        return

    run_model = deps.workflow_run_model
    step_model = deps.workflow_step_model
    run = (
        await session.execute(
            deps.select(run_model).where(
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
            deps.select(step_model)
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
    deps: WorkflowHookDependencies,
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

    run_model = deps.workflow_run_model
    run = (
        await session.execute(
            deps.select(run_model).where(
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
            master = await session.get(deps.poster_master_model, master_id)
            if master is not None and master.workflow_run_id == run.id:
                if not master.image_id:
                    master.image_id = image_id
                if master.status == "generating":
                    master.status = "ready"
    else:
        render_id = req.get("workflow_render_id")
        if isinstance(render_id, str) and render_id:
            render = await session.get(deps.poster_render_model, render_id)
            if render is not None and render.workflow_run_id == run.id:
                render.image_id = image_id
                if render.status in {"generating", "revising"}:
                    render.status = "ready"
