from __future__ import annotations

from PIL import Image as PILImage

from app.background_removal import alpha_refine


def _make_rgba(width: int, height: int, alpha_fill: int = 0) -> PILImage.Image:
    return PILImage.new("RGBA", (width, height), (180, 60, 40, alpha_fill))


def _set_alpha_block(im: PILImage.Image, box: tuple[int, int, int, int], alpha: int) -> None:
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, _ = im.getpixel((x, y))
            im.putpixel((x, y), (r, g, b, alpha))


def test_refine_returns_rgba() -> None:
    src = _make_rgba(32, 32, alpha_fill=0)
    _set_alpha_block(src, (10, 10, 22, 22), 255)
    out = alpha_refine.refine(src)
    src.close()
    try:
        assert out.mode == "RGBA"
        assert out.size == (32, 32)
    finally:
        out.close()


def test_refine_removes_isolated_speckles() -> None:
    src = _make_rgba(64, 64, alpha_fill=0)
    _set_alpha_block(src, (16, 16, 48, 48), 255)
    src.putpixel((2, 2), (180, 60, 40, 255))
    src.putpixel((60, 60), (180, 60, 40, 255))

    out = alpha_refine.refine(src)
    src.close()
    try:
        alpha = out.getchannel("A")
        assert alpha.getpixel((2, 2)) == 0
        assert alpha.getpixel((60, 60)) == 0
        assert alpha.getpixel((32, 32)) > 200
    finally:
        out.close()


def test_refine_fills_small_holes() -> None:
    src = _make_rgba(64, 64, alpha_fill=0)
    _set_alpha_block(src, (16, 16, 48, 48), 255)
    src.putpixel((30, 30), (180, 60, 40, 0))
    src.putpixel((31, 30), (180, 60, 40, 0))

    out = alpha_refine.refine(src)
    src.close()
    try:
        alpha = out.getchannel("A")
        assert alpha.getpixel((30, 30)) > 200
        assert alpha.getpixel((31, 30)) > 200
    finally:
        out.close()


def test_refine_softens_hard_edge() -> None:
    src = _make_rgba(40, 40, alpha_fill=0)
    _set_alpha_block(src, (10, 10, 30, 30), 255)
    out = alpha_refine.refine(src)
    src.close()
    try:
        alpha = out.getchannel("A")
        edge_value = alpha.getpixel((10, 20))
        assert 0 < edge_value < 255
    finally:
        out.close()


def test_refine_handles_tiny_image() -> None:
    src = _make_rgba(2, 2, alpha_fill=128)
    out = alpha_refine.refine(src)
    src.close()
    try:
        assert out.mode == "RGBA"
        assert out.size == (2, 2)
    finally:
        out.close()
