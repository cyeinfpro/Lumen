from __future__ import annotations

from PIL import Image as PILImage
from PIL import ImageDraw

from .types import TransparentQcReport

# Thresholds tuned for first-launch leniency (see plan 2026-04-28).
# Stricter values are intentional follow-ups, not initial rollout.
BORDER_ALPHA_MAX_THRESHOLD = 16
MIN_BBOX_MARGIN_RATIO = 0.01
MIN_ALPHA_COVERAGE = 0.02
MAX_ALPHA_COVERAGE = 0.95
MIN_LARGEST_COMPONENT_RATIO = 0.60
COMPONENT_ANALYSIS_MAX_SIDE = 256


def evaluate(rgba: PILImage.Image) -> TransparentQcReport:
    if rgba.mode != "RGBA":
        return _fail(
            ["alpha_missing"],
            foreground_bbox=None,
            alpha_coverage=1.0,
            border_alpha_max=255,
            largest_component_ratio=None,
        )

    alpha = rgba.getchannel("A")
    width, height = alpha.size
    if width < 4 or height < 4:
        return _fail(
            ["image_too_small"],
            foreground_bbox=None,
            alpha_coverage=0.0,
            border_alpha_max=0,
            largest_component_ratio=None,
        )

    alpha_min, alpha_max = alpha.getextrema()
    if alpha_max == 0:
        return _fail(
            ["alpha_all_transparent"],
            foreground_bbox=None,
            alpha_coverage=0.0,
            border_alpha_max=0,
            largest_component_ratio=None,
        )
    if alpha_min == 255:
        return _fail(
            ["alpha_all_opaque"],
            foreground_bbox=(0, 0, width, height),
            alpha_coverage=1.0,
            border_alpha_max=255,
            largest_component_ratio=1.0,
        )

    border_alpha_max = _border_alpha_max(alpha)
    fg_mask = alpha.point(lambda v: 255 if v >= 16 else 0)
    fg_count = fg_mask.tobytes().count(b"\xff")
    total = width * height
    coverage = fg_count / total if total else 0.0
    bbox = fg_mask.getbbox()
    largest_ratio = _largest_component_ratio(fg_mask)
    fg_mask.close()

    failure_reasons: list[str] = []
    warnings: list[str] = []

    if border_alpha_max > BORDER_ALPHA_MAX_THRESHOLD:
        failure_reasons.append(f"border_alpha_too_high:{border_alpha_max}")

    if bbox is None:
        failure_reasons.append("no_foreground")
    else:
        margin_px = max(1, int(round(min(width, height) * MIN_BBOX_MARGIN_RATIO)))
        x0, y0, x1, y1 = bbox
        if x0 < margin_px or y0 < margin_px or (width - x1) < margin_px or (height - y1) < margin_px:
            failure_reasons.append("subject_touches_border")

    if coverage < MIN_ALPHA_COVERAGE:
        failure_reasons.append(f"foreground_too_small:{coverage:.4f}")
    elif coverage > MAX_ALPHA_COVERAGE:
        failure_reasons.append(f"foreground_too_large:{coverage:.4f}")

    if (
        largest_ratio is not None
        and largest_ratio < MIN_LARGEST_COMPONENT_RATIO
    ):
        failure_reasons.append(f"fragmented_subject:{largest_ratio:.4f}")
    elif largest_ratio is None:
        warnings.append("connectivity_skipped")

    score = _score(
        coverage=coverage,
        border_alpha_max=border_alpha_max,
        largest_component_ratio=largest_ratio,
        bbox_ok="subject_touches_border" not in failure_reasons,
    )
    passed = not failure_reasons

    return TransparentQcReport(
        passed=passed,
        score=score,
        failure_reasons=failure_reasons,
        warnings=warnings,
        foreground_bbox=bbox,
        alpha_coverage=coverage,
        border_alpha_max=border_alpha_max,
        largest_component_ratio=largest_ratio,
    )


def _border_alpha_max(alpha: PILImage.Image) -> int:
    width, height = alpha.size
    pixels = alpha.load()
    best = 0
    for x in range(width):
        v = pixels[x, 0]
        if v > best:
            best = v
        v = pixels[x, height - 1]
        if v > best:
            best = v
    for y in range(1, height - 1):
        v = pixels[0, y]
        if v > best:
            best = v
        v = pixels[width - 1, y]
        if v > best:
            best = v
    return int(best)


def _largest_component_ratio(fg_mask: PILImage.Image) -> float | None:
    width, height = fg_mask.size
    longest = max(width, height)
    if longest > COMPONENT_ANALYSIS_MAX_SIDE:
        scale = COMPONENT_ANALYSIS_MAX_SIDE / longest
        target = (max(1, int(width * scale)), max(1, int(height * scale)))
        work = fg_mask.resize(target, PILImage.NEAREST)
    else:
        work = fg_mask.copy()

    try:
        work = work.point(lambda v: 255 if v >= 128 else 0)
        pixels = work.load()
        w, h = work.size
        total_fg = 0
        for y in range(h):
            for x in range(w):
                if pixels[x, y] == 255:
                    total_fg += 1
        if total_fg == 0:
            return None

        label = 1
        for y in range(h):
            for x in range(w):
                if pixels[x, y] == 255:
                    if label > 250:
                        return None
                    ImageDraw.floodfill(work, (x, y), label)
                    label += 1

        if label == 1:
            return None

        data = work.tobytes()
        counts: dict[int, int] = {}
        for v in range(1, label):
            c = data.count(bytes([v]))
            if c:
                counts[v] = c
        if not counts:
            return None
        return max(counts.values()) / total_fg
    finally:
        work.close()


def _score(
    *,
    coverage: float,
    border_alpha_max: int,
    largest_component_ratio: float | None,
    bbox_ok: bool,
) -> float:
    border_score = max(0.0, 1.0 - border_alpha_max / 64.0)
    if MIN_ALPHA_COVERAGE <= coverage <= MAX_ALPHA_COVERAGE:
        coverage_score = 1.0
    elif coverage < MIN_ALPHA_COVERAGE:
        coverage_score = max(0.0, coverage / MIN_ALPHA_COVERAGE)
    else:
        coverage_score = max(0.0, (1.0 - coverage) / (1.0 - MAX_ALPHA_COVERAGE))
    component_score = largest_component_ratio if largest_component_ratio is not None else 0.85
    bbox_score = 1.0 if bbox_ok else 0.0
    return 0.30 * border_score + 0.25 * coverage_score + 0.25 * component_score + 0.20 * bbox_score


def _fail(
    reasons: list[str],
    *,
    foreground_bbox: tuple[int, int, int, int] | None,
    alpha_coverage: float,
    border_alpha_max: int,
    largest_component_ratio: float | None,
) -> TransparentQcReport:
    return TransparentQcReport(
        passed=False,
        score=0.0,
        failure_reasons=reasons,
        warnings=[],
        foreground_bbox=foreground_bbox,
        alpha_coverage=alpha_coverage,
        border_alpha_max=border_alpha_max,
        largest_component_ratio=largest_component_ratio,
    )
