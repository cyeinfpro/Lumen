"""Durable sidecar execution and billing boundary helpers."""

from __future__ import annotations

from typing import Any

from ...upstream_clients.image_job_models import (
    ImageJobCostKnowledge,
    ImageJobExecutionHandle,
)


SIDECAR_EXECUTION_KEY = "sidecar_execution"


def sidecar_execution_from_request(
    upstream_request: dict[str, Any] | None,
) -> ImageJobExecutionHandle | None:
    if not isinstance(upstream_request, dict):
        return None
    return ImageJobExecutionHandle.from_mapping(
        upstream_request.get(SIDECAR_EXECUTION_KEY)
    )


def sidecar_cost_requires_settlement(
    upstream_request: dict[str, Any] | None,
) -> bool:
    execution = sidecar_execution_from_request(upstream_request)
    return bool(
        execution is not None
        and execution.cost_knowledge
        in {
            ImageJobCostKnowledge.UNKNOWN,
            ImageJobCostKnowledge.INCURRED,
        }
    )


async def release_or_settle_generation(
    billing: Any,
    session: Any,
    generation: Any,
    *,
    reason: str,
) -> None:
    execution = sidecar_execution_from_request(
        getattr(generation, "upstream_request", None)
    )
    if execution is not None and execution.cost_knowledge in {
        ImageJobCostKnowledge.UNKNOWN,
        ImageJobCostKnowledge.INCURRED,
    }:
        await billing.settle_unknown_upstream(
            session,
            generation,
            reason=reason,
            knowledge=execution.cost_knowledge.value,
        )
        return
    await billing.release(session, generation, reason=reason)


__all__ = [
    "SIDECAR_EXECUTION_KEY",
    "release_or_settle_generation",
    "sidecar_cost_requires_settlement",
    "sidecar_execution_from_request",
]
