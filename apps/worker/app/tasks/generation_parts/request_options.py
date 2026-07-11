from __future__ import annotations

from typing import Any

from ._facade import GenerationFacade

_g = GenerationFacade()
bind_generation_facade = _g.bind

IMAGE_RENDER_QUALITY_VALUES = {"low", "medium", "high", "auto"}
IMAGE_OUTPUT_FORMAT_VALUES = {"png", "jpeg", "webp"}
IMAGE_BACKGROUND_VALUES = {"auto", "opaque", "transparent"}
IMAGE_MODERATION_VALUES = {"auto", "low"}


def parse_size_string(size: str) -> tuple[int, int]:
    if not isinstance(size, str) or "x" not in size:
        raise ValueError(f"invalid resolved size: {size!r}")
    raw_width, raw_height = size.split("x", 1)
    if not raw_width.isdigit() or not raw_height.isdigit():
        raise ValueError(f"invalid resolved size: {size!r}")
    return int(raw_width), int(raw_height)


def validate_resolved_size(
    size: str,
    aspect_ratio: str,
    *,
    validate_aspect_ratio: bool = True,
    max_ratio_deviation: float = 0.02,
) -> tuple[int, int]:
    width, height = _g._parse_size_string(size)
    _g.validate_explicit_size(width, height)
    if validate_aspect_ratio and isinstance(aspect_ratio, str) and ":" in aspect_ratio:
        raw_ratio_width, raw_ratio_height = aspect_ratio.split(":", 1)
        if raw_ratio_width.isdigit() and raw_ratio_height.isdigit():
            ratio_width = int(raw_ratio_width)
            ratio_height = int(raw_ratio_height)
            if ratio_width > 0 and ratio_height > 0:
                target = ratio_width / ratio_height
                actual = width / height
                deviation = abs(actual - target) / target
                if deviation > max_ratio_deviation:
                    raise ValueError(
                        "resolved size aspect ratio drift too large: "
                        f"size={size} requested={aspect_ratio} "
                        f"deviation={deviation:.3%}"
                    )
    return width, height


def parse_aspect_ratio_value(aspect_ratio: str) -> tuple[int, int] | None:
    if not isinstance(aspect_ratio, str) or ":" not in aspect_ratio:
        return None
    raw_ratio_width, raw_ratio_height = aspect_ratio.split(":", 1)
    if not raw_ratio_width.isdigit() or not raw_ratio_height.isdigit():
        return None
    ratio_width = int(raw_ratio_width)
    ratio_height = int(raw_ratio_height)
    if ratio_width <= 0 or ratio_height <= 0:
        return None
    return ratio_width, ratio_height


def aspect_ratio_prompt_constraint(aspect_ratio: str) -> str:
    parsed = _g._parse_aspect_ratio_value(aspect_ratio)
    if parsed is None:
        return ""
    shape_hint = " This means a square canvas." if parsed[0] == parsed[1] else ""
    return (
        "\n\nAspect ratio constraint: the final image canvas must be a strict "
        f"{aspect_ratio} ratio.{shape_hint} Do not reinterpret this as a poster, "
        "portrait, landscape, social cover, or any other ratio."
    )


def prompt_with_aspect_ratio_constraint(prompt: str, aspect_ratio: str) -> str:
    constraint = _g._aspect_ratio_prompt_constraint(aspect_ratio)
    if not constraint:
        return prompt
    normalized_prompt = prompt.rstrip()
    if constraint.strip() in normalized_prompt:
        return normalized_prompt
    return f"{normalized_prompt}{constraint}"


def primary_input_image_id_valid(
    primary_input_image_id: str | None,
    input_image_ids: list[str],
) -> bool:
    return primary_input_image_id is None or primary_input_image_id in input_image_ids


def request_option(
    upstream_request: dict[str, Any],
    key: str,
    allowed: set[str],
    default: str,
) -> str:
    value = upstream_request.get(key)
    return value if isinstance(value, str) and value in allowed else default


def request_compression(upstream_request: dict[str, Any]) -> int | None:
    value = upstream_request.get("output_compression")
    if value is None:
        return None
    try:
        compression = int(value)
    except (TypeError, ValueError):
        return None
    if 0 <= compression <= 100:
        return compression
    return None


def request_render_quality(
    upstream_request: dict[str, Any],
    *,
    size: str,
) -> str:
    _ = size
    quality = _g._request_option(
        upstream_request,
        "render_quality",
        _g._IMAGE_RENDER_QUALITY_VALUES,
        "auto",
    )
    if quality in {"low", "medium", "high"}:
        return quality
    return "medium"


def request_responses_model(upstream_request: dict[str, Any]) -> str:
    value = upstream_request.get("responses_model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    try:
        fast = _g.parse_provider_bool(
            upstream_request.get("fast"),
            default=False,
        )
    except ValueError:
        fast = False
    if fast:
        return _g.DEFAULT_IMAGE_RESPONSES_MODEL_FAST
    return _g.DEFAULT_IMAGE_RESPONSES_MODEL


def image_request_options(
    upstream_request: dict[str, Any] | None,
    *,
    size: str,
) -> dict[str, Any]:
    request = upstream_request if isinstance(upstream_request, dict) else {}
    try:
        fast_mode = _g.parse_provider_bool(request.get("fast"), default=False)
    except ValueError:
        fast_mode = False
    render_quality = _g._request_render_quality(request, size=size)
    output_format = _g._request_option(
        request,
        "output_format",
        _g._IMAGE_OUTPUT_FORMAT_VALUES,
        "jpeg",
    )
    background = _g._request_option(
        request,
        "background",
        _g._IMAGE_BACKGROUND_VALUES,
        "auto",
    )
    if background == "transparent":
        output_format = "png"
    options: dict[str, Any] = {
        "fast": fast_mode,
        "responses_model": _g._request_responses_model(request),
        "render_quality": render_quality,
        "output_format": output_format,
        "background": background,
        "moderation": _g._request_option(
            request,
            "moderation",
            _g._IMAGE_MODERATION_VALUES,
            "low",
        ),
    }
    if output_format in {"jpeg", "webp"}:
        options["output_compression"] = _g._request_compression(request)
        if options["output_compression"] is None:
            options["output_compression"] = 100
    options["n"] = _g._image_requested_count(request)
    return options


def image_requested_count(upstream_request: dict[str, Any] | None) -> int:
    request = upstream_request if isinstance(upstream_request, dict) else {}
    raw = request.get("n")
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(10, value))
