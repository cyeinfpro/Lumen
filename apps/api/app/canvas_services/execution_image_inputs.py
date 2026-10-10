"""Validated Canvas image parameters and input cardinality."""

from typing import Any
from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS
from lumen_core.schema_models import ImageParamsIn
from .errors import canvas_http
from .graph_resolution import ResolvedNode


def image_params(config: dict[str, Any]) -> ImageParamsIn:
    try:
        quality = str(config.get("quality") or "").lower()
        size = str(config.get("size") or "").lower()
        resolution = quality if quality in {"1k", "2k", "4k"} else size
        render_quality = str(config.get("render_quality") or "").lower()
        if render_quality not in {"auto", "low", "medium", "high", "xhigh", "max"}:
            render_quality = "medium" if quality == "standard" else "high"
        return ImageParamsIn.model_validate(
            {
                "model": config.get("model") or "gpt-image-2",
                "aspect_ratio": config.get("aspect_ratio") or "1:1",
                "size_mode": config.get("size_mode") or "auto",
                "fixed_size": config.get("fixed_size"),
                "count": int(config.get("count") or 1),
                "quality": (resolution if resolution in {"1k", "2k", "4k"} else "1k"),
                "render_quality": render_quality,
                "output_format": config.get("output_format") or "webp",
                "output_compression": config.get("output_compression"),
                "background": config.get("background") or "auto",
                "moderation": config.get("moderation") or "low",
            }
        )
    except (TypeError, ValueError) as exc:
        raise canvas_http(
            "canvas_image_config_invalid",
            "Canvas image node configuration is invalid",
            422,
            reason=str(exc),
        ) from exc


def _require_single_image(
    resolved: ResolvedNode,
    *,
    handle: str,
    node_type: str,
) -> dict[str, Any]:
    values = resolved.images_by_handle.get(handle, [])
    if len(values) != 1:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "Canvas image input requires exactly one asset",
            422,
            node_type=node_type,
            target_handle=handle,
            actual=len(values),
        )
    return values[0]


def image_task_inputs(
    *,
    node_type: str,
    resolved: ResolvedNode,
) -> tuple[list[str], str | None]:
    attachment_ids: list[str]
    mask_image_id: str | None
    if node_type == "image_generate":
        references = resolved.images_by_handle.get("references", [])
        masks = resolved.images_by_handle.get("mask", [])
        if len(masks) > 1:
            raise canvas_http(
                "canvas_mask_invalid",
                "image generation accepts at most one mask",
                422,
            )
        if masks and len(references) != 1:
            raise canvas_http(
                "canvas_mask_invalid",
                "mask requires exactly one reference image",
                422,
            )
        attachment_ids = [item["image_id"] for item in references]
        mask_image_id = masks[0]["image_id"] if masks else None
    else:
        source = _require_single_image(
            resolved,
            handle="source",
            node_type=node_type,
        )
        if node_type == "image_edit":
            references = resolved.images_by_handle.get("references", [])
            attachment_ids = [
                source["image_id"],
                *(item["image_id"] for item in references),
            ]
            mask_image_id = None
        elif node_type == "image_inpaint":
            mask = _require_single_image(
                resolved,
                handle="mask",
                node_type=node_type,
            )
            attachment_ids = [source["image_id"]]
            mask_image_id = mask["image_id"]
        elif node_type == "image_upscale":
            attachment_ids = [source["image_id"]]
            mask_image_id = None
        else:
            raise canvas_http(
                "canvas_node_not_executable",
                "node type cannot be executed as an image task",
                422,
                node_type=node_type,
            )
    if len(attachment_ids) > MAX_MESSAGE_ATTACHMENTS:
        raise canvas_http(
            "canvas_input_cardinality_invalid",
            "Canvas image task exceeds the attachment limit",
            422,
            maximum=MAX_MESSAGE_ATTACHMENTS,
            actual=len(attachment_ids),
        )
    return attachment_ids, mask_image_id
