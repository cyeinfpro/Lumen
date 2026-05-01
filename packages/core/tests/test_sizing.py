import pytest

from lumen_core.sizing import (
    _fallback_by_budget,
    resolve_size,
    validate_explicit_size,
)


def test_resolve_size_auto_uses_upstream_auto_with_ratio_instruction():
    resolved = resolve_size("16:9", "auto")

    assert resolved.size == "auto"
    assert resolved.width is None
    assert resolved.height is None
    assert "16:9" in resolved.prompt_suffix


def test_resolve_size_fixed_accepts_valid_explicit_size():
    resolved = resolve_size("1:1", "fixed", "1024x1024")

    assert resolved.size == "1024x1024"
    assert resolved.width == 1024
    assert resolved.height == 1024
    assert resolved.prompt_suffix == ""


def test_resolve_size_fixed_without_explicit_size_uses_aspect_preset():
    resolved = resolve_size("16:9", "fixed")

    assert resolved.size == "3840x2160"
    assert resolved.width == 3840
    assert resolved.height == 2160
    assert resolved.prompt_suffix == ""


def test_resolve_size_fixed_rejects_invalid_format():
    with pytest.raises(ValueError, match="invalid fixed_size format"):
        resolve_size("1:1", "fixed", "1024 by 1024")


@pytest.mark.parametrize(
    ("width", "height", "message"),
    [
        (0, 1024, "positive"),
        (1025, 1024, "multiple"),
        (3856, 2160, "longest side"),
        (256, 256, "total pixels"),
        (3840, 3840, "total pixels"),
        (3840, 1264, "aspect ratio"),
    ],
)
def test_validate_explicit_size_rejects_invalid_boundaries(width, height, message):
    with pytest.raises(ValueError, match=message):
        validate_explicit_size(width, height)


def test_validate_explicit_size_accepts_documented_upper_bound():
    validate_explicit_size(3840, 2160)


@pytest.mark.parametrize(
    ("aspect", "budget"),
    [
        ("1:1", 256),
        ("16:9", 1_572_864),
        ("21:9", 65_536),
    ],
)
def test_fallback_by_budget_returns_aligned_size_within_budget(aspect, budget):
    w, h = _fallback_by_budget(aspect, budget)

    assert w >= 16
    assert h >= 16
    assert w % 16 == 0
    assert h % 16 == 0
    assert w * h <= budget


def test_fallback_by_budget_keeps_ratio_close_after_alignment():
    w, h = _fallback_by_budget("21:9", 65_536)

    ratio = w / h
    target = 21 / 9
    assert abs(ratio - target) / target < 0.02
    assert w * h <= 65_536


def test_fallback_by_budget_rejects_budget_below_minimum_aligned_pixel_count():
    with pytest.raises(ValueError, match="budget too small"):
        _fallback_by_budget("1:1", 255)
