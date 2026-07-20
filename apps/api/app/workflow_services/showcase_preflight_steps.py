"""Planning, prompt assembly, and review stages for showcase preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import ModelCandidate

from ..workflow_domain.showcase_shot_pool import ShotClass, ShotVariant


ShowcasePreflightProgressHook = Callable[
    [str, str, int | None, int | None],
    Awaitable[None],
]


@dataclass(frozen=True)
class ShowcasePreflightRequest:
    product_analysis: dict[str, Any]
    selected_candidate: ModelCandidate
    accessory_plan: dict[str, Any]
    template: str
    shot_picks: list[tuple[ShotClass, ShotVariant]]
    age_segment: str | None
    final_quality: str
    user_prompt: str
    aspect_ratio: str
    scene_environment: str
    scene_strategy: str
    scene_variety: str
    scene_planner: str
    continuity_anchor: str
    allow_pet: bool
    allow_background_people: bool
    reference_images: list[dict[str, str]] | None
    reference_image_skips: list[dict[str, str]] | None


async def notify_preflight_progress(
    progress_hook: ShowcasePreflightProgressHook | None,
    phase: str,
    detail: str,
    current: int | None,
    total: int | None,
) -> None:
    if progress_hook is not None:
        await progress_hook(phase, detail, current, total)


def _serialized_shot_picks(
    request: ShowcasePreflightRequest,
) -> list[tuple[ShotClass, dict[str, Any]]]:
    return [(shot_class, dict(variant)) for shot_class, variant in request.shot_picks]


async def resolve_preflight_provider_order(
    runtime: Any,
    db: AsyncSession,
    request: ShowcasePreflightRequest,
) -> Any:
    if request.scene_planner == "rules_fallback":
        return None
    try:
        return await runtime._resolve_scene_provider_order(db)
    except Exception as exc:  # noqa: BLE001
        runtime.logger.warning(
            "apparel scene provider resolution failed (will retry per call): %s",
            exc,
        )
        return None


def _rules_fallback_planning(
    runtime: Any,
    request: ShowcasePreflightRequest,
) -> dict[str, Any]:
    return runtime._rules_fallback_scene_planning(
        product_analysis=request.product_analysis,
        template=request.template,
        scene_environment=request.scene_environment,
        shot_picks=_serialized_shot_picks(request),
        aspect_ratio=request.aspect_ratio,
        user_prompt=request.user_prompt,
        accessory_plan=request.accessory_plan,
        allow_pet=request.allow_pet,
        continuity_anchor=request.continuity_anchor,
        scene_strategy=request.scene_strategy,
        scene_variety=request.scene_variety,
    )


async def _initial_scene_planning(
    runtime: Any,
    db: AsyncSession,
    request: ShowcasePreflightRequest,
    *,
    garment_lock: dict[str, Any],
    model_summary: str,
    provider_order: Any,
    progress_hook: ShowcasePreflightProgressHook | None,
) -> dict[str, Any]:
    total = len(request.shot_picks)
    if request.scene_planner == "rules_fallback":
        await notify_preflight_progress(
            progress_hook,
            "fallback",
            "正在用本地规则生成基础镜头方案",
            0,
            total,
        )
        return _rules_fallback_planning(runtime, request)
    await notify_preflight_progress(
        progress_hook,
        "director",
        f"GPT-5.5 正在根据商品图和模特图批量规划并扩写 {total} 张",
        0,
        total,
    )
    return await runtime._plan_scene_cards_with_gpt55(
        db,
        product_analysis=request.product_analysis,
        garment_lock=garment_lock,
        model_summary=model_summary,
        template=request.template,
        scene_environment=request.scene_environment,
        shot_picks=_serialized_shot_picks(request),
        aspect_ratio=request.aspect_ratio,
        output_count=total,
        user_prompt=request.user_prompt,
        accessory_plan=request.accessory_plan,
        scene_strategy=request.scene_strategy,
        scene_variety=request.scene_variety,
        continuity_anchor=request.continuity_anchor,
        allow_pet=request.allow_pet,
        allow_background_people=request.allow_background_people,
        provider_order=provider_order,
        reference_images=request.reference_images,
    )


async def prepare_scene_planning(
    runtime: Any,
    db: AsyncSession,
    request: ShowcasePreflightRequest,
    *,
    garment_lock: dict[str, Any],
    model_summary: str,
    provider_order: Any,
    progress_hook: ShowcasePreflightProgressHook | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    planning = await _initial_scene_planning(
        runtime,
        db,
        request,
        garment_lock=garment_lock,
        model_summary=model_summary,
        provider_order=provider_order,
        progress_hook=progress_hook,
    )
    scene_cards = list(planning.get("scene_cards") or [])
    if len(scene_cards) == len(request.shot_picks):
        return planning, scene_cards
    await notify_preflight_progress(
        progress_hook,
        "fallback",
        "GPT-5.5 镜头数量不匹配，正在切换本地规则兜底",
        0,
        len(request.shot_picks),
    )
    planning = _rules_fallback_planning(runtime, request)
    return planning, list(planning.get("scene_cards") or [])


def _prompt_item(
    *,
    index: int,
    shot_type: ShotClass,
    scene_card: dict[str, Any],
    shooting_brief: str,
    final_prompt: str,
) -> dict[str, Any]:
    return {
        "index": index,
        "scene_card_id": scene_card.get("id"),
        "shot_class": shot_type,
        "status": "director_batch" if shooting_brief else "scene_card",
        "shooting_brief": shooting_brief,
        "final_prompt": final_prompt,
        "candidate_briefs": [shooting_brief] if shooting_brief else [],
        "selected_candidate_index": None,
        "selection_scores": [],
        "scene_keywords": [
            value
            for value in (
                scene_card.get("scene_family"),
                scene_card.get("location"),
            )
            if str(value or "").strip()
        ],
        "composition_keywords": [
            value
            for value in (
                scene_card.get("composition"),
                scene_card.get("composition_detail"),
            )
            if str(value or "").strip()
        ],
        "lighting_keywords": [
            value
            for value in (
                scene_card.get("lighting"),
                scene_card.get("lighting_detail"),
            )
            if str(value or "").strip()
        ],
        "action_keywords": [
            value
            for value in (
                scene_card.get("micro_event"),
                scene_card.get("pose"),
                scene_card.get("motion"),
            )
            if str(value or "").strip()
        ],
        "photographic_idea_keywords": (
            [scene_card.get("creative_intent")]
            if str(scene_card.get("creative_intent") or "").strip()
            else []
        ),
        "product_visibility_checklist": (
            [str(scene_card.get("product_visibility") or "")]
            if str(scene_card.get("product_visibility") or "").strip()
            else []
        ),
        "negative_prompt_notes": list(scene_card.get("negative") or []),
        "regenerate_if": [],
        "fallback_reason": None,
    }


def _showcase_prompt(
    runtime: Any,
    request: ShowcasePreflightRequest,
    *,
    shot_type: ShotClass,
    variant: ShotVariant,
    scene_card: dict[str, Any] | None,
    garment_lock: dict[str, Any],
    composed_prompt: str,
) -> str:
    return runtime._showcase_prompt(
        product_analysis=request.product_analysis,
        selected_candidate=request.selected_candidate,
        accessory_plan=request.accessory_plan,
        template=request.template,
        shot_type=shot_type,
        shot_variant=variant,
        age_segment=request.age_segment,
        final_quality=request.final_quality,
        user_prompt=request.user_prompt,
        aspect_ratio=request.aspect_ratio,
        scene_environment=request.scene_environment,
        scene_card=scene_card,
        garment_lock=garment_lock,
        composed_prompt=composed_prompt,
        allow_pet=request.allow_pet,
        allow_background_people=request.allow_background_people,
    )


async def compose_prompt_records(
    runtime: Any,
    request: ShowcasePreflightRequest,
    scene_cards: list[dict[str, Any]],
    *,
    garment_lock: dict[str, Any],
    progress_hook: ShowcasePreflightProgressHook | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    per_image_prompts: list[dict[str, Any]] = []
    prompt_records: list[dict[str, Any]] = []
    total = len(request.shot_picks)
    for index, ((shot_type, variant), scene_card) in enumerate(
        zip(request.shot_picks, scene_cards),
        start=1,
    ):
        shooting_brief = str(scene_card.get("shooting_brief") or "").strip()
        await notify_preflight_progress(
            progress_hook,
            "composer",
            f"正在组装第 {index}/{total} 张最终提示词",
            index - 1,
            total,
        )
        final_prompt = _showcase_prompt(
            runtime,
            request,
            shot_type=shot_type,
            variant=variant,
            scene_card=scene_card,
            garment_lock=garment_lock,
            composed_prompt=shooting_brief,
        )
        prompt_item = _prompt_item(
            index=index,
            shot_type=shot_type,
            scene_card=scene_card,
            shooting_brief=shooting_brief,
            final_prompt=final_prompt,
        )
        per_image_prompts.append(prompt_item)
        prompt_records.append(
            {
                "index": index,
                "shot_type": shot_type,
                "variant": variant,
                "scene_card": scene_card,
                "prompt_item": prompt_item,
                "final_prompt": final_prompt,
            }
        )
        await notify_preflight_progress(
            progress_hook,
            "composer",
            f"已组装 {index}/{total} 张最终提示词",
            index,
            total,
        )
    return per_image_prompts, prompt_records


def build_review_batch_context(
    planning: dict[str, Any],
    request: ShowcasePreflightRequest,
) -> dict[str, Any]:
    return {
        "series_concept": planning.get("series_concept"),
        "continuity_anchors": list(planning.get("continuity_anchors") or []),
        "scene_fingerprints": list(planning.get("scene_fingerprints") or []),
        "scene_strategy": request.scene_strategy,
        "scene_variety": request.scene_variety,
        "continuity_anchor": request.continuity_anchor,
        "scene_count": len(request.shot_picks),
    }


def _rewrite_instruction(review: dict[str, Any]) -> str:
    return str(review.get("rewrite_instruction") or "").strip() or (
        "简化动作和道具关系，避免任何手、头发、宠物、饮料杯、手机、包带或"
        "前景物遮挡商品主体，并保持商品主体清楚。"
    )


async def _attempt_safe_rewrite(
    runtime: Any,
    db: AsyncSession,
    request: ShowcasePreflightRequest,
    record: dict[str, Any],
    *,
    garment_lock: dict[str, Any],
    model_summary: str,
    provider_order: Any,
    batch_context: dict[str, Any],
    review: dict[str, Any],
    rewrite_instruction: str,
    progress_hook: ShowcasePreflightProgressHook | None,
    review_total: int,
) -> dict[str, Any]:
    index = int(record["index"])
    shot_type = record["shot_type"]
    variant = record["variant"]
    scene_card = record["scene_card"]
    prompt_item = record["prompt_item"]
    rewritten = await runtime._compose_image_prompt_with_gpt55(
        db,
        base_prompt=str(record["final_prompt"] or ""),
        product_analysis=request.product_analysis,
        garment_lock=garment_lock,
        model_summary=model_summary,
        scene_card=scene_card,
        shot_class=shot_type,
        template=request.template,
        aspect_ratio=request.aspect_ratio,
        final_quality=request.final_quality,
        rewrite_instruction=rewrite_instruction,
        provider_order=provider_order,
        reference_images=request.reference_images,
    )
    rewritten_brief = runtime._composition_shooting_brief(rewritten)
    if not rewritten_brief:
        return review
    rewritten_prompt = _showcase_prompt(
        runtime,
        request,
        shot_type=shot_type,
        variant=variant,
        scene_card=scene_card,
        garment_lock=garment_lock,
        composed_prompt=rewritten_brief,
    )
    await notify_preflight_progress(
        progress_hook,
        "review",
        f"GPT-5.5 正在复核第 {index} 张改写结果",
        index - 1,
        review_total,
    )
    rewritten_review = await runtime._review_prompt_risk_with_gpt55(
        db,
        final_prompt=rewritten_prompt,
        garment_lock=garment_lock,
        scene_card=scene_card,
        batch_context=batch_context,
        provider_order=provider_order,
    )
    if rewritten_review.get("must_rewrite"):
        return review
    prompt_item.update(
        {
            **rewritten,
            "index": index,
            "scene_card_id": scene_card.get("id"),
            "shot_class": shot_type,
            "status": rewritten.get("status") or "rewritten",
            "final_prompt": rewritten_prompt,
            "risk_rewrite_applied": True,
        }
    )
    record["final_prompt"] = rewritten_prompt
    return rewritten_review


def _apply_guarded_prompt(
    runtime: Any,
    request: ShowcasePreflightRequest,
    record: dict[str, Any],
    *,
    garment_lock: dict[str, Any],
    review: dict[str, Any],
    rewrite_instruction: str,
) -> dict[str, Any]:
    scene_card = record["scene_card"]
    guarded_brief = runtime._guarded_shooting_brief(
        str(scene_card.get("shooting_brief") or ""),
        rewrite_instruction=rewrite_instruction,
    )
    guarded_prompt = _showcase_prompt(
        runtime,
        request,
        shot_type=record["shot_type"],
        variant=record["variant"],
        scene_card=None,
        garment_lock=garment_lock,
        composed_prompt=guarded_brief,
    )
    record["prompt_item"].update(
        {
            "status": "guarded",
            "shooting_brief": guarded_brief,
            "final_prompt": guarded_prompt,
            "candidate_briefs": [guarded_brief],
            "risk_rewrite_instruction": rewrite_instruction,
            "risk_guard_applied": True,
        }
    )
    record["final_prompt"] = guarded_prompt
    return {
        **review,
        "risk_level": "medium",
        "must_rewrite": False,
        "safe_fallback": False,
        "guarded_composer": True,
    }


async def _review_prompt_record(
    runtime: Any,
    db: AsyncSession,
    request: ShowcasePreflightRequest,
    record: dict[str, Any],
    *,
    garment_lock: dict[str, Any],
    model_summary: str,
    provider_order: Any,
    batch_context: dict[str, Any],
    use_gpt_review: bool,
    progress_hook: ShowcasePreflightProgressHook | None,
    review_total: int,
) -> dict[str, Any]:
    index = int(record["index"])
    scene_card = record["scene_card"]
    if not use_gpt_review:
        review = runtime._fallback_risk_review(
            scene_card=scene_card,
            reason="rules_fallback_preflight",
        )
        await notify_preflight_progress(
            progress_hook,
            "review",
            f"已完成 {index}/{review_total} 张本地风险标记",
            index,
            review_total,
        )
        return review
    await notify_preflight_progress(
        progress_hook,
        "review",
        f"GPT-5.5 正在复核第 {index}/{review_total} 张提示词风险",
        index - 1,
        review_total,
    )
    review = await runtime._review_prompt_risk_with_gpt55(
        db,
        final_prompt=str(record["final_prompt"] or ""),
        garment_lock=garment_lock,
        scene_card=scene_card,
        batch_context=batch_context,
        provider_order=provider_order,
    )
    if review.get("must_rewrite"):
        instruction = _rewrite_instruction(review)
        await notify_preflight_progress(
            progress_hook,
            "composer",
            f"GPT-5.5 正在按复核意见改写第 {index} 张提示词",
            index - 1,
            review_total,
        )
        review = await _attempt_safe_rewrite(
            runtime,
            db,
            request,
            record,
            garment_lock=garment_lock,
            model_summary=model_summary,
            provider_order=provider_order,
            batch_context=batch_context,
            review=review,
            rewrite_instruction=instruction,
            progress_hook=progress_hook,
            review_total=review_total,
        )
        if review.get("must_rewrite"):
            review = _apply_guarded_prompt(
                runtime,
                request,
                record,
                garment_lock=garment_lock,
                review=review,
                rewrite_instruction=instruction,
            )
    await notify_preflight_progress(
        progress_hook,
        "review",
        f"已完成 {index}/{review_total} 张提示词风险复核",
        index,
        review_total,
    )
    return review


async def review_prompt_records(
    runtime: Any,
    db: AsyncSession,
    request: ShowcasePreflightRequest,
    prompt_records: list[dict[str, Any]],
    *,
    planning: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    provider_order: Any,
    progress_hook: ShowcasePreflightProgressHook | None,
) -> list[dict[str, Any]]:
    prompt_reviews: list[dict[str, Any]] = []
    batch_context = build_review_batch_context(planning, request)
    use_gpt_review = (
        request.scene_planner != "rules_fallback"
        and str(planning.get("planner") or "") != "rules_fallback"
    )
    review_total = len(prompt_records)
    for record in prompt_records:
        review = await _review_prompt_record(
            runtime,
            db,
            request,
            record,
            garment_lock=garment_lock,
            model_summary=model_summary,
            provider_order=provider_order,
            batch_context=batch_context,
            use_gpt_review=use_gpt_review,
            progress_hook=progress_hook,
            review_total=review_total,
        )
        prompt_reviews.append({"index": int(record["index"]), **review})
    return prompt_reviews
