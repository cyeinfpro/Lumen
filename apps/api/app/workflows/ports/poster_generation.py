"""Typed side-effect ports for poster generation use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class PosterMasterTask:
    candidate_index: int
    style_summary: Mapping[str, object]
    copy_analysis: Mapping[str, object]
    intent: str
    prompt: str
    attachment_ids: tuple[str, ...]
    idempotency_key: str
    quality_mode: str
    size_mode: str
    size: str | None
    workflow_meta: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PosterRenderTask:
    master_id: str
    aspect_ratio: str
    intent: str
    prompt: str
    attachment_ids: tuple[str, ...]
    idempotency_key: str
    quality_mode: str
    use_master_as_reference: bool
    workflow_meta: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PosterTaskResult:
    bundle: object
    generation_ids: tuple[str, ...]


class PosterGenerationPort(Protocol):
    async def submit_master(self, task: PosterMasterTask) -> PosterTaskResult: ...

    async def submit_render(self, task: PosterRenderTask) -> PosterTaskResult: ...
