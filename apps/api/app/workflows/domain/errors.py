"""Pure workflow domain errors."""


class WorkflowDomainError(ValueError):
    """Base class for invalid domain values or transitions."""


class InvalidWorkflowCommand(WorkflowDomainError):
    """The command is structurally invalid."""


class InvalidWorkflowPlan(WorkflowDomainError):
    """The selected policy produced an invalid plan."""


class ModelLibrarySyncLimitExceeded(ValueError):
    """A model-library sync exceeded a bounded input budget."""


class ModelLibrarySyncLeaseLost(RuntimeError):
    """A model-library sync lost ownership of its lease."""


__all__ = [
    "InvalidWorkflowCommand",
    "InvalidWorkflowPlan",
    "ModelLibrarySyncLeaseLost",
    "ModelLibrarySyncLimitExceeded",
    "WorkflowDomainError",
]
