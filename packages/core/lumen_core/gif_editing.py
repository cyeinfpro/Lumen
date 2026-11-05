"""Bounded, local GIF inspection and editing. Never calls an image provider.

Retiming edits only Graphic Control Extension delay bytes. Frame replacement
uses independent full-canvas frames and verifies every untouched rendered frame.
GIF cannot represent arbitrary RGBA: only the replacement may be quantized.
Inputs whose untouched composite cannot be represented losslessly are rejected.
"""
from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from PIL import Image, UnidentifiedImageError

from .image_resource_limits import validate_gif_resource_budget

MAX_GIF_BYTES = 32 * 1024 * 1024


class _LimitedBuffer(io.BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_GIF_BYTES:
            raise ValueError("Edited image exceeds the 32 MiB limit")
        return super().write(data)


@dataclass(frozen=True)
class _Frame:
    delay_offset: int | None
    duration: int


@dataclass(frozen=True)
class _Structure:
    width: int
    height: int
    frames: tuple[_Frame, ...]
    loop: int | None

    def metadata(self) -> dict:
        durations = [frame.duration for frame in self.frames]
        return {
            "width": self.width, "height": self.height,
            "frame_count": len(self.frames), "durations_ms": durations,
            "total_duration_ms": sum(durations), "loop": self.loop,
        }


def _bounded_bytes(data: bytes, label: str) -> None:
    if not isinstance(data, bytes) or not data or len(data) > MAX_GIF_BYTES:
        raise ValueError(f"{label} must be nonempty and at most 32 MiB")


def _require_gif_bytes(data: bytes, end: int) -> None:
    if end > len(data):
        raise ValueError("Truncated GIF")


def _read_subblocks(data: bytes, start: int) -> tuple[int, bytes]:
    """Skip bounded subblocks, retaining only the loop-extension prefix."""
    prefix = bytearray()
    while True:
        _require_gif_bytes(data, start + 1)
        length = data[start]
        start += 1
        if not length:
            return start, bytes(prefix)
        _require_gif_bytes(data, start + length)
        if len(prefix) < 12:
            prefix.extend(data[start:start + min(length, 12 - len(prefix))])
        start += length


def _read_extension(
    data: bytes,
    pos: int,
    pending: tuple[int, int] | None,
    loop: int | None,
    seen_loop: bool,
) -> tuple[int, tuple[int, int] | None, int | None, bool]:
    """Parse control, application or comment blocks without decoding pixels."""
    _require_gif_bytes(data, pos + 2)
    label = data[pos + 1]
    if label == 0xF9:
        _require_gif_bytes(data, pos + 8)
        if pending is not None or data[pos + 2] != 4 or data[pos + 7] != 0:
            raise ValueError("Invalid GIF graphic control extension")
        flags = data[pos + 3]
        if flags & 0xE2 or ((flags >> 2) & 7) > 3:
            raise ValueError("Unsupported GIF disposal or user-input mode")
        pending = (pos + 4, struct.unpack_from("<H", data, pos + 4)[0] * 10)
        pos += 8
    elif label == 0xFF:
        _require_gif_bytes(data, pos + 3)
        size = data[pos + 2]
        if size != 11:
            raise ValueError("Invalid GIF application extension")
        _require_gif_bytes(data, pos + 3 + size)
        name = data[pos + 3:pos + 3 + size]
        pos, payload = _read_subblocks(data, pos + 3 + size)
        if name in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
            if seen_loop or len(payload) != 3 or payload[0] != 1:
                raise ValueError("Invalid GIF loop extension")
            seen_loop = True
            loop = int.from_bytes(payload[1:3], "little")
    elif label == 0xFE:
        pos, _ = _read_subblocks(data, pos + 2)
    else:
        # Plain-text rendering and unknown extension semantics are not
        # safely reproducible by Pillow; reject rather than erase them.
        raise ValueError("Unsupported GIF extension")
    return pos, pending, loop, seen_loop


def _read_frame_data(
    data: bytes,
    pos: int,
    width: int,
    height: int,
    frame_count: int,
    has_global: bool,
) -> int:
    """Validate a descriptor and its palette/data before accepting the frame."""
    _require_gif_bytes(data, pos + 10)
    left, top, fw, fh = struct.unpack_from("<HHHH", data, pos + 1)
    flags = data[pos + 9]
    if flags & 0x18 or not fw or not fh or left + fw > width or top + fh > height:
        raise ValueError("Invalid GIF frame bounds")
    validate_gif_resource_budget(width, height, frame_count)
    has_local = bool(flags & 128)
    if not has_global and not has_local:
        raise ValueError("GIF frame has no color table")
    pos += 10 + (3 * (2 ** ((flags & 7) + 1)) if has_local else 0)
    _require_gif_bytes(data, pos + 2)
    if not 2 <= data[pos] <= 8 or not data[pos + 1]:
        raise ValueError("Invalid GIF image data")
    pos, _ = _read_subblocks(data, pos + 1)
    return pos


def _parse(data: bytes) -> _Structure:
    _bounded_bytes(data, "GIF")
    if len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("Expected a GIF file")
    width, height = struct.unpack_from("<HH", data, 6)
    validate_gif_resource_budget(width, height, 1)
    packed = data[10]
    has_global = bool(packed & 128)
    pos = 13 + (3 * (2 ** ((packed & 7) + 1)) if has_global else 0)
    frames: list[_Frame] = []
    pending: tuple[int, int] | None = None
    loop: int | None = None
    seen_loop = False

    _require_gif_bytes(data, pos)
    while True:
        _require_gif_bytes(data, pos + 1)
        marker = data[pos]
        if marker == 0x3B:
            if pos + 1 != len(data) or pending is not None or not frames:
                raise ValueError("Invalid GIF trailer or trailing data")
            return _Structure(width, height, tuple(frames), loop)
        if marker == 0x21:
            pos, pending, loop, seen_loop = _read_extension(
                data, pos, pending, loop, seen_loop
            )
        elif marker == 0x2C:
            pos = _read_frame_data(
                data, pos, width, height, len(frames) + 1, has_global
            )
            frames.append(_Frame(*(pending if pending is not None else (None, 0))))
            pending = None
        else:
            raise ValueError("Invalid GIF block")


def probe_gif(data: bytes) -> dict:
    """Validate structure/budgets without pixel decoding, for capacity admission."""
    return _parse(data).metadata()


def _decode(data: bytes) -> tuple[_Structure, list[Image.Image]]:
    parsed = _parse(data)
    frames: list[Image.Image] = []
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format != "GIF" or source.size != (parsed.width, parsed.height):
                raise ValueError("GIF decoder metadata mismatch")
            for index in range(len(parsed.frames)):
                source.seek(index)
                source.load()
                frames.append(source.convert("RGBA"))
            if source.n_frames != len(parsed.frames):
                raise ValueError("GIF decoder frame count mismatch")
    except (OSError, EOFError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid or unsafe GIF image data") from exc
    return parsed, frames


def inspect_gif(data: bytes) -> dict:
    parsed, _ = _decode(data)
    return parsed.metadata()


def _validate_index(index: int, count: int) -> None:
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count:
        raise ValueError("GIF frame index is out of range")


def extract_gif_frame(data: bytes, index: int) -> bytes:
    parsed, frames = _decode(data)
    _validate_index(index, len(parsed.frames))
    result = _LimitedBuffer()
    frames[index].save(result, format="PNG")
    return result.getvalue()


def retime_gif(data: bytes, speed: float) -> bytes:
    parsed, _ = _decode(data)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or not 0.1 <= speed <= 10:
        raise ValueError("GIF speed must be between 0.1 and 10")
    if speed == 1:
        return data
    result = bytearray(data)
    for frame in parsed.frames:
        if frame.delay_offset is None or frame.duration == 0:
            raise ValueError("GIF has unspecified/zero frame delays and cannot be reliably retimed")
        delay = int((Decimal(frame.duration // 10) / Decimal(str(speed))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if not 1 <= delay <= 65535:
            raise ValueError("Requested speed exceeds GIF's 10 ms to 655350 ms frame-delay range")
        struct.pack_into("<H", result, frame.delay_offset, delay)
    return bytes(result)


def _replacement(data: bytes, size: tuple[int, int]) -> Image.Image:
    _bounded_bytes(data, "Replacement PNG")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Replacement must be a static PNG")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format != "PNG" or source.size != size or getattr(source, "n_frames", 1) != 1:
                raise ValueError("Replacement PNG must be static and match the GIF frame dimensions")
            source.load()
            result = source.convert("RGBA")
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid replacement PNG") from exc
    # GIF only supports binary transparency. Quantization is confined to the
    # replacement frame; original frames are never silently degraded.
    alpha = result.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    result.putalpha(alpha)
    return result


def _palette_frame(frame: Image.Image, *, transparent: bool, exact: bool) -> Image.Image:
    rgb = frame.convert("RGB")
    alpha = frame.getchannel("A")
    # Normalize invisible RGB before palette collection, without Python pixel lists.
    invisible = alpha.point(lambda value: 255 if value == 0 else 0)
    rgb.paste((0, 0, 0), mask=invisible)
    limit = 255 if transparent else 256
    colors = rgb.getcolors(257)
    if exact:
        # Transparent pixels do not consume a visible palette entry.
        visible_colors = set() if colors is None else {color for _, color in colors}
        if colors is None:
            raise ValueError("This GIF's composited frames exceed the lossless palette limit")
        if invisible.getbbox() is not None and (0, 0, 0) in visible_colors:
            # Keep black if an opaque black pixel exists. A bounded C-level mask
            # avoids allocating a Python object per pixel.
            black = rgb.point(lambda value: 255 if value == 0 else 0).convert("RGB")
            r, g, b = black.split()
            from PIL import ImageChops
            opaque_black = ImageChops.multiply(ImageChops.multiply(r, g), ImageChops.multiply(b, alpha))
            if opaque_black.getbbox() is None:
                visible_colors.remove((0, 0, 0))
        if len(visible_colors) > limit:
            raise ValueError("This GIF's composited frames exceed the lossless palette limit")
        palette = sorted(visible_colors) or [(0, 0, 0)]
        if transparent:
            # Quantize to a palette with no reserved slot first, then shift.
            palette = palette[:255]
        # Quantize without reduction first. A caller-supplied palette uses a
        # nearest-color cache that can change nearby colors even when exact
        # matches exist, whereas <=256 colors survive median-cut unchanged.
        indexed = rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        if transparent:
            generated = indexed.getpalette() or []
            color_to_index = {color: i + 1 for i, color in enumerate(palette)}
            mapping = [color_to_index.get(tuple(generated[i:i+3]), 0) for i in range(0, 768, 3)]
            indexed = indexed.point(mapping)
            full = [0, 0, 0] + [v for color in palette for v in color]
            indexed.putpalette(full + [0] * (768 - len(full)))
    else:
        indexed = rgb.quantize(colors=limit, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
        if transparent:
            palette = (indexed.getpalette() or [])[:765]
            indexed = indexed.point([min(i + 1, 255) for i in range(256)])
            indexed.putpalette([0, 0, 0] + palette + [0] * (765 - len(palette)))
    if transparent:
        indexed.paste(0, mask=invisible)
        indexed.info["transparency"] = 0
    return indexed


def _encode_independent(frames: list[Image.Image], parsed: _Structure, target: int) -> bytes:
    transparent = any(frame.getchannel("A").getextrema()[0] < 255 for frame in frames)
    out = bytearray()
    for index, frame in enumerate(frames):
        indexed = _palette_frame(frame, transparent=transparent, exact=index != target)
        indexed.info.clear()
        stream = _LimitedBuffer()
        options = {"format": "GIF", "optimize": False, "duration": parsed.frames[index].duration, "disposal": 2}
        if transparent:
            options["transparency"] = 0
        indexed.save(stream, **options)
        single = stream.getvalue()
        table_size = 3 * (2 ** ((single[10] & 7) + 1))
        table = single[13:13 + table_size]
        blocks = single[13 + table_size:-1]
        if index == 0:
            out.extend(single[:13 + table_size])
            if parsed.loop is not None:
                out.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01" + struct.pack("<H", parsed.loop) + b"\x00")
        else:
            # The single image always contains GCE(8 bytes), then descriptor.
            if blocks[:3] != b"\x21\xf9\x04" or blocks[8] != 0x2C:
                raise ValueError("Unexpected GIF encoder output")
            descriptor = bytearray(blocks[8:18])
            descriptor[9] = (descriptor[9] & 0x78) | 0x80 | (single[10] & 7)
            blocks = blocks[:8] + descriptor + table + blocks[18:]
        out.extend(blocks)
        if len(out) >= MAX_GIF_BYTES:
            raise ValueError("Edited GIF exceeds the 32 MiB limit")
    out[:6] = b"GIF89a"
    out.append(0x3B)
    return bytes(out)


def _same_visual(left: Image.Image, right: Image.Image) -> bool:
    from PIL import ImageChops
    if left.getchannel("A").tobytes() != right.getchannel("A").tobytes():
        return False
    difference = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
    return ImageChops.multiply(difference, left.getchannel("A").convert("RGB")).getbbox() is None


def replace_gif_frame(data: bytes, index: int, png: bytes) -> bytes:
    parsed, frames = _decode(data)
    _validate_index(index, len(parsed.frames))
    replacement = _replacement(png, (parsed.width, parsed.height))
    if _same_visual(frames[index], replacement):
        return data
    originals = frames[:]
    frames[index] = replacement
    result = _encode_independent(frames, parsed, index)
    checked, decoded = _decode(result)
    if checked.metadata() != parsed.metadata():
        raise ValueError("Edited GIF failed timing/loop verification")
    for position, (before, after) in enumerate(zip(originals, decoded, strict=True)):
        if position != index and not _same_visual(before, after):
            raise ValueError("This GIF cannot be edited without altering another frame")
    if not _same_visual(replacement, decoded[index]):
        # Palette loss on the new frame is expected, but transparency is not.
        if replacement.getchannel("A").tobytes() != decoded[index].getchannel("A").tobytes():
            raise ValueError("Edited GIF failed replacement transparency verification")
    return result
