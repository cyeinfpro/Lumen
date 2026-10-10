"""Owner-scoped prepared-asset gate for an entire plan before admission."""

from sqlalchemy import select
from lumen_core.model_entities import Image, Video
from .asset_descriptors import asset_descriptor
from .errors import canvas_http


async def validate_plan_assets(db, *, user_id, graph, plan):
    nodes = {node["id"]: node for node in graph["nodes"]}
    active = {step.node_id for step in plan.steps if step.reuse is None}
    needed = {}
    for edge in graph["edges"]:
        if edge["target_node_id"] not in active:
            continue
        source = nodes[edge["source_node_id"]]
        kind = {
            "image_asset": "image",
            "mask_asset": "image",
            "video_asset": "video",
        }.get(source["type"])
        if kind:
            needed[(kind, source["config"].get(kind + "_id", ""))] = None
    references = [reference for _, reference in plan.bindings]
    references.extend(step.reuse for step in plan.steps if step.reuse is not None)
    for reference in references:
        needed[(reference.asset_kind, reference.asset_id)] = reference.source_sha256
    found = {}
    for kind, model in (("image", Image), ("video", Video)):
        identifiers = {
            identifier for (asset_kind, identifier) in needed if asset_kind == kind
        }
        if identifiers:
            rows = (
                await db.execute(
                    select(model).where(
                        model.id.in_(identifiers),
                        model.user_id == user_id,
                        model.deleted_at.is_(None),
                    )
                )
            ).scalars()
            found.update({(kind, row.id): asset_descriptor(row) for row in rows})
    for identity, source_hash in needed.items():
        asset = found.get(identity)
        if (
            asset is None
            or asset["preparation_state"] != "ready"
            or (source_hash is not None and asset["source_sha256"] != source_hash)
        ):
            raise canvas_http(
                "canvas_plan_asset_not_ready",
                "a required asset is unavailable or still preparing",
                422,
                kind=identity[0],
                asset_id=identity[1],
            )
