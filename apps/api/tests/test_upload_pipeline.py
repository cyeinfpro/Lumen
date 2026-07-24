from __future__ import annotations

import asyncio
import errno
import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image as PILImage

from app.services import upload_pipeline


class _Upload:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, _size: int) -> bytes:
        return next(self._chunks, b"")


def _png_bytes(size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", size, color=(10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


def _rgba_png_bytes(size: tuple[int, int], alpha: int = 0) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGBA", size, color=(255, 255, 255, alpha)).save(
        output,
        format="PNG",
    )
    return output.getvalue()


def _jpeg_bytes(size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", size, color=(30, 60, 90)).save(output, format="JPEG")
    return output.getvalue()


def _png_header(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, payload: bytes) -> bytes:
        contents = kind + payload
        return (
            struct.pack(">I", len(payload))
            + contents
            + struct.pack(">I", zlib.crc32(contents) & 0xFFFFFFFF)
        )

    return b"".join(
        (
            signature,
            chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
            ),
            chunk(b"IDAT", zlib.compress(b"")),
            chunk(b"IEND", b""),
        )
    )


def _limits(
    *,
    concurrency: int = 2,
    inflight_bytes: int = 10_000_000,
    inflight_pixels: int = 10_000_000,
) -> upload_pipeline.UploadBudgetLimits:
    return upload_pipeline.UploadBudgetLimits(
        max_concurrency=concurrency,
        max_inflight_bytes=inflight_bytes,
        max_inflight_pixels=inflight_pixels,
    )


def test_upload_budget_limits_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUMEN_IMAGE_UPLOAD_MAX_CONCURRENCY", "7")
    monkeypatch.setenv("LUMEN_IMAGE_UPLOAD_MAX_INFLIGHT_BYTES", "12345")
    monkeypatch.setenv("LUMEN_IMAGE_UPLOAD_MAX_INFLIGHT_PIXELS", "67890")

    limits = upload_pipeline.UploadBudgetLimits.from_env()

    assert limits == upload_pipeline.UploadBudgetLimits(
        max_concurrency=7,
        max_inflight_bytes=12345,
        max_inflight_pixels=67890,
    )


@pytest.mark.asyncio
async def test_concurrent_upload_admission_is_process_bounded(
    tmp_path: Path,
) -> None:
    budget = upload_pipeline.UploadBudget(_limits(concurrency=1))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_first() -> None:
        async with upload_pipeline.stage_upload(
            _Upload([b"first"]),
            storage_root=tmp_path,
            max_bytes=100,
            budget=budget,
        ):
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold_first())
    await entered.wait()
    with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
        async with upload_pipeline.stage_upload(
            _Upload([b"second"]),
            storage_root=tmp_path,
            max_bytes=100,
            budget=budget,
        ):
            pass
    assert exc_info.value.code == "upload_capacity_exceeded"
    assert exc_info.value.status_code == 503
    release.set()
    await first
    assert budget.snapshot() == upload_pipeline.UploadBudgetSnapshot(0, 0, 0)


@pytest.mark.asyncio
async def test_inflight_byte_budget_rejects_and_cleans_temp_file(
    tmp_path: Path,
) -> None:
    budget = upload_pipeline.UploadBudget(_limits(inflight_bytes=8))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_first() -> None:
        async with upload_pipeline.stage_upload(
            _Upload([b"123456"]),
            storage_root=tmp_path,
            max_bytes=100,
            budget=budget,
        ):
            entered.set()
            await release.wait()

    first = asyncio.create_task(hold_first())
    await entered.wait()
    with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
        async with upload_pipeline.stage_upload(
            _Upload([b"abc"]),
            storage_root=tmp_path,
            max_bytes=100,
            budget=budget,
        ):
            pass
    assert exc_info.value.code == "upload_bytes_capacity_exceeded"
    release.set()
    await first
    assert list((tmp_path / ".upload-tmp").iterdir()) == []
    assert budget.snapshot() == upload_pipeline.UploadBudgetSnapshot(0, 0, 0)


@pytest.mark.asyncio
async def test_per_file_limit_rejects_and_cleans_temp_file(tmp_path: Path) -> None:
    budget = upload_pipeline.UploadBudget(_limits())
    with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
        async with upload_pipeline.stage_upload(
            _Upload([b"1234", b"5678"]),
            storage_root=tmp_path,
            max_bytes=7,
            budget=budget,
        ):
            pass
    assert exc_info.value.code == "too_large"
    assert list((tmp_path / ".upload-tmp").iterdir()) == []
    assert budget.snapshot() == upload_pipeline.UploadBudgetSnapshot(0, 0, 0)


