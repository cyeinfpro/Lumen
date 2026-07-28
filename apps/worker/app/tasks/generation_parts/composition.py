"""Production builder for the explicit Generation runtime."""

from __future__ import annotations

from ...config import settings
from ...db import SessionLocal
from ...storage import storage
from ...storage_writes import StorageWriteCoordinator
from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from ...upstream_parts.upstream_impl import build_image_upstream_runtime
from .composition_ports import (
    DefaultGenerationBilling,
    DefaultGenerationCredentials,
    DefaultGenerationArtifacts,
    DefaultGenerationEvents,
    DefaultGenerationLease,
    DefaultGenerationProvider,
    DefaultGenerationQueue,
    DefaultGenerationStore,
    DefaultGenerationWorkflows,
)
from .runner import run_generation
from .runtime import GenerationRuntime, ImagePostprocessRuntime
from .services import RunGenerationDeps


def build_generation_runtime(
    *,
    storage_writes: StorageWriteCoordinator | None = None,
    image_upstream_runtime: ImageUpstreamRuntime | None = None,
) -> GenerationRuntime:
    upstream_runtime = image_upstream_runtime or build_image_upstream_runtime()
    postprocess_runtime = ImagePostprocessRuntime()
    deps = RunGenerationDeps(
        store=DefaultGenerationStore(SessionLocal),
        artifacts=DefaultGenerationArtifacts(storage, storage_writes),
        billing=DefaultGenerationBilling(),
        events=DefaultGenerationEvents(),
        provider=DefaultGenerationProvider(
            postprocess_runtime,
            storage,
            upstream_runtime,
        ),
        queue=DefaultGenerationQueue(settings),
        lease=DefaultGenerationLease(),
        credentials=DefaultGenerationCredentials(),
        workflows=DefaultGenerationWorkflows(),
    )
    return GenerationRuntime(
        deps=deps,
        runner=run_generation,
        image_upstream_runtime=upstream_runtime,
        postprocess_runtime=postprocess_runtime,
    )
