"""Resolve plan references from owned immutable executions and live assets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.canvas_models import CanvasNodeExecution, CanvasNodeSelection
from lumen_core.canvas_run_plan import OutputReference
from lumen_core.model_entities import Image, Video

from .errors import canvas_http


async def plan_output_references(
    db: AsyncSession,
    *,
    user_id: str,
    canvas_id: str,
    graph: dict[str, Any],
    reuse_outputs: dict[str, Any],
) -> tuple[
    dict[str, OutputReference], dict[str, OutputReference], dict[str, OutputReference]
]:
    nodes = {node["id"] for node in graph["nodes"]}
    selections = list(
        (
            await db.execute(
                select(CanvasNodeSelection).where(
                    CanvasNodeSelection.canvas_id == canvas_id,
                    CanvasNodeSelection.node_id.in_(nodes),
                    CanvasNodeSelection.execution_id.is_not(None),
                )
            )
        ).scalars()
    )
    choices: list[tuple[str, str, str, int, str]] = [
        ("selected", row.node_id, row.execution_id, int(row.output_index), row.node_id)
        for row in selections
    ]
    choices.extend(
        (
            "pinned",
            edge["source_node_id"],
            edge["pinned_execution_id"],
            int(edge["pinned_output_index"]),
            edge["id"],
        )
        for edge in graph["edges"]
        if edge["binding_mode"] == "pinned"
    )
    choices.extend(
        ("reuse", node_id, value.execution_id, value.output_index, node_id)
        for node_id, value in reuse_outputs.items()
    )
    execution_ids = {choice[2] for choice in choices}
    executions = (
        {
            row.id: row
            for row in (
                await db.execute(
                    select(CanvasNodeExecution).where(
                        CanvasNodeExecution.id.in_(execution_ids),
                        CanvasNodeExecution.user_id == user_id,
                        CanvasNodeExecution.canvas_id == canvas_id,
                        CanvasNodeExecution.status.in_(
                            ("succeeded", "partial_failed", "reused")
                        ),
                    )
                )
            ).scalars()
        }
        if execution_ids
        else {}
    )
    candidates = []
    for category, node_id, execution_id, index, key in choices:
        execution = executions.get(execution_id)
        output = None
        if execution is not None and execution.node_id == node_id:
            outputs = execution.outputs_jsonb or []
            output = outputs[index] if 0 <= index < len(outputs) else None
        if not isinstance(output, dict) or output.get("type") not in {"image", "video"}:
            if category == "selected":
                continue
            raise canvas_http(
                "canvas_input_unresolved",
                "plan output is unavailable",
                422,
                node_id=node_id,
            )
        candidates.append((category, node_id, execution_id, index, key, output))
    assets = {}
    for model, kind in ((Image, "image"), (Video, "video")):
        ids = {
            output.get(f"{kind}_id")
            for *_, output in candidates
            if output.get("type") == kind
        }
        if ids:
            assets.update(
                {
                    (kind, row.id): row
                    for row in (
                        await db.execute(
                            select(model).where(
                                model.id.in_(ids),
                                model.user_id == user_id,
                                model.deleted_at.is_(None),
                            )
                        )
                    ).scalars()
                }
            )
    results: dict[str, dict[str, OutputReference]] = {
        "selected": {},
        "pinned": {},
        "reuse": {},
    }
    for category, node_id, execution_id, index, key, output in candidates:
        kind = output["type"]
        asset = assets.get((kind, output.get(f"{kind}_id")))
        if asset is None or (output.get("sha256") and output["sha256"] != asset.sha256):
            if category == "selected":
                continue
            raise canvas_http(
                "canvas_input_unresolved",
                "plan asset is unavailable",
                422,
                node_id=node_id,
            )
        results[category][key] = OutputReference(
            node_id,
            execution_id,
            index,
            kind,
            asset.id,
            asset.sha256,
        )
    return results["selected"], results["pinned"], results["reuse"]
