"""Keep Canvas-owned media tasks on the Canvas retry path."""

from __future__ import annotations

from typing import Any

from .errors import canvas_http


def is_canvas_task(row: Any) -> bool:
    request = getattr(row, "upstream_request", None)
    if not isinstance(request, dict):
        return False
    execution_id = request.get("canvas_execution_id")
    return (
        request.get("source") == "canvas"
        and isinstance(execution_id, str)
        and bool(execution_id)
    )


def reject_canvas_retry(row: Any) -> None:
    if is_canvas_task(row):
        raise canvas_http(
            "canvas_retry_requires_canvas",
            "Canvas 任务请回画布重新运行节点",
            409,
        )


__all__ = ["is_canvas_task", "reject_canvas_retry"]
