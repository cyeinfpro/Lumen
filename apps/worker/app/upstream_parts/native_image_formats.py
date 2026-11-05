"""Native edit controls and response format fidelity, without format fallback."""
from __future__ import annotations

import io

from PIL import Image
from lumen_core.image_resource_limits import validate_gif_request_budget, validate_gif_resource_budget
from lumen_core.svg_output import validate_static_svg

from ..provider_runtime.upstream_services import resolve_image_upstream_services
from .generated_payload import cleanup_owned_generated_payload, materialize_generated_payload
from .image_execution import ImageExecutionRequest

_NATIVE = frozenset({"svg", "gif"})
_MODELS = frozenset({"gpt-image-2.6-flare", "gpt-image-2.6-sunburst"})


def validate_native_edit(request: ImageExecutionRequest) -> None:
    if request.output_format == "svg":
        raise ValueError("Native SVG editing is unsupported; use SVG text generation or an explicitly separate vision-to-SVG workflow")
    if request.output_format not in _NATIVE:
        return
    if request.image_model not in _MODELS or request.n != 1:
        raise ValueError("SVG/GIF edit requires GPT Image 2.6 and n=1")
    if not request.images:
        raise ValueError("Native edit requires a static PNG, JPEG or WebP reference")
    if request.mask is not None or request.markup is not None:
        raise ValueError("SVG/GIF output currently cannot be combined with mask or markup")
    validate_gif_request_budget(request.size, request.frame_count)


def _validate_native_bytes(raw: bytes, output_format: str) -> dict | None:
    if output_format == "svg":
        validate_static_svg(raw)
        return None
    # Generated payloads retain the existing generation byte limit. Do not
    # accidentally apply the separate local GIF editor's 32 MiB upload limit.
    with Image.open(io.BytesIO(raw)) as image:
        count = getattr(image, "n_frames", 1)
        if image.format != "GIF" or count < 2:
            raise ValueError("Expected an animated GIF, not a static/raster substitute")
        validate_gif_resource_budget(image.width, image.height, count)
        loop = image.info.get("loop")
        durations = []
        for index in range(count):
            image.seek(index)
            durations.append(int(image.info.get("duration", 0)))
        return {"width": image.width, "height": image.height,
                "frame_count": count, "durations_ms": durations,
                "total_duration_ms": sum(durations), "loop": loop}


def validate_native_results(results, request: ImageExecutionRequest, *,
                            url: str, path: str, trace_id: str) -> dict | None:
    """Refuse ignored output_format after 2xx without permitting a paid retry."""
    if request.output_format not in _NATIVE:
        return
    actual = []
    try:
        if len(results) != 1:
            raise ValueError("Expected exactly one native image result")
        for payload, _ in results:
            raw = materialize_generated_payload(payload)
            metadata = _validate_native_bytes(raw, request.output_format)
            if request.output_format == "gif":
                actual.append(metadata)
    except (ValueError, OSError, TypeError, EOFError, SyntaxError, Image.DecompressionBombError) as exc:
        for payload, _ in results:
            cleanup_owned_generated_payload(payload)
        services = resolve_image_upstream_services(request.upstream_runtime)
        raise services.infrastructure.UpstreamError(
            f"Provider did not return a valid native {request.output_format} result; "
            "the accepted request was not retried or converted to another format",
            status_code=200,
            error_code=services.infrastructure.EC.DIRECT_IMAGE_RESULT_UNKNOWN.value,
            payload={
                "path": path, "method": "POST", "url": url, "x_trace_id": trace_id,
                "upstream_result_unknown": True, "response_received": True,
                "requested_output_format": request.output_format,
                "reason": "native_output_format_mismatch",
            },
        ) from exc


    if request.output_format == "gif":
        requested = {
            "size": request.size,
            "frame_count": request.frame_count if request.frame_count is not None else 9,
            "frame_duration_ms": request.frame_duration_ms if request.frame_duration_ms is not None else 200,
        }
        observed = actual[0]
        mismatches = []
        if request.size != "auto" and request.size != f"{observed['width']}x{observed['height']}":
            mismatches.append("size")
        if requested["frame_count"] != observed["frame_count"]:
            mismatches.append("frame_count")
        if any(duration != requested["frame_duration_ms"] for duration in observed["durations_ms"]):
            mismatches.append("frame_duration_ms")
        # A usable provider result is retained. Differences are evidence, never
        # an invitation to rerun a paid request or invent a frame-grid formula.
        return {"output_format": "gif", "result_count": len(actual),
                "requested": requested, "actual": actual, "mismatches": mismatches}
    return None
