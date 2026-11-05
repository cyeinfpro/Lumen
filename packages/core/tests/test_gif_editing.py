"""Synthetic-only coverage for the bounded local GIF editor."""
import io
import struct

import pytest
from PIL import Image

from lumen_core.gif_editing import (
    MAX_GIF_BYTES, extract_gif_frame, inspect_gif, probe_gif,
    replace_gif_frame, retime_gif,
)


def png(image):
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def gif(colors=("red", "blue", "green"), durations=(100, 230, 470), loop=0, disposal=2):
    frames = [Image.new("RGBA", (8, 6), color) for color in colors]
    stream = io.BytesIO()
    args = dict(format="GIF", save_all=True, append_images=frames[1:], duration=list(durations), disposal=disposal, optimize=False)
    if loop is not None:
        args["loop"] = loop
    frames[0].save(stream, **args)
    return stream.getvalue()


def decoded(data):
    with Image.open(io.BytesIO(data)) as source:
        result = []
        for i in range(source.n_frames):
            source.seek(i)
            result.append(source.convert("RGBA"))
        return result


def visible_pixels(image):
    data = image.tobytes()
    return [tuple(data[i:i + 4]) if data[i + 3] else (0, 0, 0, 0) for i in range(0, len(data), 4)]


def test_probe_and_inspect_metadata_agree():
    data = gif(loop=3)
    expected = dict(width=8, height=6, frame_count=3, durations_ms=[100, 230, 470], total_duration_ms=800, loop=3)
    assert probe_gif(data) == inspect_gif(data) == expected


@pytest.mark.parametrize("loop", [None, 0, 2, 65535])
def test_retime_changes_only_delay_bytes(loop):
    source = gif(loop=loop)
    changed = retime_gif(source, 2)
    assert inspect_gif(changed)["durations_ms"] == [50, 120, 240]
    assert inspect_gif(changed)["loop"] == loop
    assert len(changed) == len(source)
    allowed = set()
    start = 0
    while (offset := source.find(b"\x21\xf9\x04", start)) != -1:
        allowed.update([offset + 4, offset + 5])
        start = offset + 8
    assert all(a == b or i in allowed for i, (a, b) in enumerate(zip(source, changed)))
    assert [visible_pixels(f) for f in decoded(source)] == [visible_pixels(f) for f in decoded(changed)]
    assert retime_gif(source, 1) == source


@pytest.mark.parametrize("speed", [0, -1, 0.099, 10.01, float("inf"), float("nan"), True, "2", None])
def test_invalid_speed(speed):
    with pytest.raises(ValueError):
        retime_gif(gif(), speed)


def test_noninteger_speed_uses_nearest_centisecond():
    assert inspect_gif(retime_gif(gif(), 1.7))["durations_ms"] == [60, 140, 280]


def test_retime_overflow_and_underflow_not_clamped():
    with pytest.raises(ValueError, match="range"):
        retime_gif(gif(durations=(10, 20, 30)), 10)
    with pytest.raises(ValueError, match="range"):
        retime_gif(gif(durations=(655350, 20, 30)), 0.1)


def test_zero_delays_preserved_but_not_retimed():
    source = gif(durations=(0, 230, 470))
    assert retime_gif(source, 1) == source
    with pytest.raises(ValueError, match="zero"):
        retime_gif(source, 2)


@pytest.mark.parametrize("loop", [None, 0, 7])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_extract_and_replace_preserve_other_frames_timing_and_loop(loop, index):
    source = gif(loop=loop)
    original = decoded(source)
    extracted = Image.open(io.BytesIO(extract_gif_frame(source, index)))
    assert extracted.format == "PNG"
    assert visible_pixels(extracted.convert("RGBA")) == visible_pixels(original[index])
    replacement = Image.new("RGBA", (8, 6), "yellow")
    changed = replace_gif_frame(source, index, png(replacement))
    assert inspect_gif(changed) == inspect_gif(source)
    actual = decoded(changed)
    for i in range(3):
        assert visible_pixels(actual[i]) == visible_pixels(replacement if i == index else original[i])


@pytest.mark.parametrize("index", [-1, 3, True, 0.5, "0"])
def test_bad_index(index):
    with pytest.raises(ValueError):
        extract_gif_frame(gif(), index)
    with pytest.raises(ValueError):
        replace_gif_frame(gif(), index, png(Image.new("RGB", (8, 6))))


@pytest.mark.parametrize("replacement", [b"not png", gif(), png(Image.new("RGB", (9, 6)))])
def test_replacement_rejects_masquerade_or_size_change(replacement):
    with pytest.raises(ValueError):
        replace_gif_frame(gif(), 1, replacement)