@pytest.mark.asyncio
async def test_pixel_budget_rejects_second_inflight_decode(tmp_path: Path) -> None:
    payload = _png_bytes((10, 10))
    budget = upload_pipeline.UploadBudget(_limits(inflight_pixels=150))
    async with upload_pipeline.stage_upload(
        _Upload([payload]),
        storage_root=tmp_path,
        max_bytes=100_000,
        budget=budget,
    ) as first:
        upload_pipeline.prepare_image_upload(
            first,
            "first.png",
            allowed_mime={"image/png"},
            normalizable_mime=set(),
            max_bytes=100_000,
            max_pixels=10_000,
            max_long_side=100,
        )
        async with upload_pipeline.stage_upload(
            _Upload([payload]),
            storage_root=tmp_path,
            max_bytes=100_000,
            budget=budget,
        ) as second:
            with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
                upload_pipeline.prepare_image_upload(
                    second,
                    "second.png",
                    allowed_mime={"image/png"},
                    normalizable_mime=set(),
                    max_bytes=100_000,
                    max_pixels=10_000,
                    max_long_side=100,
                )
    assert exc_info.value.code == "upload_pixels_capacity_exceeded"
    assert budget.snapshot() == upload_pipeline.UploadBudgetSnapshot(0, 0, 0)


@pytest.mark.asyncio
async def test_prepare_preserves_original_and_builds_bounded_reference(
    tmp_path: Path,
) -> None:
    payload = _png_bytes((3000, 1500))
    budget = upload_pipeline.UploadBudget(_limits(inflight_pixels=10_000_000))
    async with upload_pipeline.stage_upload(
        _Upload([payload]),
        storage_root=tmp_path,
        max_bytes=10_000_000,
        budget=budget,
    ) as staged:
        prepared = upload_pipeline.prepare_image_upload(
            staged,
            "reference.png",
            allowed_mime={"image/png"},
            normalizable_mime=set(),
            max_bytes=10_000_000,
            max_pixels=10_000_000,
            max_long_side=4096,
        )
        assert prepared.original_path.read_bytes() == payload
        assert prepared.mime == "image/png"
        assert (prepared.width, prepared.height) == (3000, 1500)
        assert prepared.metadata["mask_preflight"]["has_alpha"] is False
        assert prepared.normalized_ref_meta["mime"] == "image/webp"
        assert prepared.normalized_ref_meta["width"] == 2048
        assert prepared.normalized_ref_meta["height"] == 1024
        assert prepared.normalized_ref_meta["bytes"] == (
            prepared.normalized_ref_path.stat().st_size
        )
        assert len(prepared.normalized_ref_meta["sha256"]) == 64


@pytest.mark.asyncio
async def test_prepare_rejects_long_side_and_allows_explicit_larger_cap(
    tmp_path: Path,
) -> None:
    payload = _png_bytes((5000, 100))
    budget = upload_pipeline.UploadBudget(_limits(inflight_pixels=1_000_000))
    async with upload_pipeline.stage_upload(
        _Upload([payload]),
        storage_root=tmp_path,
        max_bytes=1_000_000,
        budget=budget,
    ) as staged:
        with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
            upload_pipeline.prepare_image_upload(
                staged,
                "wide.png",
                allowed_mime={"image/png"},
                normalizable_mime=set(),
                max_bytes=1_000_000,
                max_pixels=1_000_000,
                max_long_side=4096,
            )
        assert exc_info.value.code == "too_large"

    async with upload_pipeline.stage_upload(
        _Upload([payload]),
        storage_root=tmp_path,
        max_bytes=1_000_000,
        budget=budget,
    ) as staged:
        prepared = upload_pipeline.prepare_image_upload(
            staged,
            "volcano-asset.png",
            allowed_mime={"image/png"},
            normalizable_mime=set(),
            max_bytes=1_000_000,
            max_pixels=1_000_000,
            max_long_side=8192,
        )
        assert (prepared.width, prepared.height) == (5000, 100)


