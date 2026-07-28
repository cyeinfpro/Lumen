"""Public typed runtime for completion tasks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from .contracts import CompletionCommand, CompletionResult, CompletionServices


class CompletionRunner(Protocol):
    async def __call__(self, command: CompletionCommand) -> CompletionResult: ...


@dataclass(frozen=True, slots=True)
class CompletionRuntime:
    services: CompletionServices
    runner: CompletionRunner
    image_upstream_runtime: ImageUpstreamRuntime

    async def run(self, command: CompletionCommand) -> CompletionResult:
        return await self.runner(command)


__all__ = ["CompletionRuntime"]
