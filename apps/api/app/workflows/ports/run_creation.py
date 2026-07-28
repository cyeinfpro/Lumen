"""Typed persistence boundary for creating workflow runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


WorkflowQualityMode = Literal["standard", "premium"]
PosterAspect = Literal["1:1", "9:16", "16:9", "3:4", "4:3", "2:3", "3:2", "4:5"]


@dataclass(frozen=True, slots=True)
class PosterBrandAssets:
    logo_image_id: str | None
    product_image_id: str | None
    primary_color: str | None
    font_family: str | None


@dataclass(frozen=True, slots=True)
class CreateApparelRunCommand:
    user_id: str
    product_image_ids: tuple[str, ...]
    user_prompt: str
    quality_mode: WorkflowQualityMode
    title: str | None


@dataclass(frozen=True, slots=True)
class CreatePosterRunCommand:
    user_id: str
    conversation_id: str | None
    copy_text: str
    style_id: str
    target_aspects: tuple[PosterAspect, ...]
    brand_assets: PosterBrandAssets
    quality_mode: WorkflowQualityMode
    title: str | None


@dataclass(frozen=True, slots=True)
class WorkflowRunCreated:
    workflow_run_id: str
    status: str
    current_step: str


class WorkflowRunCreationPort(Protocol):
    async def create_apparel(
        self,
        command: CreateApparelRunCommand,
    ) -> WorkflowRunCreated: ...

    async def create_poster(
        self,
        command: CreatePosterRunCommand,
    ) -> WorkflowRunCreated: ...


__all__ = [
    "CreateApparelRunCommand",
    "CreatePosterRunCommand",
    "PosterAspect",
    "PosterBrandAssets",
    "WorkflowQualityMode",
    "WorkflowRunCreated",
    "WorkflowRunCreationPort",
]
