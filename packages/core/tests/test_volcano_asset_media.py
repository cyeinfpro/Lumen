from __future__ import annotations

import asyncio
import hashlib
import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PILImage

from lumen_core import volcano_asset_media
from lumen_core.models import Image, ImageVariant, Video


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _NestedTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self, *results: object) -> None:
        self._results = iter(results)
        self.commit = AsyncMock()
        self.added: list[object] = []

    async def execute(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(next(self._results))

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


def _jpeg_bytes(size: tuple[int, int] = (300, 300)) -> bytes:
    output = io.BytesIO()
    PILImage.new("RGB", size, color=(120, 80, 40)).save(output, format="JPEG")
    return output.getvalue()


def _image(storage_key: str) -> Image:
    return Image(
        id="image-1",
        user_id="user-1",
        source="uploaded",
        storage_key=storage_key,
        mime="image/png",
        width=300,
        height=300,
        size_bytes=100,
        sha256="0" * 64,
        visibility="private",
        metadata_jsonb={},
    )


def _video(storage_key: str, metadata: dict[str, object]) -> Video:
    return Video(
        id="video-1",
        user_id="user-1",
        storage_key=storage_key,
        mime="video/mp4",
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        size_bytes=100,
        sha256="0" * 64,
        etag="video-1",
        has_audio=False,
        faststart=True,
        visibility="private",
        metadata_jsonb=metadata,
    )


def test_import_does_not_eager_load_media_or_change_pillow_global() -> None:
    script = """
import sys
from PIL import Image
before = Image.MAX_IMAGE_PIXELS
import lumen_core
assert "lumen_core.volcano_asset_media" not in sys.modules
import lumen_core.volcano_asset_media
assert Image.MAX_IMAGE_PIXELS == before
"""

    subprocess.run([sys.executable, "-c", script], check=True)


def test_atomic_install_replaces_different_content(tmp_path: Path) -> None:
    destination = tmp_path / "variant.bin"
    destination.write_bytes(b"stale")
    expected = b"fresh"

    volcano_asset_media._install_file_atomic(
        destination,
        expected,
        sha256=hashlib.sha256(expected).hexdigest(),
    )

    assert destination.read_bytes() == expected


def test_atomic_install_detects_a_competing_different_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "variant.bin"
    expected = b"fresh"
    original_replace = volcano_asset_media.os.replace

    def replace_then_overwrite(source: Path, target: Path) -> None:
        original_replace(source, target)
        target.write_bytes(b"competing-content")

    monkeypatch.setattr(volcano_asset_media.os, "replace", replace_then_overwrite)

    with pytest.raises(volcano_asset_media.VolcanoAssetMediaError) as exc_info:
        volcano_asset_media._install_file_atomic(
            destination,
            expected,
            sha256=hashlib.sha256(expected).hexdigest(),
        )

    assert exc_info.value.code == "volcano_asset_media_storage_conflict"


@pytest.mark.asyncio
async def test_image_variant_repairs_invalid_existing_file_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_key = "images/source.png"
    source = tmp_path / storage_key
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    image = _image(storage_key)
    variant_key = volcano_asset_media.volcano_asset_image_key(image)
    destination = tmp_path / variant_key
    destination.write_bytes(b"not-a-jpeg")
    existing = ImageVariant(
        id="variant-1",
        image_id=image.id,
        kind=volcano_asset_media.VOLCANO_ASSET_IMAGE_KIND,
        storage_key=variant_key,
        width=300,
        height=300,
    )
    rendered_data = _jpeg_bytes()
    rendered = volcano_asset_media.VolcanoAssetImageJpeg(
        data=rendered_data,
        width=300,
        height=300,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_image_jpeg",
        lambda _source: rendered,
    )
    session = _Session(image, existing, image, existing)

    result = await volcano_asset_media.ensure_volcano_asset_image_variant(
        session,
        image,
        storage_root=str(tmp_path),
    )

    assert result is existing
    assert destination.read_bytes() == rendered_data
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_variant_repairs_hash_mismatch_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_key = "videos/source.mp4"
    source = tmp_path / storage_key
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    stale_data = b"stale-video"
    video = _video(storage_key, {})
    variant_key = volcano_asset_media.volcano_asset_video_key(video)
    destination = tmp_path / variant_key
    destination.write_bytes(stale_data)
    video.metadata_jsonb = {
        volcano_asset_media.VOLCANO_ASSET_VIDEO_METADATA_KEY: {
            "kind": volcano_asset_media.VOLCANO_ASSET_VIDEO_KIND,
            "storage_key": variant_key,
            "mime": volcano_asset_media.VOLCANO_ASSET_VIDEO_MIME,
            "width": 1280,
            "height": 720,
            "duration_ms": 2_000,
            "fps": 30.0,
            "has_audio": False,
            "size_bytes": len(stale_data),
            "sha256": hashlib.sha256(b"different-video").hexdigest(),
        }
    }
    rendered_data = b"fresh-video"
    rendered = volcano_asset_media.VolcanoAssetVideoMp4(
        data=rendered_data,
        width=1280,
        height=720,
        duration_ms=2_000,
        fps=30.0,
        has_audio=False,
        size_bytes=len(rendered_data),
        sha256=hashlib.sha256(rendered_data).hexdigest(),
    )
    monkeypatch.setattr(
        volcano_asset_media,
        "make_volcano_asset_video_mp4",
        lambda _source: rendered,
    )
    session = _Session(video, video)

    result = await volcano_asset_media.ensure_volcano_asset_video_variant(
        session,
        video,
        storage_root=str(tmp_path),
    )

    assert result["sha256"] == rendered.sha256
    assert destination.read_bytes() == rendered_data
    assert (
        video.metadata_jsonb[volcano_asset_media.VOLCANO_ASSET_VIDEO_METADATA_KEY]
        == result
    )
    session.commit.assert_not_awaited()


def test_video_transcode_semaphore_is_scoped_to_running_loop() -> None:
    async def capture() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
        return (
            volcano_asset_media._video_transcode_semaphore(),
            volcano_asset_media._video_transcode_semaphore(),
        )

    first, first_again = asyncio.run(capture())
    second, second_again = asyncio.run(capture())

    assert first is first_again
    assert second is second_again
    assert first is not second
