import pytest

from lumen_core.image_resource_limits import (
    MAX_GIF_FRAMES,
    MAX_GIF_REQUEST_FRAMES,
    validate_gif_request_budget,
    validate_gif_resource_budget,
)


def test_preview_request_limit_is_separate_from_local_animation_limit():
    assert MAX_GIF_REQUEST_FRAMES == 16
    assert MAX_GIF_FRAMES == 64
    validate_gif_request_budget("512x512", 16)
    validate_gif_resource_budget(512, 512, 64)


@pytest.mark.parametrize("size", ["auto", "512x512"])
@pytest.mark.parametrize("frames", [17, 64])
def test_unconfirmed_preview_frame_counts_are_rejected(size, frames):
    with pytest.raises(ValueError, match="between 2 and 16"):
        validate_gif_request_budget(size, frames)
