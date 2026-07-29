"""Shared contracts for apparel scene planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from lumen_core.providers import ProviderDefinition

from ...ports.runtime_state import ProviderRoundRobinStatePort

SceneStrategy = Literal["balanced", "natural_series", "editorial_campaign"]
SceneVariety = Literal["safe", "rich", "wild"]
ScenePlannerMode = Literal["gpt55_preflight", "gpt55_batch_only", "rules_fallback"]
ContinuityAnchor = Literal["none", "accessory", "pet", "location_series"]


@dataclass(frozen=True, slots=True)
class SceneProviderSelection:
    order: tuple[ProviderDefinition, ...] | None = None
    runtime: ProviderRoundRobinStatePort | None = None


@dataclass(frozen=True, slots=True)
class ScenePlanningRequest:
    product_analysis: dict[str, Any]
    garment_lock: dict[str, Any]
    model_summary: str
    template: str
    scene_environment: str
    shot_picks: list[tuple[str, dict[str, Any]]]
    aspect_ratio: str
    output_count: int
    user_prompt: str
    accessory_plan: dict[str, Any]
    scene_strategy: str
    scene_variety: str
    continuity_anchor: str
    allow_pet: bool
    allow_background_people: bool


@dataclass(frozen=True, slots=True)
class FallbackPlanningRequest:
    product_analysis: dict[str, Any]
    template: str
    scene_environment: str
    shot_picks: list[tuple[str, dict[str, Any]]]
    aspect_ratio: str
    user_prompt: str
    accessory_plan: dict[str, Any]
    allow_pet: bool
    continuity_anchor: str
    scene_strategy: str
    scene_variety: str


@dataclass(frozen=True, slots=True)
class PromptCompositionRequest:
    base_prompt: str
    product_analysis: dict[str, Any]
    garment_lock: dict[str, Any]
    model_summary: str
    scene_card: dict[str, Any]
    shot_class: str
    template: str
    aspect_ratio: str
    final_quality: str
    rewrite_instruction: str | None
