"""Lifecycle-owned API runtime resources."""

from .container import (
    ApiProcessState,
    ApiRuntime,
    ApiRuntimeDiagnostics,
    CapabilityStatus,
    RuntimeCapability,
    bind_api_process_state,
)
from .lifecycle import (
    CleanupCallback,
    CleanupFailure,
    LifecycleDiagnostics,
    LifecycleState,
    RuntimeLifecycle,
)
from .slot import RuntimeSlot

__all__ = [
    "ApiProcessState",
    "ApiRuntime",
    "ApiRuntimeDiagnostics",
    "CapabilityStatus",
    "CleanupCallback",
    "CleanupFailure",
    "LifecycleDiagnostics",
    "LifecycleState",
    "RuntimeCapability",
    "RuntimeLifecycle",
    "RuntimeSlot",
    "bind_api_process_state",
]
