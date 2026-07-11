"""Showcase scene planning, prompt review, and preflight orchestration."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.models import ModelCandidate

from ..routes._showcase_shot_pool import ShotClass, ShotVariant
from .showcase_runtime import runtime as _runtime


_ShowcasePreflightProgressHook = Callable[
    [str, str, int | None, int | None],
    Awaitable[None],
]


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
    brief = selected_candidate.model_brief_json or {}
    model_summary = str(brief.get("summary") or user_prompt or "自然电商模特")
    garment_lock = runtime._build_garment_lock(product_analysis)
    provider_order = None
    if scene_planner != "rules_fallback":
        try:
            provider_order = await runtime._resolve_scene_provider_order(db)
        except Exception as exc:  # noqa: BLE001
            runtime.logger.warning(
                "apparel scene provider resolution failed (will retry per call): %s",
                exc,
            )
            provider_order = None
    if scene_planner == "rules_fallback":
        if progress_hook is not None:
            await progress_hook(
                "fallback",
                "正在用本地规则生成基础镜头方案",
                0,
                len(shot_picks),
            )
        planning = runtime._rules_fallback_scene_planning(
            product_analysis=product_analysis,
            template=template,
            scene_environment=scene_environment,
            shot_picks=[(cls, dict(variant)) for cls, variant in shot_picks],
            aspect_ratio=aspect_ratio,
            user_prompt=user_prompt,
            accessory_plan=accessory_plan,
            allow_pet=allow_pet,
            continuity_anchor=continuity_anchor,
            scene_strategy=scene_strategy,
            scene_variety=scene_variety,
        )
    else:
        if progress_hook is not None:
            await progress_hook(
                "director",
                f"GPT-5.5 正在根据商品图和模特图批量规划并扩写 {len(shot_picks)} 张",
                0,
                len(shot_picks),
            )
        planning = await runtime._plan_scene_cards_with_gpt55(
            db,
            product_analysis=product_analysis,
            garment_lock=garment_lock,
            model_summary=model_summary,
            template=template,
            scene_environment=scene_environment,
            shot_picks=[(cls, dict(variant)) for cls, variant in shot_picks],
            aspect_ratio=aspect_ratio,
            output_count=len(shot_picks),
            user_prompt=user_prompt,
            accessory_plan=accessory_plan,
            scene_strategy=scene_strategy,
            scene_variety=scene_variety,
            continuity_anchor=continuity_anchor,
            allow_pet=allow_pet,
            allow_background_people=allow_background_people,
            provider_order=provider_order,
            reference_images=reference_images,
        )
    scene_cards = list(planning.get("scene_cards") or [])
    if len(scene_cards) != len(shot_picks):
        if progress_hook is not None:
            await progress_hook(
                "fallback",
                "GPT-5.5 镜头数量不匹配，正在切换本地规则兜底",
                0,
                len(shot_picks),
            )
        planning = runtime._rules_fallback_scene_planning(
            product_analysis=product_analysis,
            template=template,
            scene_environment=scene_environment,
            shot_picks=[(cls, dict(variant)) for cls, variant in shot_picks],
            aspect_ratio=aspect_ratio,
            user_prompt=user_prompt,
            accessory_plan=accessory_plan,
            allow_pet=allow_pet,
            continuity_anchor=continuity_anchor,
            scene_strategy=scene_strategy,
            scene_variety=scene_variety,
        )
        scene_cards = list(planning.get("scene_cards") or [])

    per_image_prompts: list[dict[str, Any]] = []
    prompt_reviews: list[dict[str, Any]] = []
    prompt_records: list[dict[str, Any]] = []
    use_gpt_review = (
        scene_planner != "rules_fallback"
        and str(planning.get("planner") or "") != "rules_fallback"
    )

    for idx, ((shot_type, variant), scene_card) in enumerate(
        zip(shot_picks, scene_cards),
        start=1,
    ):
        shooting_brief = str(scene_card.get("shooting_brief") or "").strip()
        if progress_hook is not None:
            await progress_hook(
                "composer",
                f"正在组装第 {idx}/{len(shot_picks)} 张最终提示词",
                idx - 1,
                len(shot_picks),
            )
        final_prompt = runtime._showcase_prompt(
            product_analysis=product_analysis,
            selected_candidate=selected_candidate,
            accessory_plan=accessory_plan,
            template=template,
            shot_type=shot_type,
            shot_variant=variant,
            age_segment=age_segment,
            final_quality=final_quality,
            user_prompt=user_prompt,
            aspect_ratio=aspect_ratio,
            scene_environment=scene_environment,
            scene_card=scene_card,
            garment_lock=garment_lock,
            composed_prompt=shooting_brief,
            allow_pet=allow_pet,
            allow_background_people=allow_background_people,
        )
        prompt_item = {
            "index": idx,
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
        per_image_prompts.append(prompt_item)
        prompt_records.append(
            {
                "index": idx,
                "shot_type": shot_type,
                "variant": variant,
                "scene_card": scene_card,
                "prompt_item": prompt_item,
                "final_prompt": final_prompt,
            }
        )
        if progress_hook is not None:
            await progress_hook(
                "composer",
                f"已组装 {idx}/{len(shot_picks)} 张最终提示词",
                idx,
                len(shot_picks),
            )
    batch_context = {
        "series_concept": planning.get("series_concept"),
        "continuity_anchors": list(planning.get("continuity_anchors") or []),
        "scene_fingerprints": list(planning.get("scene_fingerprints") or []),
        "scene_strategy": scene_strategy,
        "scene_variety": scene_variety,
        "continuity_anchor": continuity_anchor,
        "scene_count": len(shot_picks),
    }
    review_total = len(prompt_records)
    for record in prompt_records:
        idx = int(record["index"])
        shot_type = record["shot_type"]
        variant = record["variant"]
        scene_card = record["scene_card"]
        prompt_item = record["prompt_item"]
        final_prompt = str(record["final_prompt"] or "")
        if not use_gpt_review:
            reason = "rules_fallback_preflight"
            review = runtime._fallback_risk_review(
                scene_card=scene_card,
                reason=reason,
            )
            prompt_reviews.append({"index": idx, **review})
            if progress_hook is not None:
                await progress_hook(
                    "review",
                    f"已完成 {idx}/{review_total} 张本地风险标记",
                    idx,
                    review_total,
                )
            continue

        if progress_hook is not None:
            await progress_hook(
                "review",
                f"GPT-5.5 正在复核第 {idx}/{review_total} 张提示词风险",
                idx - 1,
                review_total,
            )
        review = await runtime._review_prompt_risk_with_gpt55(
            db,
            final_prompt=final_prompt,
            garment_lock=garment_lock,
            scene_card=scene_card,
            batch_context=batch_context,
            provider_order=provider_order,
        )
        if review.get("must_rewrite"):
            rewrite_instruction = str(review.get("rewrite_instruction") or "").strip()
            if not rewrite_instruction:
                rewrite_instruction = (
                    "简化动作和道具关系，避免任何手、头发、宠物、饮料杯、手机、包带或"
                    "前景物遮挡商品主体，并保持商品主体清楚。"
                )
            if progress_hook is not None:
                await progress_hook(
                    "composer",
                    f"GPT-5.5 正在按复核意见改写第 {idx} 张提示词",
                    idx - 1,
                    review_total,
                )
            rewritten = await runtime._compose_image_prompt_with_gpt55(
                db,
                base_prompt=final_prompt,
                product_analysis=product_analysis,
                garment_lock=garment_lock,
                model_summary=model_summary,
                scene_card=scene_card,
                shot_class=shot_type,
                template=template,
                aspect_ratio=aspect_ratio,
                final_quality=final_quality,
                rewrite_instruction=rewrite_instruction,
                provider_order=provider_order,
                reference_images=reference_images,
            )
            rewritten_brief = runtime._composition_shooting_brief(rewritten)
            if rewritten_brief:
                rewritten_prompt = runtime._showcase_prompt(
                    product_analysis=product_analysis,
                    selected_candidate=selected_candidate,
                    accessory_plan=accessory_plan,
                    template=template,
                    shot_type=shot_type,
                    shot_variant=variant,
                    age_segment=age_segment,
                    final_quality=final_quality,
                    user_prompt=user_prompt,
                    aspect_ratio=aspect_ratio,
                    scene_environment=scene_environment,
                    scene_card=scene_card,
                    garment_lock=garment_lock,
                    composed_prompt=rewritten_brief,
                    allow_pet=allow_pet,
                    allow_background_people=allow_background_people,
                )
                if progress_hook is not None:
                    await progress_hook(
                        "review",
                        f"GPT-5.5 正在复核第 {idx} 张改写结果",
                        idx - 1,
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
                if not rewritten_review.get("must_rewrite"):
                    prompt_item.update(
                        {
                            **rewritten,
                            "index": idx,
                            "scene_card_id": scene_card.get("id"),
                            "shot_class": shot_type,
                            "status": rewritten.get("status") or "rewritten",
                            "final_prompt": rewritten_prompt,
                            "risk_rewrite_applied": True,
                        }
                    )
                    record["final_prompt"] = rewritten_prompt
                    final_prompt = rewritten_prompt
                    review = rewritten_review
            if review.get("must_rewrite"):
                guarded_brief = runtime._guarded_shooting_brief(
                    str(scene_card.get("shooting_brief") or ""),
                    rewrite_instruction=rewrite_instruction,
                )
                guarded_prompt = runtime._showcase_prompt(
                    product_analysis=product_analysis,
                    selected_candidate=selected_candidate,
                    accessory_plan=accessory_plan,
                    template=template,
                    shot_type=shot_type,
                    shot_variant=variant,
                    age_segment=age_segment,
                    final_quality=final_quality,
                    user_prompt=user_prompt,
                    aspect_ratio=aspect_ratio,
                    scene_environment=scene_environment,
                    scene_card=None,
                    garment_lock=garment_lock,
                    composed_prompt=guarded_brief,
                    allow_pet=allow_pet,
                    allow_background_people=allow_background_people,
                )
                prompt_item.update(
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
                final_prompt = guarded_prompt
                review = {
                    **review,
                    "risk_level": "medium",
                    "must_rewrite": False,
                    "safe_fallback": False,
                    "guarded_composer": True,
                }
        prompt_reviews.append({"index": idx, **review})
        if progress_hook is not None:
            await progress_hook(
                "review",
                f"已完成 {idx}/{review_total} 张提示词风险复核",
                idx,
                review_total,
            )
    final_prompts = [str(record.get("final_prompt") or "") for record in prompt_records]
    if progress_hook is not None:
        await progress_hook(
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
            for ref in (reference_images or [])
            if str(ref.get("label") or "").strip()
        ],
        "gpt55_reference_image_skips": list(reference_image_skips or []),
    }
