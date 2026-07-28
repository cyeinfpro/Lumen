"""Lifecycle-owned API runtime resources."""

from .container import (
    ApiRuntime,
    ApiRuntimeDiagnostics,
    CapabilityStatus,
    RuntimeCapability,
)
from .lifecycle import (
    CleanupCallback,
    CleanupFailure,
    LifecycleDiagnostics,
    LifecycleState,
    RuntimeLifecycle,
)

__all__ = [
    "ApiRuntime",
    "ApiRuntimeDiagnostics",
    "CapabilityStatus",
    "CleanupCallback",
    "CleanupFailure",
    "LifecycleDiagnostics",
    "LifecycleState",
    "RuntimeCapability",
    "RuntimeLifecycle",
]
