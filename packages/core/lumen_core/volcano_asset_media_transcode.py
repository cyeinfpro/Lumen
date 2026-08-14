"""Pure image and video transcode helpers for Volcano assets."""

from __future__ import annotations

import hashlib
import io
import json
import math
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError

from lumen_core.model_entities import Image

from .volcano_asset_media_types import (
    VOLCANO_ASSET_IMAGE_KIND,
    VOLCANO_ASSET_JPEG_QUALITIES,
    VOLCANO_ASSET_IMAGE_MAX_BYTES,
    VOLCANO_ASSET_IMAGE_MAX_SIDE,
    VOLCANO_ASSET_MAX_ASPECT_RATIO,
    VOLCANO_ASSET_MIN_ASPECT_RATIO,
    VOLCANO_ASSET_MIN_SIDE,
    VOLCANO_ASSET_SOURCE_MAX_PIXELS,
    VOLCANO_ASSET_VIDEO_FPS,
    VOLCANO_ASSET_VIDEO_MAX_BYTES,
    VOLCANO_ASSET_VIDEO_MAX_DURATION_MS,
    VOLCANO_ASSET_VIDEO_MAX_FPS,
    VOLCANO_ASSET_VIDEO_MAX_PIXELS,
    VOLCANO_ASSET_VIDEO_MAX_SIDE,
    VOLCANO_ASSET_VIDEO_MIN_DURATION_MS,
    VOLCANO_ASSET_VIDEO_MIN_FPS,
    VOLCANO_ASSET_VIDEO_MIN_PIXELS,
    VOLCANO_ASSET_VIDEO_POSTER_MAX_BYTES,
    VOLCANO_ASSET_VIDEO_POSTER_MAX_SIDE,
    VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE,
    VOLCANO_ASSET_NEUTRAL_RGB,
    VolcanoAssetImageJpeg,
    VolcanoAssetMediaError,
)


def _even(value: float, *, minimum: int = 2) -> int:
    rounded = max(minimum, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded + 1


def _padded_canvas_size(width: int, height: int) -> tuple[float, float]:
    if width <= 0 or height <= 0:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_invalid",
            "media dimensions are invalid",
        )
    ratio = width / height
    if ratio > VOLCANO_ASSET_MAX_ASPECT_RATIO:
        return float(width), width / VOLCANO_ASSET_MAX_ASPECT_RATIO
    if ratio < VOLCANO_ASSET_MIN_ASPECT_RATIO:
        return height * VOLCANO_ASSET_MIN_ASPECT_RATIO, float(height)
    return float(width), float(height)


def _image_layout(width: int, height: int) -> tuple[int, int, int, int]:
    canvas_width, canvas_height = _padded_canvas_size(width, height)
    scale = min(
        1.0,
        VOLCANO_ASSET_IMAGE_MAX_SIDE / max(canvas_width, canvas_height),
    )
    scaled_min_side = min(canvas_width, canvas_height) * scale
    if scaled_min_side < VOLCANO_ASSET_MIN_SIDE:
        scale = VOLCANO_ASSET_MIN_SIDE / min(canvas_width, canvas_height)
    target_canvas_width = max(1, int(round(canvas_width * scale)))
    target_canvas_height = max(1, int(round(canvas_height * scale)))
    ratio = target_canvas_width / target_canvas_height
    if ratio > VOLCANO_ASSET_MAX_ASPECT_RATIO:
        target_canvas_height = max(
            target_canvas_height,
            math.ceil(target_canvas_width / VOLCANO_ASSET_MAX_ASPECT_RATIO),
        )
    elif ratio < VOLCANO_ASSET_MIN_ASPECT_RATIO:
        target_canvas_width = max(
            target_canvas_width,
            math.ceil(target_canvas_height * VOLCANO_ASSET_MIN_ASPECT_RATIO),
        )
    target_content_width = max(1, int(round(width * scale)))
    target_content_height = max(1, int(round(height * scale)))
    return (
        target_content_width,
        target_content_height,
        target_canvas_width,
        target_canvas_height,
    )


