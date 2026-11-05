"""GIF frames use their own documented size contract, not static image limits."""

import pytest
from lumen_core.sizing import resolve_size, validate_gif_frame_size


def test_native_gif_accepts_512_square_without_changing_static_validation():
    resolved = resolve_size("1:1", "fixed", "512x512", output_format="gif")
    assert (resolved.size, resolved.width, resolved.height) == ("512x512", 512, 512)
    with pytest.raises(ValueError, match="total pixels"):
        resolve_size("1:1", "fixed", "512x512", output_format="png")


@pytest.mark.parametrize("width,height", [(0, 512), (-16, 512), (511, 512), (512, 513), (2048, 512)])
def test_gif_invalid_frame_size(width, height):
    with pytest.raises(ValueError):
        validate_gif_frame_size(width, height)


def test_gif_default_is_small_and_static_default_is_unchanged():
    assert resolve_size("1:1", "fixed", output_format="gif").size == "512x512"
    assert resolve_size("1:1", "fixed").size == "2880x2880"


def test_gif_auto_size_preserves_ratio_instruction():
    resolved = resolve_size("7:10", "auto", output_format="gif")
    assert resolved.size == "auto"
    assert "7:10" in resolved.prompt_suffix
