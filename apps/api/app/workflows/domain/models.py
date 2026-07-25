"""Immutable workflow domain values."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from .ids import require_identifier


class WorkflowKind(StrEnum):
    APPAREL_SHOWCASE = "apparel_model_showcase"
    APPAREL_MODEL_LIBRARY = "apparel_model_library_generate"
    POSTER_DESIGN = "poster_design"


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class WorkflowInput:
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _immutable_mapping(self.payload))


@dataclass(frozen=True)
class WorkflowCommand:
    user_id: str
    workflow_kind: WorkflowKind
    input: WorkflowInput
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_id",
            require_identifier(self.user_id, name="user_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            require_identifier(
                self.idempotency_key,
                name="idempotency_key",
                max_length=256,
            ),
        )


@dataclass(frozen=True)
class WorkflowStepPlan:
    key: str
    title: str
    requires_approval: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", require_identifier(self.key, name="step key"))
        if not self.title.strip():
            raise ValueError("step title is required")


@dataclass(frozen=True)
class AssetRequirement:
    role: str
    minimum: int = 0
    maximum: int | None = None

    def __post_init__(self) -> None:
        if self.minimum < 0:
            raise ValueError("asset minimum cannot be negative")
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("asset maximum cannot be less than minimum")


@dataclass(frozen=True)
class CostEstimate:
    minimum_micro: int = 0
    maximum_micro: int = 0

    def __post_init__(self) -> None:
        if self.minimum_micro < 0 or self.maximum_micro < self.minimum_micro:
            raise ValueError("invalid workflow cost estimate")


@dataclass(frozen=True)
class WorkflowPlan:
    steps: tuple[WorkflowStepPlan, ...]
    required_assets: tuple[AssetRequirement, ...] = ()
    estimated_cost: CostEstimate = field(default_factory=CostEstimate)

    def __post_init__(self) -> None:
        keys = [step.key for step in self.steps]
        if not keys:
            raise ValueError("workflow plan requires at least one step")
        if len(keys) != len(set(keys)):
            raise ValueError("workflow plan step keys must be unique")


@dataclass(frozen=True)
class WorkflowRunSnapshot:
    run_id: str
    user_id: str
    workflow_kind: WorkflowKind
    status: str
    current_step: str
    version: int = 1
    output: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            require_identifier(self.run_id, name="run_id"),
        )
        object.__setattr__(self, "output", _immutable_mapping(self.output))


__all__ = [
    "AssetRequirement",
    "CostEstimate",
    "WorkflowCommand",
    "WorkflowInput",
    "WorkflowKind",
    "WorkflowPlan",
    "WorkflowRunSnapshot",
    "WorkflowStepPlan",
]