@pytest.mark.asyncio
async def test_prepare_normalizes_mpo_to_jpeg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _jpeg_bytes((640, 480))
    original_image_mime_type = upload_pipeline._image_mime_type

    def fake_image_mime_type(image: PILImage.Image) -> str:
        mime = original_image_mime_type(image)
        return "image/mpo" if mime == "image/jpeg" else mime

    monkeypatch.setattr(
        upload_pipeline,
        "_image_mime_type",
        fake_image_mime_type,
    )
    budget = upload_pipeline.UploadBudget(_limits(inflight_pixels=1_000_000))
    async with upload_pipeline.stage_upload(
        _Upload([payload]),
        storage_root=tmp_path,
        max_bytes=1_000_000,
        budget=budget,
    ) as staged:
        prepared = upload_pipeline.prepare_image_upload(
            staged,
            "iphone-photo.mpo",
            allowed_mime={"image/jpeg"},
            normalizable_mime={"image/mpo"},
            max_bytes=1_000_000,
            max_pixels=1_000_000,
            max_long_side=4096,
        )
        assert prepared.mime == "image/jpeg"
        assert (prepared.width, prepared.height) == (640, 480)
        assert prepared.original_path.read_bytes() != payload
        assert prepared.metadata["upload_normalized"] == {
            "source_mime": "image/mpo",
            "target_mime": "image/jpeg",
            "reason": "unsupported_upload_mime",
        }
        with PILImage.open(prepared.original_path) as image:
            assert image.get_format_mimetype() == "image/jpeg"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reference_size", "expected_code"),
    [
        (_png_bytes((32, 32)), None, "invalid_mask_alpha"),
        (_rgba_png_bytes((32, 32)), (64, 64), "mask_size_mismatch"),
    ],
)
async def test_prepare_rejects_invalid_masks(
    tmp_path: Path,
    payload: bytes,
    reference_size: tuple[int, int] | None,
    expected_code: str,
) -> None:
    budget = upload_pipeline.UploadBudget(_limits(inflight_pixels=1_000_000))
    async with upload_pipeline.stage_upload(
        _Upload([payload]),
        storage_root=tmp_path,
        max_bytes=1_000_000,
        budget=budget,
    ) as staged:
        with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
            upload_pipeline.prepare_image_upload(
                staged,
                "mask.png",
                allowed_mime={"image/png"},
                normalizable_mime=set(),
                max_bytes=1_000_000,
                max_pixels=1_000_000,
                max_long_side=4096,
                mask_requested=True,
                reference_size=reference_size,
            )
    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_compression_bomb_header_maps_to_413_and_cleans(
    tmp_path: Path,
) -> None:
    budget = upload_pipeline.UploadBudget(
        _limits(inflight_pixels=20_000_000_000)
    )
    with pytest.raises(upload_pipeline.UploadPipelineError) as exc_info:
        async with upload_pipeline.stage_upload(
            _Upload([_png_header(100_000, 100_000)]),
            storage_root=tmp_path,
            max_bytes=100_000,
            budget=budget,
        ) as staged:
            upload_pipeline.prepare_image_upload(
                staged,
                "bomb.png",
                allowed_mime={"image/png"},
                normalizable_mime=set(),
                max_bytes=100_000,
                max_pixels=64_000_000,
                max_long_side=200_000,
            )
    assert exc_info.value.code == "too_many_pixels"
    assert exc_info.value.status_code == 413
    assert list((tmp_path / ".upload-tmp").iterdir()) == []
    assert budget.snapshot() == upload_pipeline.UploadBudgetSnapshot(0, 0, 0)


def test_publish_temp_file_does_not_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        upload_pipeline.publish_temp_file(source, destination)

    assert destination.read_bytes() == b"old"


def test_publish_temp_file_falls_back_without_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")

    def hardlink_unsupported(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EPERM, "hardlinks unavailable")

    monkeypatch.setattr(upload_pipeline.os, "link", hardlink_unsupported)
    upload_pipeline.publish_temp_file(source, destination)

    assert destination.read_bytes() == b"new"


def test_publish_fallback_removes_partial_destination_on_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")

    def hardlink_unsupported(_source: Path, _destination: Path) -> None:
        raise OSError(errno.EPERM, "hardlinks unavailable")

    def copy_failure(*_args, **_kwargs) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(upload_pipeline.os, "link", hardlink_unsupported)
    monkeypatch.setattr(upload_pipeline.shutil, "copyfileobj", copy_failure)

    with pytest.raises(OSError, match="copy failed"):
        upload_pipeline.publish_temp_file(source, destination)

    assert not destination.exists()
