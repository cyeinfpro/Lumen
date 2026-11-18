"""Showcase scene planning, prompt review, and preflight orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .apparel_scene_planner import (
    build_garment_lock as _build_garment_lock,
)
from .showcase_preflight_steps import (
    ShowcasePreflightProgressHook as _ShowcasePreflightProgressHook,
    ShowcasePreflightRequest,
    compose_prompt_records,
    notify_preflight_progress,
    prepare_scene_planning,
    resolve_preflight_provider_order,
    review_prompt_records,
)
from ..ports.runtime_state import ProviderRoundRobinStatePort


@dataclass(frozen=True, slots=True)
class ShowcasePreflightCommand:
    db: AsyncSession
    request: ShowcasePreflightRequest
    provider_runtime: ProviderRoundRobinStatePort
    progress_hook: _ShowcasePreflightProgressHook | None = None


def _resolve_showcase_preflight_command(
    command: ShowcasePreflightCommand | None,
    legacy_kwargs: dict[str, Any],
) -> ShowcasePreflightCommand:
    if command is not None:
        if legacy_kwargs:
            raise TypeError("cannot mix ShowcasePreflightCommand with keyword arguments")
        return command
    params = dict(legacy_kwargs)
    db = params.pop("db")
    provider_runtime = params.pop("provider_runtime")
    progress_hook = params.pop("progress_hook", None)
    params.setdefault("reference_images", None)
    params.setdefault("reference_image_skips", None)
    return ShowcasePreflightCommand(
        db=db,
        request=ShowcasePreflightRequest(**params),
        provider_runtime=provider_runtime,
        progress_hook=progress_hook,
    )


async def _prepare_showcase_preflight_impl(
    command: ShowcasePreflightCommand | None = None,
    **legacy_kwargs: Any,
) -> dict[str, Any]:
    cmd = _resolve_showcase_preflight_command(command, legacy_kwargs)
    db = cmd.db
    request = cmd.request
    provider_runtime = cmd.provider_runtime
    progress_hook = cmd.progress_hook
    brief = request.selected_candidate.model_brief_json or {}
    model_summary = str(brief.get("summary") or request.user_prompt or "自然电商模特")
    garment_lock = _build_garment_lock(request.product_analysis)
    provider_order = await resolve_preflight_provider_order(
        db,
        request,
        provider_runtime,
    )
    planning, scene_cards = await prepare_scene_planning(
        db,
        request,
        garment_lock=garment_lock,
        model_summary=model_summary,
        provider_order=provider_order,
        provider_runtime=provider_runtime,
        progress_hook=progress_hook,
    )
    per_image_prompts, prompt_records = await compose_prompt_records(
        request,
        scene_cards,
        garment_lock=garment_lock,
        progress_hook=progress_hook,
    )
    prompt_reviews = await review_prompt_records(
        db,
        request,
        prompt_records,
        planning=planning,
        garment_lock=garment_lock,
        model_summary=model_summary,
        provider_order=provider_order,
        provider_runtime=provider_runtime,
        progress_hook=progress_hook,
    )
    final_prompts = [str(record.get("final_prompt") or "") for record in prompt_records]
    await notify_preflight_progress(
        progress_hook,
        "dispatching",
        "GPT-5.5 批量镜头和提示词已完成，正在派发生图任务",
        len(final_prompts),
        len(request.shot_picks),
    )
    return {
        "garment_lock": garment_lock,
        "planning": planning,
        "scene_cards": scene_cards,
        "per_image_prompts": per_image_prompts,
        "prompt_reviews": prompt_reviews,
        "final_prompts": final_prompts,
        "gpt55_reference_image_labels": [
            str(ref.get("label") or "").strip()
            for ref in (request.reference_images or [])
            if str(ref.get("label") or "").strip()
        ],
        "gpt55_reference_image_skips": list(request.reference_image_skips or []),
    }


# Public workflow contracts.
ShowcasePreflightProgressHook = _ShowcasePreflightProgressHook
prepare_showcase_preflight_impl = _prepare_showcase_preflight_impl
