"""Typed workflow application errors."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import WorkflowKind
from ..domain.validation import ValidationIssue


class WorkflowApplicationError(RuntimeError):
    code = "workflow_application_error"


class WorkflowPolicyNotFoundError(WorkflowApplicationError):
    code = "workflow_policy_not_found"

    def __init__(self, kind: WorkflowKind) -> None:
        self.kind = kind
        super().__init__(f"workflow policy is not registered: {kind.value}")


class WorkflowNotFoundError(WorkflowApplicationError):
    code = "workflow_not_found"

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"workflow run was not found: {run_id}")


class InvalidWorkflowCursorError(WorkflowApplicationError):
    code = "invalid_cursor"

    def __init__(self) -> None:
        super().__init__("workflow cursor is invalid")


class WorkflowRequestError(WorkflowApplicationError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        error: dict[str, object] = {"code": code, "message": message}
        if details:
            error["details"] = details
        self.status_code = status_code
        self.detail = {"error": error}
        super().__init__(message)


@dataclass(frozen=True)
class WorkflowValidationError(WorkflowApplicationError):
    issues: tuple[ValidationIssue, ...]
    code = "workflow_validation_failed"

    def __str__(self) -> str:
        return "; ".join(issue.message for issue in self.issues)


__all__ = [
    "InvalidWorkflowCursorError",
    "WorkflowApplicationError",
    "WorkflowNotFoundError",
    "WorkflowPolicyNotFoundError",
    "WorkflowRequestError",
    "WorkflowValidationError",
]