def _flatten_image(image: PILImage.Image) -> PILImage.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        try:
            background = PILImage.new(
                "RGBA",
                rgba.size,
                (*VOLCANO_ASSET_NEUTRAL_RGB, 255),
            )
            background.alpha_composite(rgba)
            return background.convert("RGB")
        finally:
            rgba.close()
    return image.convert("RGB")


def _validate_image_output(
    *,
    width: int,
    height: int,
    size_bytes: int,
) -> None:
    if (
        width <= 0
        or height <= 0
        or min(width, height) < VOLCANO_ASSET_MIN_SIDE
        or max(width, height) > VOLCANO_ASSET_IMAGE_MAX_SIDE
    ):
        raise VolcanoAssetMediaError(
            "volcano_asset_image_transcode_failed",
            "normalized asset image dimensions are invalid",
            503,
        )
    ratio = width / height
    if not (VOLCANO_ASSET_MIN_ASPECT_RATIO <= ratio <= VOLCANO_ASSET_MAX_ASPECT_RATIO):
        raise VolcanoAssetMediaError(
            "volcano_asset_image_transcode_failed",
            "normalized asset image aspect ratio is invalid",
            503,
        )
    if size_bytes <= 0 or size_bytes >= VOLCANO_ASSET_IMAGE_MAX_BYTES:
        raise VolcanoAssetMediaError(
            "volcano_asset_image_transcode_failed",
            "normalized asset image exceeds the size limit",
            503,
        )


