"""Explicit builder entrypoint for Generation task runtimes."""

from __future__ import annotations

from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from ...storage_writes import StorageWriteCoordinator
from .composition import build_generation_runtime as _compose_generation_runtime
from .runtime import GenerationRuntime


def build_generation_runtime(
    *,
    storage_writes: StorageWriteCoordinator | None = None,
    image_upstream_runtime: ImageUpstreamRuntime | None = None,
) -> GenerationRuntime:
    return _compose_generation_runtime(
        storage_writes=storage_writes,
        image_upstream_runtime=image_upstream_runtime,
    )


__all__ = ["build_generation_runtime"]
