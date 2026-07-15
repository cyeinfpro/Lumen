"""Shared dedicated media normalization for Volcano AIGC assets."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, ImageVariant, Video


VOLCANO_ASSET_IMAGE_KIND = "volcano_asset_img_v1"
VOLCANO_ASSET_IMAGE_MIME = "image/jpeg"
VOLCANO_ASSET_VIDEO_KIND = "volcano_asset_video_v1"
VOLCANO_ASSET_VIDEO_MIME = "video/mp4"
VOLCANO_ASSET_VIDEO_METADATA_KEY = "volcano_asset_video_variant"

VOLCANO_ASSET_MIN_SIDE = 300
VOLCANO_ASSET_MIN_ASPECT_RATIO = 0.4
VOLCANO_ASSET_MAX_ASPECT_RATIO = 2.5
VOLCANO_ASSET_IMAGE_MAX_SIDE = 2048
VOLCANO_ASSET_IMAGE_MAX_BYTES = 30 * 1024 * 1024
VOLCANO_ASSET_SOURCE_MAX_PIXELS = 64_000_000

VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE = 1280
VOLCANO_ASSET_VIDEO_MAX_SIDE = 1920
VOLCANO_ASSET_VIDEO_MIN_PIXELS = 409_600
VOLCANO_ASSET_VIDEO_MAX_PIXELS = 2_086_876
VOLCANO_ASSET_VIDEO_FPS = 30.0
VOLCANO_ASSET_VIDEO_MIN_FPS = 24.0
VOLCANO_ASSET_VIDEO_MAX_FPS = 60.0
VOLCANO_ASSET_VIDEO_MIN_DURATION_MS = 2_000
VOLCANO_ASSET_VIDEO_MAX_DURATION_MS = 15_000
VOLCANO_ASSET_VIDEO_MAX_BYTES = 50 * 1024 * 1024

_NEUTRAL_RGB = (245, 245, 245)
_JPEG_QUALITIES = (90, 82, 74, 66, 58, 50, 42, 34)
_VIDEO_PROFILES = (
    {"crf": "22", "maxrate": "8M", "bufsize": "16M"},
    {"crf": "26", "maxrate": "5M", "bufsize": "10M"},
    {"crf": "30", "maxrate": "3M", "bufsize": "6M"},
)
_VIDEO_TRANSCODE_SEMAPHORES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.Semaphore,
] = weakref.WeakKeyDictionary()
_VIDEO_TRANSCODE_SEMAPHORES_LOCK = threading.Lock()


class VolcanoAssetMediaError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class VolcanoAssetImageJpeg:
    data: bytes
    width: int
    height: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class VolcanoAssetVideoMp4:
    data: bytes
    width: int
    height: int
    duration_ms: int
    fps: float
    has_audio: bool
    size_bytes: int
    sha256: str


def _storage_path(storage_root: str, storage_key: str) -> Path:
    root = Path(storage_root).resolve()
    if not storage_key or "\x00" in storage_key:
        raise VolcanoAssetMediaError("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise VolcanoAssetMediaError(
            "invalid_path",
            "absolute storage paths are not allowed",
            400,
        )
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise VolcanoAssetMediaError(
            "invalid_path",
            "storage path escapes root",
            400,
        ) from exc
    return path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(
    path: Path,
    *,
    size_bytes: int,
    sha256: str,
) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == size_bytes
            and _file_sha256(path) == sha256
        )
    except OSError:
        return False


def _write_temp_file(path: Path, data: bytes) -> Path:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _install_file_atomic(
    path: Path,
    data: bytes,
    *,
    sha256: str,
) -> None:
    size_bytes = len(data)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise VolcanoAssetMediaError(
            "volcano_asset_media_storage_failed",
            "normalized asset media checksum is invalid",
            503,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        if _file_matches(path, size_bytes=size_bytes, sha256=sha256):
            return
        tmp: Path | None = None
        try:
            tmp = _write_temp_file(path, data)
            os.replace(tmp, path)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise VolcanoAssetMediaError(
                "volcano_asset_media_storage_failed",
                "normalized asset media could not be stored",
                503,
            ) from exc
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        if _file_matches(path, size_bytes=size_bytes, sha256=sha256):
            return
    raise VolcanoAssetMediaError(
        "volcano_asset_media_storage_conflict",
        "normalized asset media changed while it was being stored",
        503,
    )


def _video_transcode_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _VIDEO_TRANSCODE_SEMAPHORES_LOCK:
        semaphore = _VIDEO_TRANSCODE_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(2)
            _VIDEO_TRANSCODE_SEMAPHORES[loop] = semaphore
        return semaphore


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
            background = PILImage.new("RGBA", rgba.size, (*_NEUTRAL_RGB, 255))
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
                    _NEUTRAL_RGB,
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
                    for quality in _JPEG_QUALITIES:
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


async def ensure_volcano_asset_image_variant(
    db: AsyncSession,
    image: Image,
    *,
    storage_root: str,
) -> ImageVariant:
    current_image = (
        await db.execute(
            select(Image).where(
                Image.id == image.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if current_image is None:
        raise VolcanoAssetMediaError("not_found", "image was deleted", 404)
    image = current_image
    existing = (
        await db.execute(
            select(ImageVariant)
            .where(
                ImageVariant.image_id == image.id,
                ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
            )
            .order_by(ImageVariant.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_path = _storage_path(storage_root, existing.storage_key)
        if await asyncio.to_thread(
            _image_variant_file_is_valid,
            existing_path,
            width=existing.width,
            height=existing.height,
        ):
            return existing

    source_path = _storage_path(storage_root, image.storage_key)
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "image binary is missing", 404)
    rendered = await asyncio.to_thread(
        make_volcano_asset_image_jpeg,
        source_path,
    )
    current_image = (
        await db.execute(
            select(Image)
            .where(
                Image.id == image.id,
                Image.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current_image is None:
        raise VolcanoAssetMediaError("not_found", "image was deleted", 404)
    image = current_image
    existing = (
        await db.execute(
            select(ImageVariant)
            .where(
                ImageVariant.image_id == image.id,
                ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
            )
            .order_by(ImageVariant.created_at.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_path = _storage_path(storage_root, existing.storage_key)
        if await asyncio.to_thread(
            _image_variant_file_is_valid,
            existing_path,
            width=existing.width,
            height=existing.height,
        ):
            return existing

    key = volcano_asset_image_key(image)
    destination = _storage_path(storage_root, key)
    await asyncio.to_thread(
        _install_file_atomic,
        destination,
        rendered.data,
        sha256=rendered.sha256,
    )
    if existing is not None:
        existing.storage_key = key
        existing.width = rendered.width
        existing.height = rendered.height
        return existing

    variant = ImageVariant(
        image_id=image.id,
        kind=VOLCANO_ASSET_IMAGE_KIND,
        storage_key=key,
        width=rendered.width,
        height=rendered.height,
    )
    try:
        async with db.begin_nested():
            db.add(variant)
            await db.flush()
    except IntegrityError:
        if variant in db:
            db.expunge(variant)
        winner = (
            await db.execute(
                select(ImageVariant)
                .where(
                    ImageVariant.image_id == image.id,
                    ImageVariant.kind == VOLCANO_ASSET_IMAGE_KIND,
                )
                .order_by(ImageVariant.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if winner is not None:
            winner.storage_key = key
            winner.width = rendered.width
            winner.height = rendered.height
            return winner
        raise
    return variant


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


def make_volcano_asset_video_mp4(source_path: Path) -> VolcanoAssetVideoMp4:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_transcoder_missing",
            "ffmpeg and ffprobe are required for Volcano asset videos",
            503,
        )
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "video binary is missing", 404)

    source_metadata = _probe_video(ffprobe, source_path)
    source_width = int(source_metadata.get("width") or 0)
    source_height = int(source_metadata.get("height") or 0)
    source_duration_ms = int(source_metadata.get("duration_ms") or 0)
    if source_width <= 0 or source_height <= 0 or source_duration_ms <= 0:
        raise VolcanoAssetMediaError(
            "volcano_asset_video_decode_failed",
            "asset video metadata is invalid",
            422,
        )
    if source_width * source_height > VOLCANO_ASSET_SOURCE_MAX_PIXELS:
        raise VolcanoAssetMediaError(
            "too_many_pixels",
            "video exceeds the safe pixel limit",
            413,
        )
    target_width, target_height = _video_target_dimensions(
        source_width,
        source_height,
    )
    target_duration_s = _video_target_duration_seconds(source_duration_ms)
    last_error: VolcanoAssetMediaError | None = None
    with tempfile.TemporaryDirectory(prefix="lumen-volcano-asset-") as tmp:
        destination = Path(tmp) / "asset.mp4"
        for profile in _VIDEO_PROFILES:
            command = _ffmpeg_command(
                ffmpeg=ffmpeg,
                source_path=source_path,
                destination=destination,
                source_has_audio=bool(source_metadata.get("has_audio")),
                width=target_width,
                height=target_height,
                duration_s=target_duration_s,
                profile=profile,
            )
            try:
                proc = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise VolcanoAssetMediaError(
                    "volcano_asset_video_transcode_failed",
                    "asset video transcoding timed out",
                    503,
                ) from exc
            except OSError as exc:
                raise VolcanoAssetMediaError(
                    "volcano_asset_video_transcode_failed",
                    "asset video transcoding could not start",
                    503,
                ) from exc
            if proc.returncode != 0 or not destination.is_file():
                raise VolcanoAssetMediaError(
                    "volcano_asset_video_transcode_failed",
                    "asset video transcoding failed",
                    503,
                )
            output_metadata = _probe_video(ffprobe, destination)
            try:
                _validate_video_output(output_metadata)
            except VolcanoAssetMediaError as exc:
                last_error = exc
                if (
                    int(output_metadata.get("size_bytes") or 0)
                    > VOLCANO_ASSET_VIDEO_MAX_BYTES
                ):
                    destination.unlink(missing_ok=True)
                    continue
                raise
            data = destination.read_bytes()
            return VolcanoAssetVideoMp4(
                data=data,
                width=int(output_metadata["width"]),
                height=int(output_metadata["height"]),
                duration_ms=int(output_metadata["duration_ms"]),
                fps=float(output_metadata["fps"]),
                has_audio=bool(output_metadata["has_audio"]),
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
    raise last_error or VolcanoAssetMediaError(
        "volcano_asset_video_transcode_failed",
        "asset video could not be compressed to the required size",
        503,
    )


def volcano_asset_video_key(video: Video) -> str:
    source = Path(video.storage_key)
    return str(source.with_name(f"{video.id}.{VOLCANO_ASSET_VIDEO_KIND}.mp4"))


def volcano_asset_video_variant_metadata(video: Video) -> dict[str, Any] | None:
    metadata = video.metadata_jsonb if isinstance(video.metadata_jsonb, dict) else {}
    raw = metadata.get(VOLCANO_ASSET_VIDEO_METADATA_KEY)
    if not isinstance(raw, dict) or raw.get("kind") != VOLCANO_ASSET_VIDEO_KIND:
        return None
    storage_key = raw.get("storage_key")
    sha256 = raw.get("sha256")
    if not isinstance(storage_key, str) or not storage_key:
        return None
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        return None
    normalized = dict(raw)
    normalized["sha256"] = sha256.lower()
    return normalized


def _video_variant_file_is_valid(
    path: Path,
    metadata: dict[str, Any],
) -> bool:
    if metadata.get("mime") != VOLCANO_ASSET_VIDEO_MIME:
        return False
    validation_metadata = {
        **metadata,
        "video_codec": "h264",
        "audio_codec": "aac" if metadata.get("has_audio") else "",
    }
    try:
        _validate_video_output(validation_metadata)
        size_bytes = int(metadata.get("size_bytes") or 0)
        sha256 = str(metadata["sha256"])
    except (KeyError, OverflowError, TypeError, ValueError, VolcanoAssetMediaError):
        return False
    return _file_matches(
        path,
        size_bytes=size_bytes,
        sha256=sha256,
    )


async def ensure_volcano_asset_video_variant(
    db: AsyncSession,
    video: Video,
    *,
    storage_root: str,
) -> dict[str, Any]:
    current_video = (
        await db.execute(
            select(Video).where(
                Video.id == video.id,
                Video.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if current_video is None:
        raise VolcanoAssetMediaError("not_found", "video was deleted", 404)
    video = current_video
    existing = volcano_asset_video_variant_metadata(video)
    if existing is not None:
        existing_path = _storage_path(
            storage_root,
            str(existing["storage_key"]),
        )
        if await asyncio.to_thread(
            _video_variant_file_is_valid,
            existing_path,
            existing,
        ):
            return existing

    source_path = _storage_path(storage_root, video.storage_key)
    if not source_path.is_file():
        raise VolcanoAssetMediaError("not_found", "video binary is missing", 404)
    async with _video_transcode_semaphore():
        rendered = await asyncio.to_thread(
            make_volcano_asset_video_mp4,
            source_path,
        )
    current_video = (
        await db.execute(
            select(Video)
            .where(
                Video.id == video.id,
                Video.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current_video is None:
        raise VolcanoAssetMediaError("not_found", "video was deleted", 404)
    video = current_video
    existing = volcano_asset_video_variant_metadata(video)
    if existing is not None:
        existing_path = _storage_path(
            storage_root,
            str(existing["storage_key"]),
        )
        if await asyncio.to_thread(
            _video_variant_file_is_valid,
            existing_path,
            existing,
        ):
            return existing

    key = volcano_asset_video_key(video)
    destination = _storage_path(storage_root, key)
    await asyncio.to_thread(
        _install_file_atomic,
        destination,
        rendered.data,
        sha256=rendered.sha256,
    )
    variant = {
        "kind": VOLCANO_ASSET_VIDEO_KIND,
        "storage_key": key,
        "mime": VOLCANO_ASSET_VIDEO_MIME,
        "width": rendered.width,
        "height": rendered.height,
        "duration_ms": rendered.duration_ms,
        "fps": rendered.fps,
        "has_audio": rendered.has_audio,
        "size_bytes": rendered.size_bytes,
        "sha256": rendered.sha256,
    }
    metadata = dict(video.metadata_jsonb or {})
    metadata[VOLCANO_ASSET_VIDEO_METADATA_KEY] = variant
    video.metadata_jsonb = metadata
    return variant


__all__ = [
    "VOLCANO_ASSET_IMAGE_KIND",
    "VOLCANO_ASSET_IMAGE_MAX_BYTES",
    "VOLCANO_ASSET_IMAGE_MAX_SIDE",
    "VOLCANO_ASSET_IMAGE_MIME",
    "VOLCANO_ASSET_MAX_ASPECT_RATIO",
    "VOLCANO_ASSET_MIN_ASPECT_RATIO",
    "VOLCANO_ASSET_MIN_SIDE",
    "VOLCANO_ASSET_SOURCE_MAX_PIXELS",
    "VOLCANO_ASSET_VIDEO_FPS",
    "VOLCANO_ASSET_VIDEO_KIND",
    "VOLCANO_ASSET_VIDEO_MAX_BYTES",
    "VOLCANO_ASSET_VIDEO_MAX_DURATION_MS",
    "VOLCANO_ASSET_VIDEO_MAX_PIXELS",
    "VOLCANO_ASSET_VIDEO_MAX_SIDE",
    "VOLCANO_ASSET_VIDEO_METADATA_KEY",
    "VOLCANO_ASSET_VIDEO_MIME",
    "VOLCANO_ASSET_VIDEO_MIN_DURATION_MS",
    "VOLCANO_ASSET_VIDEO_MIN_PIXELS",
    "VOLCANO_ASSET_VIDEO_TARGET_LONG_SIDE",
    "VolcanoAssetImageJpeg",
    "VolcanoAssetMediaError",
    "VolcanoAssetVideoMp4",
    "ensure_volcano_asset_image_variant",
    "ensure_volcano_asset_video_variant",
    "make_volcano_asset_image_jpeg",
    "make_volcano_asset_video_mp4",
    "volcano_asset_image_key",
    "volcano_asset_video_key",
    "volcano_asset_video_variant_metadata",
]
