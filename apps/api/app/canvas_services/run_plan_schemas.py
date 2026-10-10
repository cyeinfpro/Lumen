"""Strict, additive request contracts for previewed batch Canvas runs."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class PlanOutputChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(min_length=1, max_length=36)
    output_index: int = Field(ge=0, le=9, strict=True)


class CanvasPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_revision: int = Field(ge=1)
    kind: Literal["upstream", "selection", "all"]
    target_node_ids: list[str] = Field(default_factory=list, max_length=1000)
    reuse_outputs: dict[str, PlanOutputChoice] = Field(
        default_factory=dict, max_length=1000
    )
    output_indices: dict[str, StrictInt] = Field(default_factory=dict, max_length=1000)
    budget_micro: int = Field(ge=0, le=2**63 - 1)
    failure_policy: Literal["continue_independent", "fail_fast"] = (
        "continue_independent"
    )
    auto_select_on_success: bool = True


class CanvasPlanStartIn(CanvasPlanIn):
    idempotency_key: str = Field(min_length=1, max_length=96)
    plan_hash: str = Field(min_length=64, max_length=64)


class CanvasPlanRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=96)
    execution_ids: list[str] = Field(min_length=1, max_length=1000)
    additional_budget_micro: int = Field(ge=0, le=2**63 - 1)
