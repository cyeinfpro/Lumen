"""Public ARQ entrypoint for completion tasks."""

from __future__ import annotations

from typing import Any

from .completion_parts.contracts import CompletionCommand
from .completion_parts.runtime import CompletionRuntime


async def run_completion(ctx: dict[str, Any], task_id: str) -> None:
    runtime = ctx.get("completion_runtime")
    if not isinstance(runtime, CompletionRuntime):
        raise TypeError("ctx['completion_runtime'] must be CompletionRuntime")
    await runtime.run(CompletionCommand.from_arq(ctx, task_id))


__all__ = ["run_completion"]
