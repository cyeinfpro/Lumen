"""Public ARQ entrypoint for image generation."""

from __future__ import annotations

from typing import Any

from .generation_parts.default_runtime import DEFAULT_GENERATION_RUNTIME
from .generation_parts.runtime import GenerationRuntime


async def run_generation(ctx: dict[str, Any], task_id: str) -> None:
    runtime = ctx.get("generation_runtime", DEFAULT_GENERATION_RUNTIME)
    if not isinstance(runtime, GenerationRuntime):
        raise TypeError("ctx['generation_runtime'] must be GenerationRuntime")
    await runtime.run(ctx, task_id)


__all__ = ["run_generation"]
