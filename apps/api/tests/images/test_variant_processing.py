from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Literal

import pytest
from PIL import Image as PILImage

from app.images.ports.image_processing import ImageVariantProcessingRequest
from app.images.processing import isolated as isolated_module
from app.images.processing.isolated import IsolatedImageProcessingExecutor
from app.images.processing.service import ProcessingError
from app.images.processing.variants import render_image_variant


def test_display_webp_dimensions_mime_and_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "display.webp"
    PILImage.new("RGB", (320, 160), (10, 20, 30)).save(source)

    prepared = render_image_variant(
        ImageVariantProcessingRequest(
            source_path=source,
            output_path=output,
            variant="display_webp",
            max_pixels=1_000_000,
            max_side=128,
        )
    )

    assert prepared.output_path == output
    assert prepared.mime == "image/webp"
    assert (prepared.width, prepared.height) == (128, 64)
    assert prepared.size_bytes == output.stat().st_size
    assert prepared.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    with PILImage.open(output) as rendered:
        assert rendered.format == "WEBP"
        assert rendered.mode == "RGB"
        assert rendered.size == (128, 64)


def test_video_reference_jpeg_flattens_transparency_to_white(
    tmp_path: Path,
) -> None:
    source = tmp_path / "transparent.png"
    output = tmp_path / "video-reference.jpg"
    PILImage.new("RGBA", (64, 32), (255, 0, 0, 0)).save(source)

    prepared = render_image_variant(
        ImageVariantProcessingRequest(
            source_path=source,
            output_path=output,
            variant="video_reference_jpeg",
            max_pixels=1_000_000,
            max_side=2048,
        )
    )

    assert prepared.mime == "image/jpeg"
    assert (prepared.width, prepared.height) == (64, 32)
    with PILImage.open(output) as rendered:
        assert rendered.format == "JPEG"
        assert rendered.mode == "RGB"
        assert rendered.getpixel((32, 16)) == (255, 255, 255)


@pytest.mark.parametrize(
    ("variant", "suffix"),
    [
        ("display_webp", ".webp"),
        ("video_reference_jpeg", ".jpg"),
    ],
)
def test_variant_pixel_limit_error(
    tmp_path: Path,
    variant: Literal["display_webp", "video_reference_jpeg"],
    suffix: str,
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / f"output{suffix}"
    PILImage.new("RGB", (10, 10), (10, 20, 30)).save(source)

    with pytest.raises(ProcessingError) as exc_info:
        render_image_variant(
            ImageVariantProcessingRequest(
                source_path=source,
                output_path=output,
                variant=variant,
                max_pixels=99,
                max_side=2048,
            )
        )

    assert exc_info.value.code == "too_many_pixels"
    assert exc_info.value.status_code == 413
    assert not output.exists()


@pytest.mark.asyncio
async def test_isolated_executor_renders_variant(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "isolated.webp"
    PILImage.new("RGB", (96, 48), (20, 40, 60)).save(source)
    executor = IsolatedImageProcessingExecutor()

    try:
        prepared = await executor.render_variant(
            ImageVariantProcessingRequest(
                source_path=source,
                output_path=output,
                variant="display_webp",
                max_pixels=1_000_000,
                max_side=64,
            )
        )
    finally:
        await executor.aclose()

    assert prepared.output_path == output
    assert prepared.mime == "image/webp"
    assert (prepared.width, prepared.height) == (64, 32)
    assert prepared.size_bytes == output.stat().st_size
    assert prepared.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_isolated_executor_cancellation_terminates_child_process(
    tmp_path: Path,
) -> None:
    source = tmp_path / "blocked-source"
    output = tmp_path / "cancelled.webp"
    os.mkfifo(source)
    executor = IsolatedImageProcessingExecutor()
    task = asyncio.create_task(
        executor.render_variant(
            ImageVariantProcessingRequest(
                source_path=source,
                output_path=output,
                variant="display_webp",
                max_pixels=1_000_000,
                max_side=64,
            )
        )
    )

    async def wait_for_child() -> None:
        while not executor._active:  # noqa: SLF001
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_child(), timeout=2)
        process = next(iter(executor._active))  # noqa: SLF001
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        await executor.aclose()

    assert not process.is_alive()
    assert executor._active == set()  # noqa: SLF001
    assert not output.exists()


def test_isolated_executor_result_timeout_constructor_override() -> None:
    """构造参数覆盖模块默认超时;缺省时仍读模块常量(monkeypatch 兼容)。"""
    assert (
        IsolatedImageProcessingExecutor(result_timeout_seconds=90.5)
        ._result_timeout_seconds  # noqa: SLF001
        == 90.5
    )
    assert (
        IsolatedImageProcessingExecutor()._result_timeout_seconds  # noqa: SLF001
        == isolated_module._PROCESS_RESULT_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_isolated_executor_result_timeout_releases_child_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolated_module, "_PROCESS_RESULT_TIMEOUT_SECONDS", 0.25)
    source = tmp_path / "blocked-source"
    output = tmp_path / "timeout.webp"
    os.mkfifo(source)
    executor = IsolatedImageProcessingExecutor()
    task = asyncio.create_task(
        executor.render_variant(
            ImageVariantProcessingRequest(
                source_path=source,
                output_path=output,
                variant="display_webp",
                max_pixels=1_000_000,
                max_side=64,
            )
        )
    )

    async def wait_for_child() -> None:
        while not executor._active:  # noqa: SLF001
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(wait_for_child(), timeout=2)
        process = next(iter(executor._active))  # noqa: SLF001
        with pytest.raises(ProcessingError) as exc_info:
            await asyncio.wait_for(task, timeout=5)
    finally:
        await executor.aclose()

    assert exc_info.value.code == "image_processing_timeout"
    assert exc_info.value.status_code == 503
    assert not process.is_alive()
    assert executor._active == set()  # noqa: SLF001
    assert not output.exists()
