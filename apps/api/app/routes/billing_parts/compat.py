"""Runtime dependency injection for billing route domain modules."""

from __future__ import annotations

from typing import Any, Callable


_runtime_provider: Callable[[], Any] | None = None


def configure_runtime(provider: Callable[[], Any]) -> None:
    """Install the facade-owned runtime provider.

    The provider is injected by the public route facade during application
    startup. Keeping the provider here, rather than importing the facade,
    makes the dependency direction one-way.
    """

    global _runtime_provider
    _runtime_provider = provider


def current_runtime() -> Any:
    """Return the currently configured route runtime."""

    if _runtime_provider is None:
        raise RuntimeError("billing route runtime has not been configured")
    return _runtime_provider()
