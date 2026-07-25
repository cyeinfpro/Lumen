"""Workflow policy contracts."""

from __future__ import annotations

from typing import Protocol

from .models import WorkflowCommand, WorkflowKind, WorkflowPlan
from .validation import ValidationResult


class WorkflowPolicy(Protocol):
    kind: WorkflowKind

    def validate(self, command: WorkflowCommand) -> ValidationResult: ...

    def plan(self, command: WorkflowCommand) -> WorkflowPlan: ...


__all__ = ["WorkflowPolicy"]
