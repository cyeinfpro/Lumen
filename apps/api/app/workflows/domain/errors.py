"""Pure workflow domain errors."""


class WorkflowDomainError(ValueError):
    """Base class for invalid domain values or transitions."""


class InvalidWorkflowCommand(WorkflowDomainError):
    """The command is structurally invalid."""


class InvalidWorkflowPlan(WorkflowDomainError):
    """The selected policy produced an invalid plan."""


__all__ = [
    "InvalidWorkflowCommand",
    "InvalidWorkflowPlan",
    "WorkflowDomainError",
]
