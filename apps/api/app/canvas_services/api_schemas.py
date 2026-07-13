"""HTTP request models for the Canvas API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def empty_graph() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "nodes": [],
        "edges": [],
        "frames": [],
        "settings": {
            "snap_to_grid": False,
            "grid_size": 16,
        },
    }


def _required_trimmed(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("value must not be blank")
    return normalized


class CanvasCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="未命名画布", min_length=1, max_length=255)
    description: str = Field(default="", max_length=10_000)
    graph: dict[str, Any] = Field(default_factory=empty_graph)
    template: str | None = Field(default=None, max_length=64)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _required_trimmed(value)


class CanvasPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return _required_trimmed(value) if value is not None else None


class CanvasDuplicateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return _required_trimmed(value) if value is not None else None


class CanvasMutationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=1)
    client_id: str = Field(min_length=1, max_length=64)
    mutation_id: str = Field(min_length=1, max_length=96)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=500)


class CanvasVersionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _required_trimmed(value)


class CanvasExecuteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=96)
    auto_select_on_success: bool = True


class CanvasSelectOutputIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_index: int = Field(ge=0)
    selection_revision: int = Field(default=0, ge=0)
