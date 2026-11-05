"""Native edit controls and response format fidelity, without format fallback."""
from __future__ import annotations

import io

from PIL import Image
from lumen_core.image_resource_limits import validate_gif_request_budget
from lumen_core.svg_output import validate_static_svg

from .. import image_artifacts
from ..provider_runtime.upstream_services import resolve_image_upstream_services
from .generated_payload import cleanup_owned_generated_payload, materialize_generated_payload
from .image_execution import ImageExecutionRequest

_NATIVE = frozenset({"svg", "gif"})
_MODELS = frozenset({"gpt-image-2.6-flare", "gpt-image-2.6-sunburst"})


def validate_native_edit(request: ImageExecutionRequest) -> None:
    if request.output_format not in _NATIVE:
        return
    if request.image_model not in _MODELS or request.n != 1:
        raise ValueError("SVG/GIF edit requires GPT Image 2.6 and n=1")
    if not request.images:
        raise ValueError("Native edit requires a static PNG, JPEG or WebP reference")
    if request.mask is not None or request.markup is not None:
        raise ValueError("SVG/GIF output currently cannot be combined with mask or markup")
    if request.output_format == "svg":
        # Explicit provider-dependent request. The public edit request enum does
        # not yet document SVG; preserve a real upstream rejection, never fall back.
        image_artifacts.require_svg_renderer()
    else:
        validate_gif_request_budget(request.size, request.frame_count)


def _validate_native_bytes(raw: bytes, output_format: str) -> None:
    if output_format == "svg":
        validate_static_svg(raw)
        return
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "GIF" or getattr(image, "n_frames", 1) < 2:
            raise ValueError("Expected an animated GIF, not a static/raster substitute")


def validate_native_results(results, request: ImageExecutionRequest, *,
                            url: str, path: str, trace_id: str) -> None:
    """Refuse ignored output_format after 2xx without permitting a paid retry."""
    if request.output_format not in _NATIVE:
        return
    try:
        if len(results) != 1:
            raise ValueError("Expected exactly one native image result")
        for payload, _ in results:
            _validate_native_bytes(materialize_generated_payload(payload), request.output_format)
    except (ValueError, OSError, TypeError) as exc:
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
