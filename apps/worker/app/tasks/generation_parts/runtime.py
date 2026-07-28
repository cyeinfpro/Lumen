from __future__ import annotations

from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Protocol

from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from .services import RunGenerationDeps


class GenerationRunner(Protocol):
    async def __call__(
        self,
        ctx: dict[str, object],
        task_id: str,
        services: RunGenerationDeps,
    ) -> None: ...


@dataclass(slots=True)
class ImagePostprocessRuntime:
    executor: Executor | None = None

    def reset(self) -> None:
        executor = self.executor
        self.executor = None
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)


@dataclass(frozen=True, slots=True)
class GenerationRuntime:
    deps: RunGenerationDeps
    runner: GenerationRunner
    image_upstream_runtime: ImageUpstreamRuntime | None = None
    postprocess_runtime: ImagePostprocessRuntime = field(
        default_factory=ImagePostprocessRuntime
    )

    async def run(self, ctx: dict[str, object], task_id: str) -> None:
        await self.runner(ctx, task_id, self.deps)

    async def shutdown(self) -> None:
        self.postprocess_runtime.reset()
