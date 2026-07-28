"""Narrow service views for generation runner phases."""

from __future__ import annotations

from dataclasses import dataclass

from .services import (
    GenerationArtifactService,
    GenerationBillingService,
    GenerationEventService,
    GenerationProviderService,
    GenerationStoreService,
    RunGenerationDeps,
)


@dataclass(frozen=True, slots=True)
class ClaimGenerationServices:
    store: GenerationStoreService
    artifacts: GenerationArtifactService
    billing: GenerationBillingService
    events: GenerationEventService

    @classmethod
    def from_deps(cls, deps: RunGenerationDeps) -> ClaimGenerationServices:
        return cls(
            store=deps.store,
            artifacts=deps.artifacts,
            billing=deps.billing,
            events=deps.events,
        )


@dataclass(frozen=True, slots=True)
class DispatchGenerationServices:
    store: GenerationStoreService
    events: GenerationEventService
    provider: GenerationProviderService

    @classmethod
    def from_deps(cls, deps: RunGenerationDeps) -> DispatchGenerationServices:
        return cls(
            store=deps.store,
            events=deps.events,
            provider=deps.provider,
        )


__all__ = [
    "ClaimGenerationServices",
    "DispatchGenerationServices",
]
