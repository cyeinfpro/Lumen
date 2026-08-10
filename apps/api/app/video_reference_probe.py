"""Bounded probing and validation for uploaded reference videos."""

from __future__ import annotations

import json
import logging
import math
import subprocess
from pathlib import Path
from typing import Any


VIDEO_REFERENCE_VIDEO_KIND = "video_ref_seedance_r2v_mp4"
VIDEO_REFERENCE_VIDEO_MIME = "video/mp4"
VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT = 2_086_876
VIDEO_REFERENCE_VIDEO_MAX_SIDE = 1920
VIDEO_REFERENCE_VIDEO_MAX_BYTES = 50 * 1024 * 1024
VIDEO_REFERENCE_VIDEO_TARGET_FPS = 30
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS = 16_777_216
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE = 4096
VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS = 2_000
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS = 30_000
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS = 60.0
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE = 300_000_000.0
VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS = 4_000_000_000.0
VIDEO_REFERENCE_VIDEO_FFPROBE_TIMEOUT_SECONDS = 15
VIDEO_REFERENCE_VIDEO_FFMPEG_TIMEOUT_SECONDS = 120
VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES = 256 * 1024 * 1024
_VIDEO_REFERENCE_VIDEO_DURATION_TOLERANCE_MS = 500
_VIDEO_REFERENCE_VIDEO_PROBE_OUTPUT_MAX_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


