from __future__ import annotations

import io
from typing import Any

from PIL import Image as PILImage

from app.tasks import completion


def _png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (width, height), color=(12, 34, 56)).save(buf, format="PNG")
    return buf.getvalue()


def test_completion_tool_image_skips_blurhash_for_tiny_images(
    monkeypatch: Any,
) -> None:
    def fail_blurhash(_img: PILImage.Image) -> str:
        raise AssertionError("tiny images must not call blurhash encoder")

    monkeypatch.setattr(completion, "_generation_compute_blurhash", fail_blurhash)

    (
        orig_ext,
        orig_mime,
        width,
        height,
        blurhash_str,
        *_variants,
    ) = completion._image_format_and_meta(_png_bytes(2, 2))

    assert orig_ext == "png"
    assert orig_mime == "image/png"
    assert (width, height) == (2, 2)
    assert blurhash_str is None


def test_tool_image_dedupe_key_uses_b64_sha1_without_item_id() -> None:
    b64_one = " data:image/png;base64,\nQUJDRA== "
    b64_two = "QUJDRA=="

    assert completion._tool_image_dedupe_key({}, b64_one).startswith("b64sha1:")
    assert completion._tool_image_dedupe_key({}, b64_one) == (
        completion._tool_image_dedupe_key({}, b64_two)
    )


def test_tool_image_dedupe_key_prefers_item_id() -> None:
    key = completion._tool_image_dedupe_key(
        {"item": {"id": "img-call-1"}},
        "different-image",
    )

    assert key == "id:img-call-1"
