"""Explicit process runtime used by upstream application parts.

The runtime is installed by ``app.upstream`` after composition is complete.
Parts depend on this leaf module instead of importing the parent facade.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from .contracts import (
    ClockPort,
    ImageJobClientPort,
    ProgressPort,
    ProviderSelectorPort,
    ResultDownloadPort,
    RuntimeSettingsPort,
    SupplierTransportPort,
    UpstreamMetricsPort,
)


@dataclass(frozen=True)
class UpstreamRuntime:
    settings: RuntimeSettingsPort | Any
    providers: ProviderSelectorPort | Any
    supplier_transport: SupplierTransportPort | Any
    image_job_client: ImageJobClientPort | Any
    result_downloads: ResultDownloadPort | Any
    progress: ProgressPort | Any
    clock: ClockPort | Any
    metrics: UpstreamMetricsPort | Any


_PROCESS_RUNTIME: ContextVar[UpstreamRuntime | None] = ContextVar(
    "upstream_process_runtime",
    default=None,
)
_RUNTIME_OVERRIDE: ContextVar[UpstreamRuntime | None] = ContextVar(
    "upstream_runtime_override",
    default=None,
)


def install_upstream_runtime(runtime: UpstreamRuntime) -> None:
    _PROCESS_RUNTIME.set(runtime)


def get_upstream_runtime() -> UpstreamRuntime:
    runtime = _RUNTIME_OVERRIDE.get() or _PROCESS_RUNTIME.get()
    if runtime is None:
        raise RuntimeError("upstream runtime has not been composed")
    return runtime


def bind_upstream_runtime(runtime: UpstreamRuntime) -> Token[UpstreamRuntime | None]:
    return _RUNTIME_OVERRIDE.set(runtime)


def reset_upstream_runtime(token: Token[UpstreamRuntime | None]) -> None:
    _RUNTIME_OVERRIDE.reset(token)


@contextmanager
def use_upstream_runtime(runtime: UpstreamRuntime) -> Iterator[UpstreamRuntime]:
    token = bind_upstream_runtime(runtime)
    try:
        yield runtime
    finally:
        reset_upstream_runtime(token)


__all__ = [
    "UpstreamRuntime",
    "bind_upstream_runtime",
    "get_upstream_runtime",
    "install_upstream_runtime",
    "reset_upstream_runtime",
    "use_upstream_runtime",
]
