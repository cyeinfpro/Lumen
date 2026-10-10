"""Versioned public model contracts and task-derived Canvas snapshots."""

from __future__ import annotations

from typing import Any

from lumen_core.canvas import canonical_hash
from lumen_core.image_models import (
    DEFAULT_IMAGE_MODEL,
    IMAGE_MODELS,
    IMAGE_RENDER_QUALITIES,
    MAX_IMAGE_COUNT,
)


def image_capability(model: str | None) -> dict[str, Any]:
    resolved = model or DEFAULT_IMAGE_MODEL
    if resolved not in IMAGE_MODELS:
        raise ValueError("unsupported image model")
    qualities = sorted(
        IMAGE_RENDER_QUALITIES
        - ({"xhigh", "max"} if resolved == DEFAULT_IMAGE_MODEL else set())
    )
    capability = {
        "schema_version": 1,
        "kind": "image",
        "model": resolved,
        "input_types": ["text", "image", "mask"],
        "maximum_outputs": MAX_IMAGE_COUNT,
        "render_qualities": qualities,
    }
    return {**capability, "version": canonical_hash(capability)}


def task_model_snapshot(owner: Any, *, kind: str) -> dict[str, Any]:
    """Allowlist fields; upstream requests, keys and locators never escape."""
    fields = (
        "model",
        "action",
        "size_requested",
        "aspect_ratio",
        "duration_s",
        "resolution",
        "generate_audio",
        "provider_kind",
        "provider_name",
    )
    parameters = {
        field: value
        for field in fields
        if isinstance(value := getattr(owner, field, None), (str, int, bool))
    }
    if kind == "image":
        capability = image_capability(parameters.get("model"))
    else:
        # This is the admitted request contract, not a guessed provider catalog.
        capability = {
            "schema_version": 1,
            "kind": "video",
            "source": "admitted_task",
            "parameters": parameters,
        }
        capability["version"] = canonical_hash(capability)
    return {
        "schema_version": 1,
        "source": "durable_task",
        "model": parameters.get("model"),
        "parameters": parameters,
        "capability": capability,
    }
