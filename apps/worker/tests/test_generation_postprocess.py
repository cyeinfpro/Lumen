from __future__ import annotations

# ruff: noqa: E402

import io
import pickle
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

import pytest
from PIL import Image as PILImage

from lumen_core.constants import GenerationErrorCode as EC

from app.background_removal import TransparentPipelineFailure
from app.provider_runtime.errors import UpstreamError
from app.tasks.generation_parts import composition_support as support
from app.tasks.generation_parts import image_artifact_contracts as artifacts
from app.tasks.generation_parts import postprocess
from app.tasks.generation_parts.runtime import ImagePostprocessRuntime


postprocess_runtime = ImagePostprocessRuntime()


def _png_bytes(size: tuple[int, int] = (32, 24)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGBA", size, (40, 120, 200, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _variant_bundle(
    payload_bytes: bytes = b"x",
    *,
    engine: str = "pil",
) -> artifacts.ImageVariantBundle:
    payload = artifacts.VariantPayload(payload_bytes, (1, 1))
    return artifacts.ImageVariantBundle(
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

    result = await support.postprocess_raw_generated_image(
        raw,
        prompt="test image",
        transparent_requested=False,
        mode="inline",
        runtime=postprocess_runtime,
    )

    assert type(result) is artifacts.PostprocessedGeneratedImage
    assert result.raw_image == raw
    assert result.sha256 == artifacts.sha256(raw)
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
    with pytest.raises(UpstreamError, match="pillow could not decode"):
        await support.postprocess_raw_generated_image(
            b"not an image",
            prompt="bad",
            transparent_requested=False,
            mode="inline",
            runtime=postprocess_runtime,
        )


@pytest.mark.asyncio
async def test_postprocess_variants_thread_mode_is_dispatchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bytes] = []

    def fake_make_variants(raw_image: bytes) -> artifacts.ImageVariantBundle:
        calls.append(raw_image)
        payload = artifacts.VariantPayload(b"x", (1, 1))
        return artifacts.ImageVariantBundle(
            orig_format="PNG",
            width=1,
            height=1,
            display=payload,
            preview=payload,
            thumb=payload,
            engine="pil",
        )

    monkeypatch.setattr(support, "make_image_variants_sync", fake_make_variants)

    variants, mode = await support.postprocess_image_variants(
        b"raw",
        mode="thread",
        runtime=postprocess_runtime,
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

    def fail_if_retried(raw_image: bytes) -> artifacts.ImageVariantBundle:
        raise AssertionError("libvips-capable helper must not run in fallback thread")

    def fake_pil_only(raw_image: bytes) -> artifacts.ImageVariantBundle:
        calls.append(raw_image)
        payload = artifacts.VariantPayload(b"pil", (1, 1))
        return artifacts.ImageVariantBundle(
            orig_format="PNG",
            width=1,
            height=1,
            display=payload,
            preview=payload,
            thumb=payload,
            engine="pil",
        )

    monkeypatch.setattr(
        support,
        "get_image_postprocess_executor",
        lambda _runtime: BrokenExecutor(),
    )
    monkeypatch.setattr(support, "make_image_variants_sync", fail_if_retried)
    monkeypatch.setattr(
        support,
        "make_image_variants_pil_only_sync",
        fake_pil_only,
    )

    variants, mode = await support.postprocess_image_variants(
        b"raw",
        mode="process_pool",
        runtime=postprocess_runtime,
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

    def fake_pil_only(raw_image: bytes) -> artifacts.ImageVariantBundle:
        payload = artifacts.VariantPayload(raw_image, (1, 1))
        return artifacts.ImageVariantBundle(
            orig_format="PNG",
            width=1,
            height=1,
            display=payload,
            preview=payload,
            thumb=payload,
            engine="pil",
        )

    executor = BrokenExecutor()
    runtime = postprocess_runtime
    monkeypatch.setattr(runtime, "executor", executor)
    monkeypatch.setattr(
        support,
        "make_image_variants_pil_only_sync",
        fake_pil_only,
    )

    variants, mode = await support.postprocess_image_variants(
        b"raw",
        mode="process_pool",
        runtime=postprocess_runtime,
    )

    assert mode == "thread"
    assert variants.engine == "pil"
    assert runtime.executor is None
    assert shutdown_calls == [{"wait": False, "cancel_futures": True}]


def test_cached_postprocess_executor_does_not_resolve_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = Executor()

    def fail_worker_resolution() -> int:
        raise AssertionError("cached executor must not resolve workers again")

    runtime = postprocess_runtime
    monkeypatch.setattr(runtime, "executor", executor)
    monkeypatch.setattr(
        support,
        "resolve_image_postprocess_workers",
        fail_worker_resolution,
    )

    assert support.get_image_postprocess_executor(runtime) is executor


def test_process_pool_variant_support_is_importable_and_picklable() -> None:
    restored = pickle.loads(pickle.dumps(support.make_image_variants_sync))

    assert restored is support.make_image_variants_sync
    assert restored.__module__ == "app.tasks.generation_parts.composition_support"
    assert support.IMAGE_POSTPROCESS_MODES == postprocess._IMAGE_POSTPROCESS_MODES


@pytest.mark.asyncio
async def test_variant_orchestration_uses_late_bound_support_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _variant_bundle(b"late-bound", engine="facade")
    reset_calls: list[None] = []

    def resolve_mode(_mode: str | None = None) -> str:
        return "inline"

    def get_executor(_runtime: object) -> Executor:
        raise AssertionError("executor should not be called by support probe")

    def reset_executor(_runtime: object) -> None:
        reset_calls.append(None)

    def make_variants(_raw_image: bytes) -> artifacts.ImageVariantBundle:
        return result

    def make_pil_variants(_raw_image: bytes) -> artifacts.ImageVariantBundle:
        return result

    async def extracted(
        raw_image: bytes,
        *,
        mode: str | None,
        hooks: postprocess.ImageVariantExecutionHooks,
        logger: Any,
    ) -> tuple[artifacts.ImageVariantBundle, str]:
        assert raw_image == b"raw"
        assert mode == "thread"
        assert logger is support.logger
        assert hooks.resolve_mode is resolve_mode
        with pytest.raises(
            AssertionError,
            match="executor should not be called",
        ):
            hooks.get_executor()
        hooks.reset_executor()
        assert hooks.make_variants_sync is make_variants
        assert hooks.make_variants_pil_only_sync is make_pil_variants
        assert hooks.broken_process_pool_type is BrokenProcessPool
        return result, "thread"

    monkeypatch.setattr(support, "resolve_image_postprocess_mode", resolve_mode)
    monkeypatch.setattr(
        support,
        "get_image_postprocess_executor",
        get_executor,
    )
    monkeypatch.setattr(
        support,
        "reset_image_postprocess_executor",
        reset_executor,
    )
    monkeypatch.setattr(support, "make_image_variants_sync", make_variants)
    monkeypatch.setattr(
        support,
        "make_image_variants_pil_only_sync",
        make_pil_variants,
    )
    monkeypatch.setattr(postprocess, "_postprocess_image_variants", extracted)

    variants, mode = await support.postprocess_image_variants(
        b"raw",
        mode="thread",
        runtime=postprocess_runtime,
    )

    assert variants is result
    assert mode == "thread"
    assert reset_calls == [None]


@pytest.mark.asyncio
async def test_raw_postprocess_uses_late_bound_support_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _png_bytes()
    variants = _variant_bundle(b"processed", engine="facade")
    expected = artifacts.PostprocessedGeneratedImage(
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
    ) -> artifacts.GeneratedImageInspection:
        return artifacts.GeneratedImageInspection("PNG", 1, 1, False)

    def sha256(_raw_image: bytes) -> str:
        return "sha"

    async def transparent_request(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("transparent hook should not run by support probe")

    def sanitize(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def process_variants(
        _raw_image: bytes,
        *,
        mode: str | None = None,
        runtime: object,
    ) -> tuple[artifacts.ImageVariantBundle, str]:
        assert runtime is postprocess_runtime
        return variants, mode or "inline"

    def compute_blurhash(_image: PILImage.Image) -> str:
        return "blur"

    def decode_error(exc: Exception) -> UpstreamError:
        return UpstreamError(str(exc))

    async def extracted(
        raw_image: bytes,
        *,
        prompt: str,
        transparent_requested: bool,
        mode: str | None,
        hooks: postprocess.GeneratedImagePostprocessHooks,
    ) -> artifacts.PostprocessedGeneratedImage:
        assert raw_image == raw
        assert prompt == "prompt"
        assert transparent_requested is True
        assert mode == "inline"
        assert hooks.inspect_generated_image_sync is inspect
        assert hooks.sha256 is sha256
        assert hooks.process_transparent_request is transparent_request
        assert hooks.transparent_pipeline_failure_type is TransparentPipelineFailure
        assert hooks.sanitize_transparent_qc_payload is sanitize
        variant_result, variant_mode = await hooks.postprocess_image_variants(
            raw_image,
            mode=mode,
        )
        assert variant_result is variants
        assert variant_mode == "inline"
        assert hooks.compute_blurhash is compute_blurhash
        assert hooks.image_decode_upstream_error is decode_error
        assert hooks.upstream_error_type is UpstreamError
        assert hooks.bad_response_error_code == EC.BAD_RESPONSE.value
        assert (
            hooks.generated_image_inspection_type is artifacts.GeneratedImageInspection
        )
        return expected

    monkeypatch.setattr(support.artifacts, "inspect_generated_image_sync", inspect)
    monkeypatch.setattr(support.artifacts, "sha256", sha256)
    monkeypatch.setattr(
        support,
        "process_transparent_request",
        transparent_request,
    )
    monkeypatch.setattr(
        support,
        "sanitize_transparent_qc_payload",
        sanitize,
    )
    monkeypatch.setattr(
        support,
        "postprocess_image_variants",
        process_variants,
    )
    monkeypatch.setattr(support.artifacts, "compute_blurhash", compute_blurhash)
    monkeypatch.setattr(support, "image_decode_upstream_error", decode_error)
    monkeypatch.setattr(postprocess, "_postprocess_raw_generated_image", extracted)

    result = await support.postprocess_raw_generated_image(
        raw,
        prompt="prompt",
        transparent_requested=True,
        mode="inline",
        runtime=postprocess_runtime,
    )

    assert result is expected
