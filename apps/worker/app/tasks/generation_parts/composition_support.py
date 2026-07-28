"""Explicit image/reference adapters used by Generation composition."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import (
    EXPLICIT_ALIGN,
    MAX_EXPLICIT_ASPECT,
    MAX_EXPLICIT_PIXELS,
    MAX_EXPLICIT_SIDE,
    MIN_EXPLICIT_PIXELS,
    GenerationErrorCode as EC,
)

from ...background_removal import (
    TransparentPipelineFailure,
    process_transparent_request,
)
from ...provider_runtime.errors import UpstreamError
from ...provider_runtime.upstream_services import ImageUpstreamRuntime
from . import image_artifact_contracts as artifacts
from . import postprocess
from . import references
from .retry_state import sanitize_transparent_qc_payload
from .runtime import ImagePostprocessRuntime


logger = logging.getLogger(__name__)
IMAGE_POSTPROCESS_MODES = frozenset({"inline", "thread", "process_pool"})
MASK_MAX_BYTES = 50 * 1024 * 1024


async def resolve_image_primary_route(
    runtime: ImageUpstreamRuntime,
) -> str:
    from ...upstream_parts.upstream_impl import resolve_image_primary_route

    return await resolve_image_primary_route(runtime=runtime)


def make_image_variants_sync(raw_image: bytes) -> artifacts.ImageVariantBundle:
    inspection = artifacts.inspect_generated_image_sync(raw_image)
    try:
        return artifacts.make_variants_with_vips_sync(raw_image, inspection)
    except Exception:
        return artifacts.make_variants_with_pil_sync(raw_image, inspection)


def make_image_variants_pil_only_sync(
    raw_image: bytes,
) -> artifacts.ImageVariantBundle:
    return artifacts.make_variants_with_pil_sync(raw_image)


def resolve_image_postprocess_mode(mode: str | None = None) -> str:
    return postprocess._resolve_image_postprocess_mode(
        mode,
        environ=os.environ,
        allowed_modes=IMAGE_POSTPROCESS_MODES,
        logger=logger,
    )


def resolve_image_postprocess_workers() -> int:
    return postprocess._resolve_image_postprocess_workers(
        environ=os.environ,
        cpu_count=os.cpu_count,
    )


def get_image_postprocess_executor(
    runtime: ImagePostprocessRuntime,
) -> ProcessPoolExecutor:
    runtime.executor = postprocess._get_image_postprocess_executor(
        runtime.executor,
        resolve_workers=resolve_image_postprocess_workers,
        executor_type=ProcessPoolExecutor,
    )
    return runtime.executor


def reset_image_postprocess_executor(
    runtime: ImagePostprocessRuntime,
) -> None:
    executor = runtime.executor
    runtime.executor = None
    postprocess._reset_image_postprocess_executor(executor)


async def postprocess_image_variants(
    raw_image: bytes,
    *,
    mode: str | None = None,
    runtime: ImagePostprocessRuntime,
) -> tuple[artifacts.ImageVariantBundle, str]:
    return await postprocess._postprocess_image_variants(
        raw_image,
        mode=mode,
        hooks=postprocess.ImageVariantExecutionHooks(
            resolve_mode=resolve_image_postprocess_mode,
            get_executor=lambda: get_image_postprocess_executor(runtime),
            reset_executor=lambda: reset_image_postprocess_executor(runtime),
            make_variants_sync=make_image_variants_sync,
            make_variants_pil_only_sync=make_image_variants_pil_only_sync,
            broken_process_pool_type=BrokenProcessPool,
        ),
        logger=logger,
    )


def image_decode_upstream_error(exc: Exception) -> UpstreamError:
    return postprocess._image_decode_upstream_error(
        exc,
        upstream_error_type=UpstreamError,
        bad_response_error_code=EC.BAD_RESPONSE.value,
    )


async def postprocess_raw_generated_image(
    raw_image: bytes,
    *,
    prompt: str,
    transparent_requested: bool,
    mode: str | None = None,
    runtime: ImagePostprocessRuntime,
) -> artifacts.PostprocessedGeneratedImage:
    return await postprocess._postprocess_raw_generated_image(
        raw_image,
        prompt=prompt,
        transparent_requested=transparent_requested,
        mode=mode,
        hooks=postprocess.GeneratedImagePostprocessHooks(
            inspect_generated_image_sync=artifacts.inspect_generated_image_sync,
            sha256=artifacts.sha256,
            process_transparent_request=process_transparent_request,
            transparent_pipeline_failure_type=TransparentPipelineFailure,
            sanitize_transparent_qc_payload=sanitize_transparent_qc_payload,
            postprocess_image_variants=lambda image, *, mode=None: (
                postprocess_image_variants(
                    image,
                    mode=mode,
                    runtime=runtime,
                )
            ),
            compute_blurhash=artifacts.compute_blurhash,
            image_decode_upstream_error=image_decode_upstream_error,
            upstream_error_type=UpstreamError,
            bad_response_error_code=EC.BAD_RESPONSE.value,
            generated_image_inspection_type=artifacts.GeneratedImageInspection,
        ),
    )


async def load_reference_images(
    session: AsyncSession,
    image_ids: list[str],
    *,
    storage_backend: references.ReferenceBlobStore,
) -> list[tuple[str, bytes]]:
    return await references.load_reference_images(
        session,
        image_ids,
        storage=storage_backend,
        log=logger,
    )


async def load_mask_image(
    session: AsyncSession,
    mask_image_id: str,
    *,
    storage_backend: references.ReferenceBlobStore,
) -> bytes:
    return await references.load_mask_image(
        session,
        mask_image_id,
        storage=storage_backend,
        max_bytes=MASK_MAX_BYTES,
    )


def resize_mask_to_reference(
    mask_bytes: bytes,
    reference_bytes: bytes,
) -> bytes:
    return references.resize_mask_to_reference(mask_bytes, reference_bytes)


def reference_pixel_size(
    reference_bytes: bytes,
) -> tuple[int, int] | None:
    return references.reference_pixel_size(reference_bytes)


def inpaint_size_from_reference(
    reference_width: int,
    reference_height: int,
) -> str | None:
    return references.inpaint_size_from_reference(
        reference_width,
        reference_height,
        explicit_align=EXPLICIT_ALIGN,
        max_explicit_aspect=MAX_EXPLICIT_ASPECT,
        max_explicit_pixels=MAX_EXPLICIT_PIXELS,
        max_explicit_side=MAX_EXPLICIT_SIDE,
        min_explicit_pixels=MIN_EXPLICIT_PIXELS,
    )


__all__ = [
    "get_image_postprocess_executor",
    "inpaint_size_from_reference",
    "load_mask_image",
    "load_reference_images",
    "postprocess_image_variants",
    "postprocess_raw_generated_image",
    "reference_pixel_size",
    "reset_image_postprocess_executor",
    "resize_mask_to_reference",
    "resolve_image_primary_route",
]
