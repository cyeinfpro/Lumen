"""Workflow identifier value types."""

from __future__ import annotations

from typing import NewType


WorkflowRunId = NewType("WorkflowRunId", str)
WorkflowStepId = NewType("WorkflowStepId", str)
WorkflowIdempotencyKey = NewType("WorkflowIdempotencyKey", str)


def require_identifier(value: str, *, name: str, max_length: int = 128) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return normalized


__all__ = [
    "WorkflowIdempotencyKey",
    "WorkflowRunId",
    "WorkflowStepId",
    "require_identifier",
]
