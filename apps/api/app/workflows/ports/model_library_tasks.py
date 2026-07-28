"""Typed task-dispatch port for standalone model-library generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ModelLibraryGenerationTask:
    task_index: int
    gender: str
    candidate_index: int
    intent: str
    prompt: str
    attachment_ids: tuple[str, ...]
    idempotency_key: str
    workflow_meta: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelLibraryTaskResult:
    bundle: object
    generation_ids: tuple[str, ...]


class ModelLibraryTaskPort(Protocol):
    async def submit(
        self,
        task: ModelLibraryGenerationTask,
    ) -> ModelLibraryTaskResult: ...
