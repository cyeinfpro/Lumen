"""Shared contracts for apparel scene planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
