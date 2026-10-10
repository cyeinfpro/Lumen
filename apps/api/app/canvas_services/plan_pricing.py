"""Read-only admission estimates; task billing remains the sole wallet writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lumen_core.billing_estimates import estimate_image_cost_for_tier
from lumen_core.billing_values import BillingError
from lumen_core.canvas import canonical_hash
from lumen_core.canvas_capabilities import image_capability
from lumen_core.canvas_schemas import IMAGE_EXECUTABLE_NODE_TYPES, NODE_OUTPUT_PORTS
from lumen_core.video_billing import (
    VideoBillingError,
    estimate_video_cost,
    video_billing_model,
    video_pricing_variant,
)

from ..services.message_submission_billing import billing_enabled
from ..services.video.options import get_video_options, require_video_create_ready
from ..services.video.submission_preflight import validate_provider_submission
from ..services.video.reference_media import (
    reference_public_base_url,
    provider_requires_public_media,
    provider_prefers_public_media_url,
)
from .plan_video_preflight import PreviewMedia, video_preview_inputs
from ..task_billing import apply_rate_multiplier_micro, rate_multiplier_x10000
from .errors import canvas_http
from .execution_image_inputs import image_params


@dataclass(frozen=True)
class PlanQuote:
    estimated_cost_micro: int
    model: str
    capability_version: str


@dataclass(frozen=True)
class VideoQuoteParameters:
    model: str
    action: str
    resolution: str
    duration_s: int
    aspect_ratio: str
    reference_media: tuple[PreviewMedia, ...] = ()
    input_image_id: str | None = None


def reference_kinds(graph: dict, node_id: str) -> list[dict[str, str]]:
    nodes = {node["id"]: node for node in graph["nodes"]}
    kinds = []
    for edge in graph["edges"]:
        if edge["target_node_id"] != node_id or edge["target_handle"] not in {
            "reference_images",
            "reference_videos",
        }:
            continue
        source = nodes[edge["source_node_id"]]
        kind = NODE_OUTPUT_PORTS[source["type"]][edge["source_handle"]].data_type
        kinds.append({"kind": kind})
    return kinds


async def cached_plan_quote(db, *, user, graph, node, cache, quote_fn):
    # Local to one preview/user only; dispatch always obtains a fresh quote.
    if node["type"].startswith("video"):
        # Reference identity/duration varies even when node configuration matches.
        return await quote_fn(db, user=user, graph=graph, node=node)
    media = []
    key = canonical_hash(
        {"type": node["type"], "config": node["config"], "media": media}
    )
    if key not in cache:
        cache[key] = await quote_fn(db, user=user, graph=graph, node=node)
    return cache[key]


async def quote_plan_node(db, *, user, graph: dict, node: dict) -> PlanQuote:
    try:
        return await calculate_plan_quote(db, user=user, graph=graph, node=node)
    except (BillingError, VideoBillingError) as exc:
        raise canvas_http(
            "canvas_plan_pricing_unavailable",
            "configured pricing is unavailable for this plan",
            422,
        ) from exc


async def calculate_plan_quote(db, *, user, graph: dict, node: dict) -> PlanQuote:
    if getattr(user, "account_mode", "wallet") != "wallet" or not await billing_enabled(
        db
    ):
        raise canvas_http(
            "canvas_plan_pricing_unavailable",
            "batch runs require configured wallet pricing",
            422,
        )
    config = node["config"]
    if node["type"] in IMAGE_EXECUTABLE_NODE_TYPES:
        params = image_params(config)
        unit_cost, _tier = await estimate_image_cost_for_tier(
            db, tier=params.quality, n=1
        )
        # Every image is billed separately, so round each unit before multiplying.
        cost = (
            apply_rate_multiplier_micro(unit_cost, rate_multiplier_x10000(user))
            * params.count
        )
        capability = image_capability(params.model)
        return PlanQuote(cost, capability["model"], capability["version"])
    return await quote_video_node(db, user=user, graph=graph, node=node)


async def quote_video_node(db, *, user, graph: dict, node: dict) -> PlanQuote:
    config = node["config"]
    model = config.get("model")
    if not model:
        raise canvas_http(
            "canvas_plan_model_required",
            "choose a video model before previewing a batch run",
            422,
            node_id=node["id"],
        )
    media, markers, first_frame = await video_preview_inputs(
        db, user=user, graph=graph, node=node
    )
    parameters = VideoQuoteParameters(
        model=model,
        action=config.get("action") or config.get("mode") or "t2v",
        resolution=config.get("resolution") or "720p",
        duration_s=int(config.get("duration_s", 5)),
        aspect_ratio=config.get("aspect_ratio") or "16:9",
        reference_media=markers,
        input_image_id=first_frame,
    )
    provider, estimates = await require_video_create_ready(db, parameters)
    upstream_model = provider.upstream_model_for(model, parameters.action)
    options = await get_video_options(user, db)
    option = next((item for item in options.models if item.model == model), None)
    if option is None or not options.enabled:
        raise canvas_http(
            "canvas_plan_model_unavailable", "video model is unavailable", 422
        )
    validate_provider_submission(provider, parameters, media)
    await reference_public_base_url(
        None,
        db,
        parameters,
        None,
        requires_public_media=provider_requires_public_media(provider),
        prefers_public_media_url=provider_prefers_public_media_url(provider),
    )
    cost = await estimate_video_cost(
        db,
        model=video_billing_model(model, upstream_model),
        action=parameters.action,
        resolution=parameters.resolution,
        duration_s=parameters.duration_s,
        generate_audio=bool(config.get("generate_audio", False)),
        estimates=estimates,
        pricing_variant=video_pricing_variant(
            parameters.action, media, resolution=parameters.resolution
        ),
    )
    capability: dict[str, Any] = {
        "model": option.model_dump(mode="json"),
        "provider_kind": provider.kind,
        "upstream_model": upstream_model,
    }
    return PlanQuote(int(cost.hold_micro), model, canonical_hash(capability))
