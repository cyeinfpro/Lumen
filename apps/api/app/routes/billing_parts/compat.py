"""Runtime dependency injection for billing route domain modules."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable


_runtime_provider: ContextVar[Callable[[], Any] | None] = ContextVar(
    "billing_route_runtime_provider",
    default=None,
)


def configure_runtime(provider: Callable[[], Any]) -> None:
    """Install the facade-owned runtime provider.

    The provider is injected by the public route facade during application
    startup. Keeping the provider here, rather than importing the facade,
    makes the dependency direction one-way.
    """

    _runtime_provider.set(provider)


def current_runtime() -> Any:
    """Return the currently configured route runtime."""

    provider = _runtime_provider.get()
    if provider is None:
        raise RuntimeError("billing route runtime has not been configured")
    return provider()