def make_volcano_asset_image_jpeg(source_path: Path) -> VolcanoAssetImageJpeg:
    try:
        opened = PILImage.open(source_path)
    except PILImage.DecompressionBombError as exc:
        raise VolcanoAssetMediaError(
            "too_many_pixels",
            "image exceeds the safe pixel limit",
            413,
        ) from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_image_decode_failed",
            "asset image could not be decoded",
            422,
        ) from exc

    with opened:
        width, height = opened.size
        if width * height > VOLCANO_ASSET_SOURCE_MAX_PIXELS:
            raise VolcanoAssetMediaError(
                "too_many_pixels",
                "image exceeds the safe pixel limit",
                413,
            )
        try:
            opened.load()
        except (OSError, ValueError) as exc:
            raise VolcanoAssetMediaError(
                "volcano_asset_image_decode_failed",
                "asset image could not be decoded",
                422,
            ) from exc
        oriented = ImageOps.exif_transpose(opened)
        try:
            (
                content_width,
                content_height,
                canvas_width,
                canvas_height,
            ) = _image_layout(oriented.width, oriented.height)
            flattened = _flatten_image(oriented)
            try:
                resized = flattened.resize(
                    (content_width, content_height),
                    PILImage.Resampling.LANCZOS,
                )
            finally:
                flattened.close()
            try:
                canvas = PILImage.new(
                    "RGB",
                    (canvas_width, canvas_height),
                    VOLCANO_ASSET_NEUTRAL_RGB,
                )
                try:
                    canvas.paste(
                        resized,
                        (
                            (canvas_width - content_width) // 2,
                            (canvas_height - content_height) // 2,
                        ),
                    )
                    data: bytes | None = None
                    for quality in VOLCANO_ASSET_JPEG_QUALITIES:
                        output = io.BytesIO()
                        try:
                            canvas.save(
                                output,
                                format="JPEG",
                                quality=quality,
                                optimize=True,
                            )
                        except OSError:
                            output = io.BytesIO()
                            canvas.save(
                                output,
                                format="JPEG",
                                quality=quality,
                            )
                        candidate = output.getvalue()
                        if len(candidate) < VOLCANO_ASSET_IMAGE_MAX_BYTES:
                            data = candidate
                            break
                finally:
                    canvas.close()
            finally:
                resized.close()
        finally:
            if oriented is not opened:
                oriented.close()

    if data is None:
        raise VolcanoAssetMediaError(
            "volcano_asset_image_transcode_failed",
            "asset image could not be compressed to the required size",
            503,
        )
    _validate_image_output(
        width=canvas_width,
        height=canvas_height,
        size_bytes=len(data),
    )
    return VolcanoAssetImageJpeg(
        data=data,
        width=canvas_width,
        height=canvas_height,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def volcano_asset_image_key(image: Image) -> str:
    source = Path(image.storage_key)
    return str(source.with_name(f"{image.id}.{VOLCANO_ASSET_IMAGE_KIND}.jpg"))


def _image_variant_file_is_valid(
    path: Path,
    *,
    width: int,
    height: int,
) -> bool:
    try:
        size_bytes = path.stat().st_size
        _validate_image_output(
            width=width,
            height=height,
            size_bytes=size_bytes,
        )
        with PILImage.open(path) as image:
            if image.format != "JPEG" or image.size != (width, height):
                return False
            image.load()
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        PILImage.DecompressionBombError,
        VolcanoAssetMediaError,
    ):
        return False
    return True


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _fps(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _float_or_none(value)
    left, right = value.split("/", 1)
    try:
        denominator = float(right)
        if denominator == 0:
            return None
        return _float_or_none(float(left) / denominator)
    except (TypeError, ValueError):
        return None


def _probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_probe_failed",
            "asset video inspection timed out",
            503,
        ) from exc
    except OSError as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_probe_failed",
            "asset video inspection could not start",
            503,
        ) from exc
    if proc.returncode != 0:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_decode_failed",
            "asset video could not be decoded",
            422,
        )
    try:
        payload = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_decode_failed",
            "asset video metadata is invalid",
            422,
        ) from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        streams = []
    video_stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video_stream, dict):
        raise VolcanoAssetMediaError(
            "volcano_asset_video_decode_failed",
            "asset video has no video stream",
            422,
        )
    audio_stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
        None,
    )
    format_payload = payload.get("format") if isinstance(payload, dict) else None
    format_payload = format_payload if isinstance(format_payload, dict) else {}
    duration = _float_or_none(format_payload.get("duration"))
    if duration is None:
        duration = _float_or_none(video_stream.get("duration"))
    return {
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "duration_ms": int(duration * 1000) if duration is not None else 0,
        "fps": _fps(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        ),
        "video_codec": str(video_stream.get("codec_name") or ""),
        "pixel_format": str(video_stream.get("pix_fmt") or ""),
        "has_audio": isinstance(audio_stream, dict),
        "audio_codec": (
            str(audio_stream.get("codec_name") or "")
            if isinstance(audio_stream, dict)
            else ""
        ),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


def _video_target_dimensions(width: int, height: int) -> tuple[int, int]:
    canvas_width, canvas_height = _padded_canvas_size(width, height)
    scale = VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE / max(
        canvas_width,
        canvas_height,
    )
    target_width = _even(canvas_width * scale)
    target_height = _even(canvas_height * scale)
    if max(target_width, target_height) > VOLCANO_ASSET_VIDEO_MAX_SIDE:
        correction = VOLCANO_ASSET_VIDEO_MAX_SIDE / max(
            target_width,
            target_height,
        )
        target_width = _even(target_width * correction)
        target_height = _even(target_height * correction)
    return target_width, target_height


def _video_target_duration_seconds(duration_ms: int) -> float:
    return min(max(duration_ms / 1000, 2.0), 15.0)


def _validate_video_output(metadata: dict[str, Any]) -> None:
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    pixels = width * height
    if (
        width <= 0
        or height <= 0
        or min(width, height) < VOLCANO_ASSET_MIN_SIDE
        or max(width, height) > VOLCANO_ASSET_VIDEO_MAX_SIDE
        or pixels < VOLCANO_ASSET_VIDEO_MIN_PIXELS
        or pixels > VOLCANO_ASSET_VIDEO_MAX_PIXELS
    ):
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video dimensions are invalid",
            503,
        )
    ratio = width / height
    if not (VOLCANO_ASSET_MIN_ASPECT_RATIO <= ratio <= VOLCANO_ASSET_MAX_ASPECT_RATIO):
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video aspect ratio is invalid",
            503,
        )
    duration_ms = int(metadata.get("duration_ms") or 0)
    if not (
        VOLCANO_ASSET_VIDEO_MIN_DURATION_MS
        <= duration_ms
        <= VOLCANO_ASSET_VIDEO_MAX_DURATION_MS
    ):
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video duration is invalid",
            503,
        )
    fps = _float_or_none(metadata.get("fps"))
    if fps is None or not (
        VOLCANO_ASSET_VIDEO_MIN_FPS <= fps <= VOLCANO_ASSET_VIDEO_MAX_FPS
    ):
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video FPS is invalid",
            503,
        )
    if metadata.get("video_codec") != "h264":
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video codec is invalid",
            503,
        )
    if metadata.get("has_audio") and metadata.get("audio_codec") != "aac":
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video audio codec is invalid",
            503,
        )
    size_bytes = int(metadata.get("size_bytes") or 0)
    if size_bytes <= 0 or size_bytes > VOLCANO_ASSET_VIDEO_MAX_BYTES:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcode_failed",
            "normalized asset video exceeds the size limit",
            503,
        )


