from __future__ import annotations

import io
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import pytest
from PIL import Image as PILImage

from app.tasks import generation


def _png_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGBA", size, (40, 120, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_postprocess_generated_image_inline_builds_variants() -> None:
    raw = _png_bytes()

    result = await generation._postprocess_raw_generated_image(
        raw,
        prompt="test image",
        transparent_requested=False,
        mode="inline",
    )

    assert result.raw_image == raw
    assert result.sha256 == generation._sha256(raw)
    assert result.orig_format == "PNG"
    assert (result.width, result.height) == (32, 24)
    assert result.display.size == (32, 24)
    assert result.preview.size == (32, 24)
    assert result.thumb.size == (32, 24)
    assert result.display.bytes.startswith(b"RIFF")
    assert result.preview.bytes.startswith(b"RIFF")
    assert result.thumb.bytes.startswith(b"\xff\xd8")
    assert result.executor_mode == "inline"


@pytest.mark.asyncio
async def test_postprocess_generated_image_rejects_invalid_bytes() -> None:
    with pytest.raises(generation.UpstreamError, match="pillow could not decode"):
        await generation._postprocess_raw_generated_image(
            b"not an image",
            prompt="bad",
            transparent_requested=False,
            mode="inline",
        )


@pytest.mark.asyncio
async def test_postprocess_variants_thread_mode_is_dispatchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []

    def fake_make_variants(raw_image: bytes) -> generation._ImageVariantBundle:
        calls.append(raw_image)
        payload = generation._VariantPayload(b"x", (1, 1))
        return generation._ImageVariantBundle(
            orig_format="PNG",
            width=1,
            height=1,
            display=payload,
            preview=payload,
            thumb=payload,
            engine="pil",
        )

    monkeypatch.setattr(generation, "_make_image_variants_sync", fake_make_variants)

    variants, mode = await generation._postprocess_image_variants(
        b"raw",
        mode="thread",
    )

    assert mode == "thread"
    assert variants.display.bytes == b"x"
    assert calls == [b"raw"]


@pytest.mark.asyncio
async def test_process_pool_failure_falls_back_to_pil_only_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenExecutor(Executor):
        def submit(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            raise RuntimeError("broken worker")

    calls: list[bytes] = []

    def fail_if_retried(raw_image: bytes) -> generation._ImageVariantBundle:
        raise AssertionError("libvips-capable helper must not run in fallback thread")

    def fake_pil_only(raw_image: bytes) -> generation._ImageVariantBundle:
        calls.append(raw_image)
        payload = generation._VariantPayload(b"pil", (1, 1))
        return generation._ImageVariantBundle(
            orig_format="PNG",
            width=1,
            height=1,
            display=payload,
            preview=payload,
            thumb=payload,
            engine="pil",
        )

    monkeypatch.setattr(generation, "_get_image_postprocess_executor", BrokenExecutor)
    monkeypatch.setattr(generation, "_make_image_variants_sync", fail_if_retried)
    monkeypatch.setattr(generation, "_make_image_variants_pil_only_sync", fake_pil_only)

    variants, mode = await generation._postprocess_image_variants(
        b"raw",
        mode="process_pool",
    )

    assert mode == "thread"
    assert variants.engine == "pil"
    assert variants.display.bytes == b"pil"
    assert calls == [b"raw"]


@pytest.mark.asyncio
async def test_broken_process_pool_resets_cached_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenExecutor(Executor):
        def submit(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            raise BrokenProcessPool("worker died")

        def shutdown(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[no-untyped-def]
            shutdown_calls.append(kwargs)

    shutdown_calls: list[dict[str, Any]] = []

    def fake_pil_only(raw_image: bytes) -> generation._ImageVariantBundle:
        payload = generation._VariantPayload(raw_image, (1, 1))
        return generation._ImageVariantBundle(
            orig_format="PNG",
            width=1,
            height=1,
            display=payload,
            preview=payload,
            thumb=payload,
            engine="pil",
        )

    executor = BrokenExecutor()
    monkeypatch.setattr(generation, "_IMAGE_POSTPROCESS_EXECUTOR", executor)
    monkeypatch.setattr(generation, "_make_image_variants_pil_only_sync", fake_pil_only)

    variants, mode = await generation._postprocess_image_variants(
        b"raw",
        mode="process_pool",
    )

    assert mode == "thread"
    assert variants.engine == "pil"
    assert generation._IMAGE_POSTPROCESS_EXECUTOR is None
    assert shutdown_calls == [{"wait": False, "cancel_futures": True}]
