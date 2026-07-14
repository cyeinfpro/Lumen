"""HTTP request models for the Canvas API."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from lumen_core.canvas_schemas import MAX_CANVAS_GRAPH_BYTES


MAX_CANVAS_MUTATION_JSON_BYTES = MAX_CANVAS_GRAPH_BYTES + 1024 * 1024
MAX_CANVAS_MUTATION_JSON_DEPTH = 32


def _json_depth_exceeds(value: Any, maximum: int) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    seen: set[int] = set()
    while stack:
        current, depth = stack.pop()
        if depth > maximum:
            return True
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            stack.extend((item, depth + 1) for item in current)
    return False


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

    @field_validator("operations")
    @classmethod
    def validate_operation_payloads(
        cls,
        value: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        try:
            payload_bytes = len(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (RecursionError, TypeError, ValueError) as exc:
            raise ValueError("operations must contain valid JSON values") from exc
        if payload_bytes > MAX_CANVAS_MUTATION_JSON_BYTES:
            raise ValueError("operations exceed the Canvas mutation payload limit")
        if _json_depth_exceeds(value, MAX_CANVAS_MUTATION_JSON_DEPTH):
            raise ValueError("operations exceed the Canvas JSON nesting limit")
        return value


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