def _make_video_poster_jpeg(
    ffmpeg: str,
    source_path: Path,
    destination: Path,
    *,
    timeout_seconds: float,
) -> bytes:
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "0",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                (
                    f"scale={VOLCANO_ASSET_VIDEO_POSTER_MAX_SIDE}:"
                    f"{VOLCANO_ASSET_VIDEO_POSTER_MAX_SIDE}:"
                    "force_original_aspect_ratio=decrease"
                ),
                "-q:v",
                "3",
                str(destination),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(60.0, max(1.0, timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_preview_failed",
            "asset video preview generation timed out",
            503,
        ) from exc
    except OSError as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_preview_failed",
            "asset video preview generation could not start",
            503,
        ) from exc
    if proc.returncode != 0 or not destination.is_file():
        raise VolcanoAssetMediaError(
            "volcano_asset_video_preview_failed",
            "asset video preview generation failed",
            503,
        )
    try:
        data = destination.read_bytes()
        if not data or len(data) > VOLCANO_ASSET_VIDEO_POSTER_MAX_BYTES:
            raise ValueError("poster size is invalid")
        with PILImage.open(destination) as poster:
            if (
                poster.format != "JPEG"
                or poster.width <= 0
                or poster.height <= 0
                or max(poster.size) > VOLCANO_ASSET_VIDEO_POSTER_MAX_SIDE
            ):
                raise ValueError("poster dimensions are invalid")
            poster.load()
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        PILImage.DecompressionBombError,
    ) as exc:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_preview_failed",
            "asset video preview is invalid",
            503,
        ) from exc
    return data


def _ffmpeg_command(
    *,
    ffmpeg: str,
    source_path: Path,
    destination: Path,
    source_has_audio: bool,
    width: int,
    height: int,
    duration_s: float,
    profile: dict[str, str],
) -> list[str]:
    video_filter = (
        f"scale=w={width}:h={height}:"
        "force_original_aspect_ratio=decrease:"
        "force_divisible_by=2:flags=lanczos,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:"
        "color=0xF5F5F5,"
        f"fps={int(VOLCANO_ASSET_VIDEO_FPS)},"
        "tpad=stop_mode=clone:stop_duration=2,"
        f"trim=duration={duration_s:.3f},"
        "setpts=PTS-STARTPTS"
    )
    audio_filter = f"apad,atrim=duration={duration_s:.3f},asetpts=PTS-STARTPTS"
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
    ]
    command.extend(
        [
            "-map",
            "0:v:0",
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            profile["crf"],
            "-maxrate",
            profile["maxrate"],
            "-bufsize",
            profile["bufsize"],
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if source_has_audio:
        command.extend(
            [
                "-map",
                "0:a:0",
                "-af",
                audio_filter,
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
            ]
        )
    command.extend(
        [
            "-t",
            f"{duration_s:.3f}",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    return command
