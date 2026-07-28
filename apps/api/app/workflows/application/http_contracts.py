"""Transport-neutral request models used by workflow application actions."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkflowAssetsAddIn(BaseModel):
    image_ids: list[str] = Field(min_length=1, max_length=64)
    asset_type: str = Field(default="project_asset", min_length=1, max_length=64)
    source_step_key: str | None = Field(default=None, max_length=64)
    label: str | None = Field(default=None, max_length=120)


__all__ = ["WorkflowAssetsAddIn"]