def test_apng_rejected():
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "red").save(stream, format="PNG", save_all=True, append_images=[Image.new("RGB", (8, 6), "blue")])
    with pytest.raises(ValueError, match="static"):
        replace_gif_frame(gif(), 1, stream.getvalue())


def test_partial_alpha_is_binary_only_on_replacement():
    replacement = Image.new("RGBA", (8, 6), (255, 0, 0, 127))
    replacement.putpixel((0, 0), (0, 255, 0, 128))
    changed = decoded(replace_gif_frame(gif(), 1, png(replacement)))
    assert changed[1].getpixel((1, 1))[3] == 0
    assert changed[1].getpixel((0, 0))[3] == 255
    assert visible_pixels(changed[0]) == visible_pixels(decoded(gif())[0])


def transparent_fixture(disposal):
    frames = []
    for x, color in [(0, "red"), (2, "green"), (4, "blue")]:
        image = Image.new("RGBA", (8, 6), (0, 0, 0, 0))
        for xx in range(x, x + 2):
            for yy in range(2, 4):
                image.putpixel((xx, yy), Image.new("RGBA", (1, 1), color).getpixel((0, 0)))
        frames.append(image)
    stream = io.BytesIO()
    frames[0].save(stream, format="GIF", save_all=True, append_images=frames[1:], duration=[50, 90, 120], disposal=disposal, loop=4, optimize=False)
    return stream.getvalue()


@pytest.mark.parametrize("disposal", [1, 2, 3, [1, 2, 3]])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_transparency_disposal_and_local_palettes(disposal, index):
    source = transparent_fixture(disposal)
    before = decoded(source)
    replacement = Image.new("RGBA", (8, 6), (0, 0, 0, 0))
    replacement.putpixel((7, 5), (255, 255, 0, 255))
    result = replace_gif_frame(source, index, png(replacement))
    assert inspect_gif(result) == inspect_gif(source)
    after = decoded(result)
    for i in range(3):
        assert visible_pixels(after[i]) == visible_pixels(replacement if i == index else before[i])


def test_duplicate_frames_are_not_merged():
    source = gif()
    original = decoded(source)
    # Make frame 1 equal frame 0. Pillow save_all normally coalesces these.
    result = replace_gif_frame(source, 1, png(original[0]))
    assert inspect_gif(result)["frame_count"] == 3
    assert inspect_gif(result)["durations_ms"] == [100, 230, 470]
    assert visible_pixels(decoded(result)[0]) == visible_pixels(decoded(result)[1])
    assert inspect_gif(retime_gif(result, 2))["frame_count"] == 3


@pytest.mark.parametrize("bad", [b"", b"GIF89a", b"wrong format", png(Image.new("RGB", (1, 1))), gif()[:-1], gif() + b"trailing"])
def test_invalid_gifs_rejected(bad):
    with pytest.raises(ValueError):
        probe_gif(bad)
    with pytest.raises(ValueError):
        inspect_gif(bad)


def test_oversized_file():
    with pytest.raises(ValueError, match="32 MiB"):
        probe_gif(b"x" * (MAX_GIF_BYTES + 1))


def test_probe_rejects_canvas_bomb_before_pixel_decode(monkeypatch):
    source = bytearray(gif())
    struct.pack_into("<HH", source, 6, 10000, 10000)
    monkeypatch.setattr(Image, "open", lambda *_: pytest.fail("must not decode"))
    with pytest.raises(ValueError, match="64000000"):
        probe_gif(bytes(source))


def test_frame_limit_checked_during_scan():
    source = gif()
    # Duplicate the complete extension/image block with its local table 65 times.
    pos = 13 + 3 * (2 ** ((source[10] & 7) + 1))
    first_gce = source.find(b"\x21\xf9\x04", pos)
    next_gce = source.find(b"\x21\xf9\x04", first_gce + 8)
    data = source[:first_gce] + source[first_gce:next_gce] * 65 + b"\x3b"
    with pytest.raises(ValueError, match="64 frames"):
        probe_gif(data)


def test_total_pixel_limit_checked_for_every_frame():
    source = bytearray(gif())
    struct.pack_into("<HH", source, 6, 6000, 6000)
    with pytest.raises(ValueError, match="64000000"):
        probe_gif(bytes(source))


