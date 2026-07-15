from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image as PILImage

from app import volcano_asset_media


def _png(path: Path, size: tuple[int, int], color=(120, 80, 40)) -> None:
    PILImage.new("RGB", size, color=color).save(path, format="PNG")


def test_image_normalization_upscales_tiny_input(tmp_path: Path) -> None:
    source = tmp_path / "tiny.png"
    _png(source, (100, 100))

    rendered = volcano_asset_media.make_volcano_asset_image_jpeg(source)

    assert (rendered.width, rendered.height) == (300, 300)
    assert rendered.size_bytes < volcano_asset_media.VOLCANO_ASSET_IMAGE_MAX_BYTES
    with PILImage.open(io.BytesIO(rendered.data)) as image:
        assert image.format == "JPEG"
        assert image.size == (300, 300)


def test_image_normalization_pads_extreme_ratio_without_cropping(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wide.png"
    _png(source, (1000, 100), color=(200, 20, 20))

    rendered = volcano_asset_media.make_volcano_asset_image_jpeg(source)

    assert (rendered.width, rendered.height) == (1000, 400)
    assert rendered.width / rendered.height == 2.5
    with PILImage.open(io.BytesIO(rendered.data)) as image:
        top = image.getpixel((500, 20))
        center = image.getpixel((500, 200))
    assert all(channel > 220 for channel in top)
    assert center[0] > 150
    assert center[1] < 80


@pytest.mark.parametrize(
    ("source_size", "expected_canvas"),
    [
        ((4_000, 1), (2_048, 820)),
        ((1, 4_000), (820, 2_048)),
    ],
)
def test_image_layout_keeps_rounded_extreme_ratios_valid(
    source_size: tuple[int, int],
    expected_canvas: tuple[int, int],
) -> None:
    _content_width, _content_height, canvas_width, canvas_height = (
        volcano_asset_media._image_layout(*source_size)
    )

    assert (canvas_width, canvas_height) == expected_canvas
    assert 0.4 <= canvas_width / canvas_height <= 2.5
    assert max(canvas_width, canvas_height) <= 2_048


def test_image_normalization_rejects_undecodable_input(tmp_path: Path) -> None:
    source = tmp_path / "broken.png"
    source.write_bytes(b"not-an-image")

    with pytest.raises(volcano_asset_media.VolcanoAssetMediaError) as exc_info:
        volcano_asset_media.make_volcano_asset_image_jpeg(source)

    assert exc_info.value.code == "volcano_asset_image_decode_failed"
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize(
    ("source_size", "expected"),
    [
        ((4000, 500), (1280, 512)),
        ((500, 4000), (512, 1280)),
        ((1920, 1080), (1280, 720)),
        ((100, 100), (1280, 1280)),
    ],
)
def test_video_target_dimensions_pad_and_upscale(
    source_size: tuple[int, int],
    expected: tuple[int, int],
) -> None:
    width, height = volcano_asset_media._video_target_dimensions(*source_size)

    assert (width, height) == expected
    assert min(width, height) >= volcano_asset_media.VOLCANO_ASSET_MIN_SIDE
    assert (
        volcano_asset_media.VOLCANO_ASSET_VIDEO_MIN_PIXELS
        <= width * height
        <= volcano_asset_media.VOLCANO_ASSET_VIDEO_MAX_PIXELS
    )
    assert 0.4 <= width / height <= 2.5


def test_video_duration_is_clamped_to_asset_range() -> None:
    assert volcano_asset_media._video_target_duration_seconds(500) == 2.0
    assert volcano_asset_media._video_target_duration_seconds(8_000) == 8.0
    assert volcano_asset_media._video_target_duration_seconds(20_000) == 15.0


def test_ffmpeg_command_preserves_no_audio_semantics(tmp_path: Path) -> None:
    command = volcano_asset_media._ffmpeg_command(
        ffmpeg="ffmpeg",
        source_path=tmp_path / "source.mp4",
        destination=tmp_path / "output.mp4",
        source_has_audio=False,
        width=1280,
        height=720,
        duration_s=2.0,
        profile={"crf": "22", "maxrate": "8M", "bufsize": "16M"},
    )

    assert "anullsrc" not in " ".join(command)
    assert "-c:a" not in command
    assert "0:a:0" not in command
    assert "-nostdin" in command
    assert command[command.index("-loglevel") + 1] == "error"
    assert any("tpad=stop_mode=clone" in part for part in command)


def test_ffmpeg_command_transcodes_existing_audio_to_aac(tmp_path: Path) -> None:
    command = volcano_asset_media._ffmpeg_command(
        ffmpeg="ffmpeg",
        source_path=tmp_path / "source.mov",
        destination=tmp_path / "output.mp4",
        source_has_audio=True,
        width=1280,
        height=720,
        duration_s=3.0,
        profile={"crf": "22", "maxrate": "8M", "bufsize": "16M"},
    )

    assert "0:a:0" in command
    assert "-af" in command
    assert command[command.index("-af") + 1].startswith("apad,")
    assert command[command.index("-c:a") + 1] == "aac"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (
            subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=60),
            "inspection timed out",
        ),
        (OSError("resource unavailable"), "inspection could not start"),
    ],
)
def test_video_probe_wraps_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    message: str,
) -> None:
    from lumen_core import volcano_asset_media as shared_media

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(shared_media.subprocess, "run", fail)

    with pytest.raises(shared_media.VolcanoAssetMediaError) as exc_info:
        shared_media._probe_video("ffprobe", tmp_path / "source.mp4")

    assert exc_info.value.code == "volcano_asset_video_probe_failed"
    assert exc_info.value.status_code == 503
    assert message in exc_info.value.message


def test_video_output_validation_allows_no_audio() -> None:
    volcano_asset_media._validate_video_output(
        {
            "width": 1280,
            "height": 720,
            "duration_ms": 2_000,
            "fps": 30.0,
            "video_codec": "h264",
            "has_audio": False,
            "audio_codec": "",
            "size_bytes": 1_000,
        }
    )


def test_video_normalization_rejects_extreme_source_pixel_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lumen_core import volcano_asset_media as shared_media

    source = tmp_path / "extreme.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(shared_media.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        shared_media,
        "_probe_video",
        lambda _ffprobe, _path: {
            "width": 10_000,
            "height": 10_000,
            "duration_ms": 5_000,
            "has_audio": False,
        },
    )

    with pytest.raises(shared_media.VolcanoAssetMediaError) as exc_info:
        shared_media.make_volcano_asset_video_mp4(source)

    assert exc_info.value.code == "too_many_pixels"
    assert exc_info.value.status_code == 413


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)
def test_video_normalization_repairs_short_low_fps_video_without_audio(
    tmp_path: Path,
) -> None:
    source = tmp_path / "short.mp4"
    proc = subprocess.run(
        [
            str(shutil.which("ffmpeg")),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=120x40:r=12:d=1",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0

    rendered = volcano_asset_media.make_volcano_asset_video_mp4(source)

    assert (rendered.width, rendered.height) == (1280, 512)
    assert 2_000 <= rendered.duration_ms <= 2_100
    assert rendered.fps == pytest.approx(30.0, abs=0.1)
    assert rendered.has_audio is False
    assert rendered.size_bytes <= volcano_asset_media.VOLCANO_ASSET_VIDEO_MAX_BYTES
