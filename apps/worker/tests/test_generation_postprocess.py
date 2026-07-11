from __future__ import annotations

import io
import pickle
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import pytest
from PIL import Image as PILImage

from app.tasks import generation
from app.tasks.generation_parts import postprocess


def _png_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGBA", size, (40, 120, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _variant_bundle(
    payload_bytes: bytes = b"x",
    *,
    engine: str = "pil",
) -> generation._ImageVariantBundle:
    payload = generation._VariantPayload(payload_bytes, (1, 1))
    return generation._ImageVariantBundle(
        orig_format="PNG",
        width=1,
        height=1,
        display=payload,
        preview=payload,
        thumb=payload,
        engine=engine,
    )


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


def test_cached_postprocess_executor_does_not_resolve_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = Executor()

    def fail_worker_resolution() -> int:
        raise AssertionError("cached executor must not resolve workers again")

    monkeypatch.setattr(generation, "_IMAGE_POSTPROCESS_EXECUTOR", executor)
    monkeypatch.setattr(
        generation,
        "_resolve_image_postprocess_workers",
        fail_worker_resolution,
    )

    assert generation._get_image_postprocess_executor() is executor


def test_process_pool_variant_facade_is_importable_and_picklable() -> None:
    restored = pickle.loads(pickle.dumps(generation._make_image_variants_sync))

    assert restored is generation._make_image_variants_sync
    assert restored.__module__ == "app.tasks.generation"
    assert generation._IMAGE_POSTPROCESS_MODES is postprocess._IMAGE_POSTPROCESS_MODES


@pytest.mark.asyncio
async def test_variant_orchestration_facade_uses_late_bound_generation_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _variant_bundle(b"late-bound", engine="facade")
    reset_calls: list[None] = []

    def resolve_mode(_mode: str | None = None) -> str:
        return "inline"

    def get_executor() -> Executor:
        raise AssertionError("executor should not be called by facade probe")

    def reset_executor() -> None:
        reset_calls.append(None)

    def make_variants(_raw_image: bytes) -> generation._ImageVariantBundle:
        return result

    def make_pil_variants(_raw_image: bytes) -> generation._ImageVariantBundle:
        return result

    async def extracted(
        raw_image: bytes,
        *,
        mode: str | None,
        hooks: postprocess.ImageVariantExecutionHooks,
        logger: Any,
    ) -> tuple[generation._ImageVariantBundle, str]:
        assert raw_image == b"raw"
        assert mode == "thread"
        assert logger is generation.logger
        assert hooks.resolve_mode is resolve_mode
        assert hooks.get_executor is get_executor
        assert hooks.reset_executor is reset_executor
        assert hooks.make_variants_sync is make_variants
        assert hooks.make_variants_pil_only_sync is make_pil_variants
        assert hooks.broken_process_pool_type is generation.BrokenProcessPool
        return result, "thread"

    monkeypatch.setattr(generation, "_resolve_image_postprocess_mode", resolve_mode)
    monkeypatch.setattr(generation, "_get_image_postprocess_executor", get_executor)
    monkeypatch.setattr(generation, "_reset_image_postprocess_executor", reset_executor)
    monkeypatch.setattr(generation, "_make_image_variants_sync", make_variants)
    monkeypatch.setattr(
        generation,
        "_make_image_variants_pil_only_sync",
        make_pil_variants,
    )
    monkeypatch.setattr(postprocess, "_postprocess_image_variants", extracted)

    variants, mode = await generation._postprocess_image_variants(
        b"raw",
        mode="thread",
    )

    assert variants is result
    assert mode == "thread"
    assert reset_calls == []


@pytest.mark.asyncio
async def test_raw_postprocess_facade_uses_late_bound_generation_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _png_bytes()
    variants = _variant_bundle(b"processed", engine="facade")
    expected = generation._PostprocessedGeneratedImage(
        raw_image=raw,
        sha256="sha",
        orig_format="PNG",
        width=1,
        height=1,
        blurhash="blur",
        display=variants.display,
        preview=variants.preview,
        thumb=variants.thumb,
    )

    def inspect(
        _raw_image: bytes,
    ) -> generation._GeneratedImageInspection:
        return generation._GeneratedImageInspection("PNG", 1, 1, False)

    def sha256(_raw_image: bytes) -> str:
        return "sha"

    async def transparent_request(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("transparent hook should not run by facade probe")

    def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def process_variants(
        _raw_image: bytes,
        *,
        mode: str | None = None,
    ) -> tuple[generation._ImageVariantBundle, str]:
        return variants, mode or "inline"

    def compute_blurhash(_image: PILImage.Image) -> str:
        return "blur"

    def decode_error(exc: Exception) -> generation.UpstreamError:
        return generation.UpstreamError(str(exc))

    async def extracted(
        raw_image: bytes,
        *,
        prompt: str,
        transparent_requested: bool,
        mode: str | None,
        hooks: postprocess.GeneratedImagePostprocessHooks,
    ) -> generation._PostprocessedGeneratedImage:
        assert raw_image == raw
        assert prompt == "prompt"
        assert transparent_requested is True
        assert mode == "inline"
        assert hooks.inspect_generated_image_sync is inspect
        assert hooks.sha256 is sha256
        assert hooks.process_transparent_request is transparent_request
        assert (
            hooks.transparent_pipeline_failure_type
            is generation.TransparentPipelineFailure
        )
        assert hooks.sanitize_transparent_qc_payload is sanitize
        assert hooks.postprocess_image_variants is process_variants
        assert hooks.compute_blurhash is compute_blurhash
        assert hooks.image_decode_upstream_error is decode_error
        assert hooks.upstream_error_type is generation.UpstreamError
        assert hooks.bad_response_error_code == generation.EC.BAD_RESPONSE.value
        assert (
            hooks.generated_image_inspection_type
            is generation._GeneratedImageInspection
        )
        assert (
            hooks.postprocessed_generated_image_type
            is generation._PostprocessedGeneratedImage
        )
        return expected

    monkeypatch.setattr(generation, "_inspect_generated_image_sync", inspect)
    monkeypatch.setattr(generation, "_sha256", sha256)
    monkeypatch.setattr(
        generation,
        "process_transparent_request",
        transparent_request,
    )
    monkeypatch.setattr(generation, "_sanitize_transparent_qc_payload", sanitize)
    monkeypatch.setattr(generation, "_postprocess_image_variants", process_variants)
    monkeypatch.setattr(generation, "_compute_blurhash", compute_blurhash)
    monkeypatch.setattr(generation, "_image_decode_upstream_error", decode_error)
    monkeypatch.setattr(postprocess, "_postprocess_raw_generated_image", extracted)

    result = await generation._postprocess_raw_generated_image(
        raw,
        prompt="prompt",
        transparent_requested=True,
        mode="inline",
    )

    assert result is expected
