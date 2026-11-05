"""Strict static SVG validation before generated originals are rendered or served."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

_NS = "http://www.w3.org/2000/svg"
_TAGS = frozenset("""svg g defs path rect circle ellipse line polyline polygon
text tspan title desc linearGradient radialGradient stop clipPath mask symbol""".split())
_ATTRS = frozenset("""id x y x1 y1 x2 y2 cx cy r rx ry width height viewBox
preserveAspectRatio d points transform fill fill-opacity fill-rule stroke
stroke-width stroke-opacity stroke-linecap stroke-linejoin stroke-miterlimit
stroke-dasharray stroke-dashoffset opacity clip-path clip-rule mask
gradientUnits gradientTransform spreadMethod offset stop-color stop-opacity
fx fy fr font-family font-size font-weight font-style text-anchor
dominant-baseline alignment-baseline dx dy rotate textLength lengthAdjust
vector-effect color display visibility href""".split())
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_LOCAL_URL = re.compile(r"url\(\s*['\"]?#[A-Za-z_][\w.:-]*['\"]?\s*\)", re.I)


def looks_like_svg(data: bytes) -> bool:
    # XML-looking bytes require strict SVG parsing, including comments/prefixed roots.
    # This only selects the validator; it never establishes that the payload is safe SVG.
    return data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")


def _parse_static_svg(data: bytes) -> ET.Element:
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("SVG exceeds 10 MiB safety limit")
    source = data.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", source, re.I):
        raise ValueError("SVG document/entity declarations are forbidden")
    stripped = re.sub(r"^\s*<\?xml\s[^?]*\?>", "", source, count=1)
    if "<?" in stripped:
        raise ValueError("SVG processing instructions are forbidden")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as exc:
        raise ValueError("Invalid SVG XML") from exc
    if root.tag not in {"svg", f"{{{_NS}}}svg"}:
        raise ValueError("Expected an SVG root")
    return root


def _validate_svg_attribute(key: str, value: str) -> None:
    if key == "{http://www.w3.org/1999/xlink}href":
        key = "href"
    if key not in _ATTRS:
        raise ValueError(f"Unsupported SVG attribute: {key}")
    if key == "href" and not re.fullmatch(r"#[A-Za-z_][\w.:-]*", value):
        raise ValueError("SVG references must be local fragments")
    residue = _LOCAL_URL.sub("", value)
    if re.search(r"url\s*\(", residue, re.I) or "\\" in value:
        raise ValueError("External or escaped SVG resource is forbidden")


def _validate_svg_tree(root: ET.Element) -> None:
    stack = [(root, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 64 or count > 100_000:
            raise ValueError("SVG structure exceeds safety limit")
        tag = node.tag
        if tag.startswith(f"{{{_NS}}}"):
            tag = tag[len(_NS) + 2:]
        if tag not in _TAGS:
            raise ValueError(f"Unsupported SVG element: {tag}")
        for key, value in node.attrib.items():
            _validate_svg_attribute(key, value)
        stack.extend((child, depth + 1) for child in node)


def _svg_viewbox(root: ET.Element) -> list[float] | None:
    value = root.get("viewBox")
    if value is None:
        return None
    try:
        values = [float(v) for v in value.replace(",", " ").split()]
    except ValueError as exc:
        raise ValueError("SVG viewBox must contain four finite numbers") from exc
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError("SVG viewBox must contain four finite numbers")
    if values[2] <= 0 or values[3] <= 0:
        raise ValueError("SVG viewBox must have positive width and height")
    return values


def _svg_dimensions(root: ET.Element) -> tuple[int, int]:
    if (root.get("width") is None) != (root.get("height") is None):
        raise ValueError("SVG must specify both numeric/px dimensions or omit both for viewBox")
    fallback = _svg_viewbox(root)
    dimensions = []
    for index, name in enumerate(("width", "height")):
        value = root.get(name)
        if value is not None and re.fullmatch(_NUMBER + r"(?:px)?", value):
            dimension = float(value.removesuffix("px"))
        elif value is None and fallback is not None:
            dimension = fallback[index + 2]
        else:
            raise ValueError("SVG requires numeric/px dimensions; only omitted dimensions may use viewBox")
        if not math.isfinite(dimension) or not 0 < dimension <= 10_000:
            raise ValueError("SVG dimensions exceed safety limit")
        dimensions.append(math.ceil(dimension))
    if dimensions[0] * dimensions[1] > 64_000_000:
        raise ValueError("SVG pixel count exceeds safety limit")
    return dimensions[0], dimensions[1]



def _validated_root_and_size(data: bytes) -> tuple[ET.Element, tuple[int, int]]:
    root = _parse_static_svg(data)
    _validate_svg_tree(root)
    return root, _svg_dimensions(root)


def validate_static_svg(data: bytes) -> tuple[int, int]:
    return _validated_root_and_size(data)[1]


def validated_svg_render_bytes(data: bytes) -> bytes:
    """Fix the renderer-only pixel viewport; preserve original download bytes."""
    root, (width, height) = _validated_root_and_size(data)
    root.set("width", str(width))
    root.set("height", str(height))
    return ET.tostring(root, encoding="utf-8")
