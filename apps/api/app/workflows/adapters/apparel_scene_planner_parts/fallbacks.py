"""Deterministic fallback result assembly."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...domain.apparel_scene_fallbacks import (
    clean_text,
    coerce_string_list,
)
from .contracts import FallbackPlanningRequest

SceneCardsBuilder = Callable[..., list[dict[str, Any]]]
PlanningResultBuilder = Callable[..., dict[str, Any]]


def rules_fallback_planning(
    *,
    request: FallbackPlanningRequest,
    fallback_scene_cards: SceneCardsBuilder,
    fallback_result: PlanningResultBuilder,
) -> dict[str, Any]:
    cards = fallback_scene_cards(
        product_analysis=request.product_analysis,
        template=request.template,
        scene_environment=request.scene_environment,
        shot_picks=request.shot_picks,
        aspect_ratio=request.aspect_ratio,
        user_prompt=request.user_prompt,
        accessory_plan=request.accessory_plan,
        allow_pet=request.allow_pet,
        continuity_anchor=request.continuity_anchor,
        scene_strategy=request.scene_strategy,
        scene_variety=request.scene_variety,
    )
    return fallback_result(cards, reason="rules_fallback_requested")


def fallback_planning_result(
    cards: list[dict[str, Any]],
    *,
    reason: str,
    unique_fingerprints: Callable[[list[dict[str, Any]]], list[str]],
) -> dict[str, Any]:
    return {
        "planner": "rules_fallback",
        "planner_status": "fallback",
        "series_concept": "规则兜底自然服饰展示",
        "continuity_anchors": [],
        "scene_cards": cards,
        "scene_fingerprints": unique_fingerprints(cards),
        "risk_notes": [reason[:200]] if reason else [],
        "fallback_reason": reason[:500] if reason else None,
    }


def fallback_prompt_composition(
    *,
    base_prompt: str,
    scene_card: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    del base_prompt
    return {
        "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
        "status": "fallback",
        "shooting_brief": "",
        "final_prompt": "",
        "candidate_briefs": [],
        "selected_candidate_index": None,
        "selection_scores": [],
        "scene_keywords": [],
        "composition_keywords": [],
        "lighting_keywords": [],
        "action_keywords": [],
        "photographic_idea_keywords": [],
        "product_visibility_checklist": [],
        "negative_prompt_notes": coerce_string_list(
            scene_card.get("negative"), max_items=8, max_len=100
        ),
        "regenerate_if": [],
        "fallback_reason": reason[:500],
    }


def fallback_risk_review(
    *,
    scene_card: dict[str, Any],
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
        "status": "fallback",
        "risk_level": "medium",
        "risks": [reason[:200]] if reason else [],
        "must_rewrite": False,
        "rewrite_instruction": "",
        "fallback_reason": reason[:500] if reason else None,
    }