class VideoReferenceVideoError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _int_or_zero(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _fps(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _float_or_none(value)
    left, right = value.split("/", 1)
    try:
        denominator = float(right)
        if denominator == 0:
            return None
        return float(left) / denominator
    except (TypeError, ValueError):
        return None


def _fit_even_dimensions(
    width: int,
    height: int,
    *,
    max_pixels: int = VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT,
    max_side: int = VIDEO_REFERENCE_VIDEO_MAX_SIDE,
) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise VideoReferenceVideoError(
            "invalid_video", "reference video has invalid dimensions", 400
        )
    scale = min(
        1.0,
        max_side / max(width, height),
        math.sqrt(max_pixels / (width * height)),
    )
    target_width = max(2, int(width * scale) // 2 * 2)
    target_height = max(2, int(height * scale) // 2 * 2)
    while target_width * target_height > max_pixels:
        if target_width >= target_height and target_width > 2:
            target_width -= 2
        elif target_height > 2:
            target_height -= 2
        else:
            break
    return target_width, target_height


def _probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                (
                    "stream=codec_type,codec_name,width,height,duration,"
                    "avg_frame_rate,r_frame_rate:format=duration,size"
                ),
                "-show_streams",
                "-show_format",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=VIDEO_REFERENCE_VIDEO_FFPROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise VideoReferenceVideoError(
            "invalid_video",
            "reference video inspection timed out",
            422,
        ) from exc
    except OSError as exc:
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "reference video inspection could not start",
            503,
        ) from exc
    if proc.returncode != 0:
        logger.info(
            "reference video ffprobe rejected media stderr=%r",
            proc.stderr.decode("utf-8", "replace")[-500:],
        )
        raise VideoReferenceVideoError(
            "invalid_video",
            "reference video could not be inspected",
            400,
        )
    if len(proc.stdout) > _VIDEO_REFERENCE_VIDEO_PROBE_OUTPUT_MAX_BYTES:
        raise VideoReferenceVideoError(
            "invalid_video",
            "reference video metadata is too large",
            422,
        )
    try:
        raw = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise VideoReferenceVideoError(
            "invalid_video", "invalid ffprobe output", 400
        ) from exc
    raw_streams = raw.get("streams") if isinstance(raw, dict) else None
    streams = raw_streams if isinstance(raw_streams, list) else []
    video_stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        None,
    )
    if not isinstance(video_stream, dict):
        raise VideoReferenceVideoError(
            "invalid_video", "reference video has no video stream", 400
        )
    audio_stream = next(
        (
            item
            for item in streams
            if isinstance(item, dict) and item.get("codec_type") == "audio"
        ),
        None,
    )
    duration = _float_or_none(video_stream.get("duration")) or _float_or_none(
        (raw.get("format") or {}).get("duration") if isinstance(raw, dict) else None
    )
    audio_codec = (
        str(audio_stream.get("codec_name") or "")
        if isinstance(audio_stream, dict)
        else ""
    )
    return {
        "width": _int_or_zero(video_stream.get("width")),
        "height": _int_or_zero(video_stream.get("height")),
        "duration_ms": int(duration * 1000) if duration is not None else 0,
        "fps": _fps(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        ),
        "has_audio": audio_stream is not None,
        "video_codec": str(video_stream.get("codec_name") or ""),
        "audio_codec": audio_codec,
        "size_bytes": max(
            _int_or_zero(
                (raw.get("format") or {}).get("size") if isinstance(raw, dict) else None
            ),
            path.stat().st_size,
        ),
    }


def _validate_source_video(metadata: dict[str, Any]) -> None:
    width = _int_or_zero(metadata.get("width"))
    height = _int_or_zero(metadata.get("height"))
    pixels = width * height
    if (
        width <= 0
        or height <= 0
        or max(width, height) > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE
        or pixels > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS
    ):
        raise VideoReferenceVideoError(
            "too_many_video_pixels",
            "reference video exceeds the safe source resolution limit",
            413,
        )
    duration_ms = _int_or_zero(metadata.get("duration_ms"))
    if not (
        VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS
        <= duration_ms
        <= VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS
    ):
        raise VideoReferenceVideoError(
            "invalid_video_duration",
            "reference video duration must be between 2 and 30 seconds",
            422,
        )
    fps = _float_or_none(metadata.get("fps"))
    if fps is None or fps <= 0 or fps > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS:
        raise VideoReferenceVideoError(
            "invalid_video_fps",
            "reference video frame rate is invalid or exceeds the safe limit",
            422,
        )
    pixel_rate = pixels * fps
    decoded_pixels = pixel_rate * (duration_ms / 1000)
    if (
        pixel_rate > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE
        or decoded_pixels > VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS
    ):
        raise VideoReferenceVideoError(
            "video_decode_budget_exceeded",
            "reference video exceeds the safe decode workload limit",
            413,
        )


def _validate_output_video(
    metadata: dict[str, Any],
    *,
    expected_width: int,
    expected_height: int,
    expected_duration_ms: int,
) -> None:
    width = _int_or_zero(metadata.get("width"))
    height = _int_or_zero(metadata.get("height"))
    if (
        width != expected_width
        or height != expected_height
        or width * height > VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT
        or max(width, height) > VIDEO_REFERENCE_VIDEO_MAX_SIDE
    ):
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video has invalid dimensions",
            503,
        )
    duration_ms = _int_or_zero(metadata.get("duration_ms"))
    if not (
        VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS
        <= duration_ms
        <= (
            VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS
            + _VIDEO_REFERENCE_VIDEO_DURATION_TOLERANCE_MS
        )
    ) or abs(duration_ms - expected_duration_ms) > (
        _VIDEO_REFERENCE_VIDEO_DURATION_TOLERANCE_MS
    ):
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video has invalid duration",
            503,
        )
    fps = _float_or_none(metadata.get("fps"))
    if fps is None or fps <= 0 or fps > VIDEO_REFERENCE_VIDEO_TARGET_FPS + 0.5:
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video has invalid frame rate",
            503,
        )
    if metadata.get("video_codec") != "h264":
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video must use H.264",
            503,
        )
    if metadata.get("has_audio") and metadata.get("audio_codec") != "aac":
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video audio must use AAC",
            503,
        )
    size_bytes = _int_or_zero(metadata.get("size_bytes"))
    if size_bytes <= 0 or size_bytes > VIDEO_REFERENCE_VIDEO_MAX_BYTES:
        raise VideoReferenceVideoError(
            "video_reference_transcode_failed",
            "transcoded reference video exceeds the output size limit",
            503,
        )


__all__ = [
    "VIDEO_REFERENCE_VIDEO_FFMPEG_MAX_ALLOC_BYTES",
    "VIDEO_REFERENCE_VIDEO_FFMPEG_TIMEOUT_SECONDS",
    "VIDEO_REFERENCE_VIDEO_KIND",
    "VIDEO_REFERENCE_VIDEO_MAX_BYTES",
    "VIDEO_REFERENCE_VIDEO_MAX_SIDE",
    "VIDEO_REFERENCE_VIDEO_MIME",
    "VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DECODE_PIXELS",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MAX_DURATION_MS",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MAX_FPS",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXEL_RATE",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MAX_PIXELS",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MAX_SIDE",
    "VIDEO_REFERENCE_VIDEO_SOURCE_MIN_DURATION_MS",
    "VIDEO_REFERENCE_VIDEO_TARGET_FPS",
    "VideoReferenceVideoError",
    "_fit_even_dimensions",
    "_float_or_none",
    "_fps",
    "_int_or_zero",
    "_probe_video",
    "_validate_output_video",
    "_validate_source_video",
]
