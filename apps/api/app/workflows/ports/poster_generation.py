"""Typed side-effect ports for poster generation use cases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain.json_types import JsonMapping
from ..domain.workflow_contracts import PublishBundle


@dataclass(frozen=True, slots=True)
class PosterMasterTask:
    candidate_index: int
    style_summary: JsonMapping
    copy_analysis: JsonMapping
    intent: str
    prompt: str
    attachment_ids: tuple[str, ...]
    idempotency_key: str
    quality_mode: str
    size_mode: str
    size: str | None
    workflow_meta: JsonMapping


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
    workflow_meta: JsonMapping


@dataclass(frozen=True, slots=True)
class PosterTaskResult:
    bundle: PublishBundle
    generation_ids: tuple[str, ...]


class PosterGenerationPort(Protocol):
    async def submit_master(self, task: PosterMasterTask) -> PosterTaskResult: ...

    async def submit_render(self, task: PosterRenderTask) -> PosterTaskResult: ...