def test_bad_disposal_and_frame_bounds():
    source = bytearray(gif())
    gce = source.find(b"\x21\xf9\x04")
    source[gce + 3] = 0x10
    with pytest.raises(ValueError, match="disposal"):
        probe_gif(bytes(source))
    source = bytearray(gif())
    descriptor = source.index(0x2C, gce + 8)
    struct.pack_into("<H", source, descriptor + 1, 8)
    with pytest.raises(ValueError, match="bounds"):
        probe_gif(bytes(source))


def test_corrupt_lzw_is_rejected_by_full_inspection():
    source = bytearray(gif())
    gce = source.find(b"\x21\xf9\x04")
    descriptor = source.index(0x2C, gce + 8)
    start = descriptor + 10
    if source[descriptor + 9] & 128:
        start += 3 * (2 ** ((source[descriptor + 9] & 7) + 1))
    # Valid structural subblock lengths, invalid compressed stream.
    for i in range(start + 2, start + 2 + source[start + 1]):
        source[i] = 255
    with pytest.raises(ValueError):
        inspect_gif(bytes(source))


def palette_gif(transparent=False):
    first = Image.new("P", (16, 16))
    first.putpalette([value for i in range(256) for value in (i, i // 2, 255 - i)])
    first.putdata(list(range(256)))
    second = first.copy()
    second.putpixel((0, 0), 254)
    stream = io.BytesIO()
    options = dict(format="GIF", save_all=True, append_images=[second], duration=[110, 150], loop=2, disposal=2, optimize=False)
    if transparent:
        options["transparency"] = 0
    first.save(stream, **options)
    return stream.getvalue()


@pytest.mark.parametrize("transparent", [False, True])
def test_full_palette_untouched_frame_is_exact(transparent):
    source = palette_gif(transparent)
    replacement = Image.new("RGBA", (16, 16), "yellow")
    result = replace_gif_frame(source, 1, png(replacement))
    assert visible_pixels(decoded(result)[0]) == visible_pixels(decoded(source)[0])
    assert inspect_gif(result) == inspect_gif(source)


def test_transparency_cannot_silently_degrade_256_opaque_colors():
    source = palette_gif()
    replacement = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    with pytest.raises(ValueError, match="lossless palette"):
        replace_gif_frame(source, 1, png(replacement))


def test_unchanged_replacement_preserves_original_bytes():
    source = palette_gif()
    assert replace_gif_frame(source, 0, extract_gif_frame(source, 0)) == source


def test_static_gif_without_gce():
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "red").save(stream, format="GIF")
    source = stream.getvalue()
    assert inspect_gif(source)["durations_ms"] == [0]
    with pytest.raises(ValueError, match="unspecified"):
        retime_gif(source, 2)
    result = replace_gif_frame(source, 0, png(Image.new("RGB", (8, 6), "blue")))
    assert inspect_gif(result)["durations_ms"] == [0]
    assert inspect_gif(result)["loop"] is None


def test_comments_survive_retiming():
    source = gif()
    data = source[:-1] + b"\x21\xfe\x03abc\x00\x3b"
    result = retime_gif(data, 2)
    assert result.endswith(b"\x21\xfe\x03abc\x00\x3b")


def test_png_size_checked_before_decoding():
    # A real PNG header declaring a different huge canvas must not be loaded.
    source = bytearray(png(Image.new("RGB", (1, 1))))
    struct.pack_into(">II", source, 16, 100000, 100000)
    import zlib
    struct.pack_into(">I", source, 29, zlib.crc32(source[12:29]))
    with pytest.raises(ValueError):
        replace_gif_frame(gif(), 1, bytes(source))


def test_output_buffer_limit_is_enforced_during_write(monkeypatch):
    import lumen_core.gif_editing as editing
    monkeypatch.setattr(editing, "MAX_GIF_BYTES", 8)
    result = editing._LimitedBuffer()
    assert result.write(b"12345678") == 8
    with pytest.raises(ValueError, match="32 MiB"):
        result.write(b"9")
    assert result.getvalue() == b"12345678"


def test_exact_frame_budget_is_accepted():
    source = gif()
    first_gce = source.find(b"\x21\xf9\x04")
    next_gce = source.find(b"\x21\xf9\x04", first_gce + 8)
    data = source[:first_gce] + source[first_gce:next_gce] * 64 + b"\x3b"
    assert inspect_gif(data)["frame_count"] == 64


@pytest.mark.parametrize("width,height", [(0, 6), (8, 0), (10001, 1), (1, 10001)])
def test_dimension_bounds_rejected_by_probe(width, height):
    source = bytearray(gif())
    struct.pack_into("<HH", source, 6, width, height)
    with pytest.raises(ValueError):
        probe_gif(bytes(source))
