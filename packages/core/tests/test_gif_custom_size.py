import pytest
from lumen_core.sizing import resolve_size
from lumen_core.image_resource_limits import validate_gif_request_budget


def test_gif_auto_and_custom_dimensions_are_separate_from_raster_presets():
    auto = resolve_size("16:9", "auto", output_format="gif")
    assert auto.size == "auto" and auto.width is None and auto.height is None
    custom = resolve_size("16:9", "fixed", "768x432", output_format="gif")
    assert (custom.size, custom.width, custom.height) == ("768x432", 768, 432)
    validate_gif_request_budget(custom.size, 2)
    validate_gif_request_budget(auto.size, 2)


@pytest.mark.parametrize("size", ["0x432", "769x432", "-16x432", "768x0"])
def test_invalid_custom_gif_size_fails(size):
    with pytest.raises(ValueError):
        resolve_size("16:9", "fixed", size, output_format="gif")


def test_gif_local_budget_is_not_removed_for_custom_size():
    with pytest.raises(ValueError, match="Lumen safety limit"):
        validate_gif_request_budget("3840x2160", 9)
