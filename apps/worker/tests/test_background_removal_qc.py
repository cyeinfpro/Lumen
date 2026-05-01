from __future__ import annotations

from PIL import Image as PILImage

from app.background_removal import qc


def _solid_alpha(width: int, height: int, value: int) -> PILImage.Image:
    rgba = PILImage.new("RGBA", (width, height), (200, 50, 30, 255))
    alpha = PILImage.new("L", (width, height), value)
    rgba.putalpha(alpha)
    return rgba


def _centered_subject(
    width: int = 64,
    height: int = 64,
    *,
    box: tuple[int, int, int, int] | None = None,
    border_alpha: int = 0,
) -> PILImage.Image:
    rgba = PILImage.new("RGBA", (width, height), (255, 255, 255, border_alpha))
    if box is None:
        box = (width // 4, height // 4, width * 3 // 4, height * 3 // 4)
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            rgba.putpixel((x, y), (200, 80, 30, 255))
    return rgba


def test_rejects_fully_opaque_image() -> None:
    rgba = _solid_alpha(32, 32, 255)
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert "alpha_all_opaque" in report.failure_reasons


def test_rejects_fully_transparent_image() -> None:
    rgba = _solid_alpha(32, 32, 0)
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert "alpha_all_transparent" in report.failure_reasons


def test_rejects_subject_touching_border() -> None:
    rgba = _centered_subject(64, 64, box=(0, 0, 50, 50))
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert any(r.startswith("subject_touches_border") for r in report.failure_reasons)


def test_rejects_too_small_subject() -> None:
    rgba = _centered_subject(64, 64, box=(30, 30, 32, 32))
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert any(r.startswith("foreground_too_small") for r in report.failure_reasons)


def test_rejects_too_large_subject() -> None:
    rgba = _centered_subject(80, 80, box=(1, 1, 79, 79))
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert any(r.startswith("foreground_too_large") for r in report.failure_reasons)


def test_rejects_non_rgba_image() -> None:
    rgb = PILImage.new("RGB", (32, 32), (255, 255, 255))
    report = qc.evaluate(rgb)
    rgb.close()
    assert report.passed is False
    assert "alpha_missing" in report.failure_reasons


def test_rejects_high_border_alpha() -> None:
    rgba = _centered_subject(64, 64, box=(16, 16, 48, 48), border_alpha=128)
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert any(r.startswith("border_alpha_too_high") for r in report.failure_reasons)


def test_rejects_fragmented_subject() -> None:
    rgba = PILImage.new("RGBA", (64, 64), (255, 255, 255, 0))
    # one big component (~28% area) + many tiny disconnected blobs (~25%)
    for y in range(20, 44):
        for x in range(20, 30):
            rgba.putpixel((x, y), (200, 80, 30, 255))
    blobs = [(50, 6), (6, 50), (50, 50), (35, 6), (6, 35), (50, 35), (35, 50)]
    for cx, cy in blobs:
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                x, y = cx + dx, cy + dy
                if 0 <= x < 64 and 0 <= y < 64:
                    rgba.putpixel((x, y), (200, 80, 30, 255))
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is False
    assert any(
        r.startswith("fragmented_subject") or r.startswith("subject_touches_border")
        for r in report.failure_reasons
    )


def test_passes_centered_subject() -> None:
    rgba = _centered_subject(64, 64, box=(16, 16, 48, 48))
    report = qc.evaluate(rgba)
    rgba.close()
    assert report.passed is True, report.failure_reasons
    assert report.border_alpha_max <= qc.BORDER_ALPHA_MAX_THRESHOLD
    assert qc.MIN_ALPHA_COVERAGE <= report.alpha_coverage <= qc.MAX_ALPHA_COVERAGE
    assert report.foreground_bbox == (16, 16, 48, 48)
    assert report.score > 0.5


def test_to_dict_round_trip() -> None:
    rgba = _centered_subject(64, 64, box=(16, 16, 48, 48))
    report = qc.evaluate(rgba)
    rgba.close()
    d = report.to_dict()
    assert d["passed"] is True
    assert isinstance(d["failure_reasons"], list)
    assert isinstance(d["score"], float)
    assert d["foreground_bbox"] == [16, 16, 48, 48]
