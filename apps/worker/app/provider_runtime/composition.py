"""Construction helpers for independently testable upstream runtimes."""

from __future__ import annotations

from typing import Any

from .runtime import UpstreamRuntime


def build_upstream_runtime(
    *,
    settings: Any,
    providers: Any,
    supplier_transport: Any,
    image_job_client: Any,
    result_downloads: Any,
    progress: Any,
    clock: Any,
    metrics: Any,
) -> UpstreamRuntime:
    return UpstreamRuntime(
        settings=settings,
        providers=providers,
        supplier_transport=supplier_transport,
        image_job_client=image_job_client,
        result_downloads=result_downloads,
        progress=progress,
        clock=clock,
        metrics=metrics,
    )


__all__ = ["build_upstream_runtime"]
