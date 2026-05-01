from __future__ import annotations

from PIL import Image as PILImage
from PIL import ImageChops
from PIL import ImageDraw
from PIL import ImageFilter

# Speckles smaller than this fraction of the image are removed.
NOISE_AREA_FRACTION = 0.0005
HOLE_AREA_FRACTION = 0.0005
EDGE_FEATHER_RADIUS = 0.6
DECONTAM_EDGE_RADIUS = 1


def refine(rgba: PILImage.Image) -> PILImage.Image:
    if rgba.mode != "RGBA":
        rgba = rgba.convert("RGBA")
    width, height = rgba.size
    if width < 4 or height < 4:
        return rgba.copy()

    alpha = rgba.getchannel("A")
    cleaned = _remove_speckles(alpha, max(8, int(width * height * NOISE_AREA_FRACTION)))
    filled = _fill_small_holes(cleaned, max(8, int(width * height * HOLE_AREA_FRACTION)))
    cleaned.close()
    feathered = filled.filter(ImageFilter.GaussianBlur(EDGE_FEATHER_RADIUS))
    filled.close()

    rgb = rgba.convert("RGB")
    decontaminated = _decontaminate_edges(rgb, feathered)
    rgb.close()

    out = PILImage.merge("RGBA", (*decontaminated.split(), feathered))
    decontaminated.close()
    return out


def _remove_speckles(alpha: PILImage.Image, min_area: int) -> PILImage.Image:
    fg = alpha.point(lambda v: 255 if v >= 16 else 0)
    components = _label_components(fg, target_value=255)
    if components is None:
        return alpha.copy()
    labeled, counts = components
    keep_labels = {label for label, count in counts.items() if count >= min_area}
    if not keep_labels:
        labeled.close()
        return alpha.point(lambda _v: 0)
    if len(keep_labels) == len(counts):
        labeled.close()
        return alpha.copy()

    keep_lookup = [255 if i in keep_labels else 0 for i in range(256)]
    keep_mask = labeled.point(lambda v: keep_lookup[v] if 0 <= v < 256 else 0)
    labeled.close()
    out = PILImage.new("L", alpha.size, 0)
    out.paste(alpha, mask=keep_mask)
    keep_mask.close()
    return out


def _fill_small_holes(alpha: PILImage.Image, min_area: int) -> PILImage.Image:
    bg = alpha.point(lambda v: 255 if v < 16 else 0)
    components = _label_components(bg, target_value=255)
    bg.close()
    if components is None:
        return alpha.copy()
    labeled, counts = components

    if not counts:
        labeled.close()
        return alpha.copy()

    width, height = labeled.size
    boundary_labels: set[int] = set()
    pixels = labeled.load()
    for x in range(width):
        boundary_labels.add(int(pixels[x, 0]))
        boundary_labels.add(int(pixels[x, height - 1]))
    for y in range(height):
        boundary_labels.add(int(pixels[0, y]))
        boundary_labels.add(int(pixels[width - 1, y]))
    boundary_labels.discard(0)

    fill_labels = {
        label for label, count in counts.items()
        if label not in boundary_labels and count <= max(min_area, 1)
    }
    if not fill_labels:
        labeled.close()
        return alpha.copy()

    fill_lookup = [255 if i in fill_labels else 0 for i in range(256)]
    fill_mask = labeled.point(lambda v: fill_lookup[v] if 0 <= v < 256 else 0)
    labeled.close()
    out = alpha.copy()
    out.paste(255, mask=fill_mask)
    fill_mask.close()
    return out


def _label_components(
    binary: PILImage.Image, *, target_value: int
) -> tuple[PILImage.Image, dict[int, int]] | None:
    work = binary.copy()
    pixels = work.load()
    width, height = work.size
    label = 1
    for y in range(height):
        for x in range(width):
            if pixels[x, y] == target_value:
                if label > 250:
                    work.close()
                    return None
                ImageDraw.floodfill(work, (x, y), label)
                label += 1
    if label == 1:
        return work, {}

    data = work.tobytes()
    counts: dict[int, int] = {}
    for v in range(1, label):
        c = data.count(bytes([v]))
        if c:
            counts[v] = c
    return work, counts


def _decontaminate_edges(rgb: PILImage.Image, alpha: PILImage.Image) -> PILImage.Image:
    edge = alpha.filter(ImageFilter.GaussianBlur(DECONTAM_EDGE_RADIUS))
    edge_mask = ImageChops.subtract(alpha, edge)
    edge.close()
    bool_mask = edge_mask.point(lambda v: 255 if v >= 8 else 0)
    edge_mask.close()
    if bool_mask.getbbox() is None:
        bool_mask.close()
        return rgb.copy()

    blurred_rgb = rgb.filter(ImageFilter.GaussianBlur(1.2))
    out = rgb.copy()
    out.paste(blurred_rgb, mask=bool_mask)
    blurred_rgb.close()
    bool_mask.close()
    return out
