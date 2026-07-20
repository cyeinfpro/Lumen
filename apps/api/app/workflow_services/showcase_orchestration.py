"""Showcase scene planning, prompt review, and preflight orchestration."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.models import ModelCandidate

from ..workflow_domain.showcase_shot_pool import ShotClass, ShotVariant
from .showcase_preflight_steps import (
    ShowcasePreflightProgressHook as _ShowcasePreflightProgressHook,
    ShowcasePreflightRequest,
    compose_prompt_records,
    notify_preflight_progress,
    prepare_scene_planning,
    resolve_preflight_provider_order,
    review_prompt_records,
)
from .showcase_runtime import runtime as _runtime


async def _prepare_showcase_preflight_impl(
    *,
    db: AsyncSession,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
    accessory_plan: dict[str, Any],
    template: str,
    shot_picks: list[tuple[ShotClass, ShotVariant]],
    age_segment: str | None,
    final_quality: str,
    user_prompt: str,
    aspect_ratio: str,
    scene_environment: str,
    scene_strategy: str,
    scene_variety: str,
    scene_planner: str,
    continuity_anchor: str,
    allow_pet: bool,
    allow_background_people: bool,
    reference_images: list[dict[str, str]] | None = None,
    reference_image_skips: list[dict[str, str]] | None = None,
    progress_hook: _ShowcasePreflightProgressHook | None = None,
) -> dict[str, Any]:
    runtime = _runtime()
    request = ShowcasePreflightRequest(
        product_analysis=product_analysis,
        selected_candidate=selected_candidate,
        accessory_plan=accessory_plan,
        template=template,
        shot_picks=shot_picks,
        age_segment=age_segment,
        final_quality=final_quality,
        user_prompt=user_prompt,
        aspect_ratio=aspect_ratio,
        scene_environment=scene_environment,
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
        scene_planner=scene_planner,
        continuity_anchor=continuity_anchor,
        allow_pet=allow_pet,
        allow_background_people=allow_background_people,
        reference_images=reference_images,
        reference_image_skips=reference_image_skips,
    )
    brief = selected_candidate.model_brief_json or {}
    model_summary = str(brief.get("summary") or user_prompt or "自然电商模特")
    garment_lock = runtime._build_garment_lock(product_analysis)
    provider_order = await resolve_preflight_provider_order(runtime, db, request)
    planning, scene_cards = await prepare_scene_planning(
        runtime,
        db,
        request,
        garment_lock=garment_lock,
        model_summary=model_summary,
        provider_order=provider_order,
        progress_hook=progress_hook,
    )
    per_image_prompts, prompt_records = await compose_prompt_records(
        runtime,
        request,
        scene_cards,
        garment_lock=garment_lock,
        progress_hook=progress_hook,
    )
    prompt_reviews = await review_prompt_records(
        runtime,
        db,
        request,
        prompt_records,
        planning=planning,
        garment_lock=garment_lock,
        model_summary=model_summary,
        provider_order=provider_order,
        progress_hook=progress_hook,
    )
    final_prompts = [str(record.get("final_prompt") or "") for record in prompt_records]
    await notify_preflight_progress(
        progress_hook,
        "dispatching",
        "GPT-5.5 批量镜头和提示词已完成，正在派发生图任务",
        len(final_prompts),
        len(shot_picks),
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
