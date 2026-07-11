"""Generated-image postprocess execution and orchestration."""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from PIL import Image as PILImage

from lumen_core.constants import GenerationErrorCode as EC

from ... import image_artifacts
from ...background_removal import (
    TransparentPipelineFailure,
    TransparentPipelineOutput,
)
from ...upstream import UpstreamError


_IMAGE_POSTPROCESS_MODES = {"inline", "thread", "process_pool"}

ImageVariantMaker = Callable[[bytes], image_artifacts._ImageVariantBundle]


@dataclass(frozen=True)
class ImageVariantExecutionHooks:
    resolve_mode: Callable[[str | None], str]
    get_executor: Callable[[], Executor]
    reset_executor: Callable[[], None]
    make_variants_sync: ImageVariantMaker
    make_variants_pil_only_sync: ImageVariantMaker
    broken_process_pool_type: type[BrokenProcessPool]


@dataclass(frozen=True)
class GeneratedImagePostprocessHooks:
    inspect_generated_image_sync: Callable[
        [bytes], image_artifacts._GeneratedImageInspection
    ]
    sha256: Callable[[bytes], str]
    process_transparent_request: Callable[..., Awaitable[TransparentPipelineOutput]]
    transparent_pipeline_failure_type: type[TransparentPipelineFailure]
    sanitize_transparent_qc_payload: Callable[[dict[str, Any]], dict[str, Any]]
    postprocess_image_variants: Callable[
        ...,
        Awaitable[tuple[image_artifacts._ImageVariantBundle, str]],
    ]
    compute_blurhash: Callable[[PILImage.Image], str | None]
    image_decode_upstream_error: Callable[[Exception], UpstreamError]
    upstream_error_type: Callable[..., UpstreamError]
    bad_response_error_code: str
    generated_image_inspection_type: Callable[
        ..., image_artifacts._GeneratedImageInspection
    ]
    postprocessed_generated_image_type: Callable[
        ..., image_artifacts._PostprocessedGeneratedImage
    ]


def _resolve_image_postprocess_mode(
    mode: str | None = None,
    *,
    environ: Mapping[str, str],
    allowed_modes: Collection[str],
    logger: logging.Logger,
) -> str:
    raw_mode = mode or environ.get("IMAGE_POSTPROCESS_MODE") or "process_pool"
    normalized = raw_mode.strip().lower().replace("-", "_")
    if normalized not in allowed_modes:
        logger.warning(
            "invalid IMAGE_POSTPROCESS_MODE=%s; using process_pool", raw_mode
        )
        return "process_pool"
    return normalized


def _resolve_image_postprocess_workers(
    *,
    environ: Mapping[str, str],
    cpu_count: Callable[[], int | None],
) -> int:
    raw_workers = environ.get("IMAGE_POSTPROCESS_WORKERS")
    if raw_workers:
        try:
            workers = int(raw_workers)
        except ValueError:
            workers = 0
        if workers > 0:
            return min(workers, 8)
    available_cpus = cpu_count() or 2
    return max(1, min(4, available_cpus // 2 or 1))


def _get_image_postprocess_executor(
    executor: ProcessPoolExecutor | None,
    *,
    resolve_workers: Callable[[], int],
    executor_type: Callable[..., ProcessPoolExecutor],
) -> ProcessPoolExecutor:
    if executor is None:
        executor = executor_type(max_workers=resolve_workers())
    return executor


def _reset_image_postprocess_executor(executor: Executor | None) -> None:
    if executor is not None:
        with suppress(Exception):
            executor.shutdown(wait=False, cancel_futures=True)


async def _postprocess_image_variants(
    raw_image: bytes,
    *,
    mode: str | None,
    hooks: ImageVariantExecutionHooks,
    logger: logging.Logger,
) -> tuple[image_artifacts._ImageVariantBundle, str]:
    executor_mode = hooks.resolve_mode(mode)
    if executor_mode == "inline":
        return hooks.make_variants_sync(raw_image), "inline"
    if executor_mode == "thread":
        return (
            await asyncio.to_thread(hooks.make_variants_sync, raw_image),
            "thread",
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            hooks.get_executor(),
            hooks.make_variants_sync,
            raw_image,
        )
        return result, "process_pool"
    except hooks.broken_process_pool_type as exc:
        hooks.reset_executor()
        logger.warning(
            "image postprocess process_pool broke; reset executor and falling back "
            "to PIL thread err=%s",
            exc,
        )
        return (
            await asyncio.to_thread(
                hooks.make_variants_pil_only_sync,
                raw_image,
            ),
            "thread",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "image postprocess process_pool failed; falling back to PIL thread err=%s",
            exc,
        )
        return (
            await asyncio.to_thread(
                hooks.make_variants_pil_only_sync,
                raw_image,
            ),
            "thread",
        )


def _image_decode_upstream_error(
    exc: Exception,
    *,
    upstream_error_type: Callable[..., UpstreamError],
    bad_response_error_code: str = EC.BAD_RESPONSE.value,
) -> UpstreamError:
    message = str(exc)
    if not (
        message.startswith("upstream returned unexpected image format")
        or message.startswith("upstream image dimensions out of range")
    ):
        message = f"pillow could not decode image: {exc}"
    return upstream_error_type(
        message,
        error_code=bad_response_error_code,
        status_code=200,
    )


async def _postprocess_raw_generated_image(
    raw_image: bytes,
    *,
    prompt: str,
    transparent_requested: bool,
    mode: str | None,
    hooks: GeneratedImagePostprocessHooks,
) -> image_artifacts._PostprocessedGeneratedImage:
    try:
        inspection = await asyncio.to_thread(
            hooks.inspect_generated_image_sync,
            raw_image,
        )
    except Exception as exc:  # noqa: BLE001
        raise hooks.image_decode_upstream_error(exc) from exc

    sha = hooks.sha256(raw_image)
    transparent_alpha_recovered = False
    transparent_qc_payload: dict[str, Any] | None = None
    transparent_provider: str | None = None

    if transparent_requested and not inspection.has_transparency:
        try:
            with PILImage.open(io.BytesIO(raw_image)) as pil:
                pil.load()
                pipeline_out = await hooks.process_transparent_request(
                    pil,
                    prompt=prompt,
                )
        except hooks.transparent_pipeline_failure_type as caught:
            failure = cast(TransparentPipelineFailure, caught)
            qc_dict = (
                hooks.sanitize_transparent_qc_payload(failure.qc.to_dict())
                if failure.qc is not None
                else None
            )
            raise hooks.upstream_error_type(
                f"transparent material pipeline failed: {failure}",
                error_code=hooks.bad_response_error_code,
                status_code=200,
                payload={
                    "transparent_qc": qc_dict,
                    "transparent_provider": failure.provider,
                },
            ) from failure
        except Exception as decode_exc:  # noqa: BLE001
            raise hooks.image_decode_upstream_error(decode_exc) from decode_exc
        raw_image = pipeline_out.rgba_png
        sha = hooks.sha256(raw_image)
        inspection = hooks.generated_image_inspection_type(
            orig_format="PNG",
            width=pipeline_out.width,
            height=pipeline_out.height,
            has_transparency=True,
        )
        transparent_alpha_recovered = True
        transparent_qc_payload = hooks.sanitize_transparent_qc_payload(
            pipeline_out.qc.to_dict()
        )
        transparent_provider = pipeline_out.provider

    try:
        variants, executor_mode = await hooks.postprocess_image_variants(
            raw_image,
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001
        raise hooks.image_decode_upstream_error(exc) from exc

    try:
        with PILImage.open(io.BytesIO(raw_image)) as pil:
            pil.load()
            blurhash_str = hooks.compute_blurhash(pil)
    except Exception as exc:  # noqa: BLE001
        raise hooks.image_decode_upstream_error(exc) from exc

    return hooks.postprocessed_generated_image_type(
        raw_image=raw_image,
        sha256=sha,
        orig_format=variants.orig_format,
        width=variants.width,
        height=variants.height,
        blurhash=blurhash_str,
        display=variants.display,
        preview=variants.preview,
        thumb=variants.thumb,
        transparent_alpha_recovered=transparent_alpha_recovered,
        transparent_qc_payload=transparent_qc_payload,
        transparent_provider=transparent_provider,
        engine=variants.engine,
        executor_mode=executor_mode,
    )
