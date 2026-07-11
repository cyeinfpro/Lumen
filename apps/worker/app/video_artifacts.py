"""Temporary-file helpers for downloaded and stored video artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_VIDEO_MIME_EXTENSIONS = {
    "video/3gpp": ".3gp",
    "video/3gpp2": ".3g2",
    "video/mp2t": ".ts",
    "video/mp4": ".mp4",
    "video/mpeg": ".mpeg",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-flv": ".flv",
    "video/x-m4v": ".m4v",
    "video/x-matroska": ".mkv",
    "video/x-ms-wmv": ".wmv",
    "video/x-msvideo": ".avi",
}
_VIDEO_MIME_ALIASES = {
    "video/avi": "video/x-msvideo",
    "video/mov": "video/quicktime",
    "video/mpegts": "video/mp2t",
    "video/x-mov": "video/quicktime",
}
_OCTET_STREAM_MIMES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
}
_COPY_CHUNK_BYTES = 1024 * 1024


class UnsupportedVideoMediaError(ValueError):
    pass


@dataclass(frozen=True)
class DownloadedVideo:
    path: Path
    mime: str
    extension: str
    size_bytes: int
    declared_mime: str | None = None
    temporary: bool = True

    def cleanup(self) -> None:
        if self.temporary:
            self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProcessedVideoFile:
    path: Path
    mime: str
    extension: str
    size_bytes: int
    sha256: str
    poster_bytes: bytes | None
    faststart: bool
    metadata: dict[str, Any]
    temporary: bool = False

    def cleanup(self) -> None:
        if self.temporary:
            self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class VideoArtifactWriteResult:
    size: int
    created: bool


def _normalized_declared_mime(raw: str | None) -> str:
    value = (raw or "").split(";", 1)[0].strip().lower()
    return _VIDEO_MIME_ALIASES.get(value, value)


def _looks_like_mpeg_ts(prefix: bytes) -> bool:
    for packet_size in (188, 192, 204):
        if len(prefix) < packet_size * 3:
            continue
        for start in range(min(packet_size, 16)):
            if all(prefix[start + packet_size * index] == 0x47 for index in range(3)):
                return True
    return False


def sniff_video_mime(prefix: bytes) -> str | None:
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        brand = prefix[8:12].lower()
        if brand.startswith(b"3g2"):
            return "video/3gpp2"
        if brand.startswith(b"3g"):
            return "video/3gpp"
        if brand == b"qt  ":
            return "video/quicktime"
        if brand in {b"m4v ", b"m4vh", b"m4vp"}:
            return "video/x-m4v"
        if brand in {b"m4a ", b"m4b "}:
            return None
        return "video/mp4"
    if prefix.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm" if b"webm" in prefix[:4096].lower() else "video/x-matroska"
    if prefix.startswith(b"OggS") and b"theora" in prefix[:4096].lower():
        return "video/ogg"
    if len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"AVI ":
        return "video/x-msvideo"
    if prefix.startswith((b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3")):
        return "video/mpeg"
    if prefix.startswith(b"FLV"):
        return "video/x-flv"
    if prefix.startswith(
        b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"
    ):
        return "video/x-ms-wmv"
    if _looks_like_mpeg_ts(prefix):
        return "video/mp2t"
    return None


def detect_video_media(prefix: bytes, declared_mime: str | None) -> tuple[str, str]:
    declared = _normalized_declared_mime(declared_mime)
    sniffed = sniff_video_mime(prefix)
    if sniffed is not None:
        return sniffed, _VIDEO_MIME_EXTENSIONS[sniffed]
    if declared not in _OCTET_STREAM_MIMES and not declared.startswith("video/"):
        raise UnsupportedVideoMediaError(
            f"response content type is not video: {declared or '<missing>'}"
        )
    raise UnsupportedVideoMediaError(
        f"video bytes have no supported signature: {declared or '<missing>'}"
    )


def downloaded_video_from_bytes(
    data: bytes,
    *,
    declared_mime: str | None = "video/mp4",
) -> DownloadedVideo:
    if not data:
        raise UnsupportedVideoMediaError("video data is empty")
    mime, extension = detect_video_media(data[:4096], declared_mime)
    fd, raw_path = tempfile.mkstemp(prefix="lumen-video-download-", suffix=".part")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as file_obj:
            file_obj.write(data)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return DownloadedVideo(
        path=path,
        mime=mime,
        extension=extension,
        size_bytes=len(data),
        declared_mime=_normalized_declared_mime(declared_mime) or None,
    )


def hash_video_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _same_file_content(path: Path, expected_sha256: str, expected_size: int) -> bool:
    try:
        if path.stat().st_size != expected_size:
            return False
    except FileNotFoundError:
        return False
    actual_sha256, _size = hash_video_file(path)
    return actual_sha256 == expected_sha256


def _copy_file_exclusive(source: Path, destination: Path) -> None:
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_obj, os.fdopen(fd, "wb") as destination_obj:
            shutil.copyfileobj(source_obj, destination_obj, length=_COPY_CHUNK_BYTES)
            destination_obj.flush()
            os.fsync(destination_obj.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def copy_video_file_exclusive_result(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> VideoArtifactWriteResult:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _same_file_content(destination, expected_sha256, expected_size):
            return VideoArtifactWriteResult(size=expected_size, created=False)
        raise FileExistsError(destination)

    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    try:
        _copy_file_exclusive(source, temporary)
        created = True
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if not _same_file_content(destination, expected_sha256, expected_size):
                raise
            created = False
        except OSError as exc:
            if exc.errno not in {
                errno.EPERM,
                errno.EACCES,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
                errno.EOPNOTSUPP,
            }:
                raise
            try:
                _copy_file_exclusive(temporary, destination)
            except FileExistsError:
                if not _same_file_content(
                    destination,
                    expected_sha256,
                    expected_size,
                ):
                    raise
                created = False
        return VideoArtifactWriteResult(size=expected_size, created=created)
    finally:
        temporary.unlink(missing_ok=True)


def copy_video_file_exclusive(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> int:
    return copy_video_file_exclusive_result(
        source,
        destination,
        expected_sha256=expected_sha256,
        expected_size=expected_size,
    ).size


def _temporary_video_path(suffix: str) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix="lumen-video-", suffix=suffix)
    os.close(fd)
    return Path(raw_path)


def _read_file_prefix(path: Path, limit: int = 4096) -> bytes:
    with path.open("rb") as file_obj:
        return file_obj.read(limit)


def _ffmpeg_error(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stderr.decode("utf-8", "replace")[-1000:]


def postprocess_video_file(downloaded: DownloadedVideo) -> ProcessedVideoFile:
    diagnostics: dict[str, Any] = {
        "input_mime": downloaded.mime,
        "input_extension": downloaded.extension,
        "declared_input_mime": downloaded.declared_mime,
    }
    output_path = downloaded.path
    output_mime = downloaded.mime
    output_extension = downloaded.extension
    output_temporary = False
    poster_bytes: bytes | None = None
    poster_path: Path | None = None
    remux_path: Path | None = None
    input_faststart = (
        _looks_faststart_path(downloaded.path)
        if downloaded.mime == "video/mp4"
        else False
    )
    diagnostics["faststart_input"] = input_faststart
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    needs_remux = downloaded.mime != "video/mp4" or not input_faststart
    try:
        if needs_remux and ffmpeg:
            diagnostics["remux_attempted"] = True
            remux_path = _temporary_video_path(".mp4")
            try:
                proc = subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-i",
                        str(downloaded.path),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(remux_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=120,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                diagnostics["remux_error"] = f"ffmpeg timeout after {exc.timeout}s"
            else:
                if (
                    proc.returncode == 0
                    and remux_path.is_file()
                    and remux_path.stat().st_size > 0
                    and sniff_video_mime(_read_file_prefix(remux_path)) == "video/mp4"
                ):
                    output_path = remux_path
                    output_mime = "video/mp4"
                    output_extension = ".mp4"
                    output_temporary = True
                    diagnostics["remux_succeeded"] = True
                else:
                    diagnostics["remux_error"] = _ffmpeg_error(proc)
            if not output_temporary and remux_path is not None:
                remux_path.unlink(missing_ok=True)
                remux_path = None
        elif needs_remux:
            diagnostics["ffmpeg_missing"] = True

        metadata = probe_video(ffprobe, output_path) if ffprobe else {}
        if not ffprobe:
            diagnostics["ffprobe_missing"] = True
        if ffmpeg:
            poster_path = _temporary_video_path(".jpg")
            try:
                poster_proc = subprocess.run(
                    [
                        ffmpeg,
                        "-y",
                        "-ss",
                        "0",
                        "-i",
                        str(output_path),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        str(poster_path),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                diagnostics["poster_error"] = (
                    f"ffmpeg poster timeout after {exc.timeout}s"
                )
            else:
                if (
                    poster_proc.returncode == 0
                    and poster_path.is_file()
                    and poster_path.stat().st_size > 0
                ):
                    poster_bytes = poster_path.read_bytes()
                else:
                    diagnostics["poster_error"] = _ffmpeg_error(poster_proc)
        else:
            diagnostics["ffmpeg_missing"] = True

        faststart = (
            _looks_faststart_path(output_path) if output_mime == "video/mp4" else False
        )
        sha256, size_bytes = hash_video_file(output_path)
        diagnostics.update(metadata)
        diagnostics.update(
            {
                "faststart": faststart,
                "output_mime": output_mime,
                "output_extension": output_extension,
                "size_bytes": size_bytes,
                "sha256": sha256,
            }
        )
        return ProcessedVideoFile(
            path=output_path,
            mime=output_mime,
            extension=output_extension,
            size_bytes=size_bytes,
            sha256=sha256,
            poster_bytes=poster_bytes,
            faststart=faststart,
            metadata=diagnostics,
            temporary=output_temporary,
        )
    except BaseException:
        if output_temporary:
            output_path.unlink(missing_ok=True)
        raise
    finally:
        if poster_path is not None:
            poster_path.unlink(missing_ok=True)


def postprocess_video_bytes(data: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    downloaded = downloaded_video_from_bytes(data, declared_mime="video/mp4")
    processed: ProcessedVideoFile | None = None
    try:
        processed = postprocess_video_file(downloaded)
        video_bytes = processed.path.read_bytes()
        diagnostics = dict(processed.metadata)
        return {
            "video_bytes": video_bytes,
            "poster_bytes": processed.poster_bytes,
            "mime": processed.mime,
            "extension": processed.extension,
            "faststart": processed.faststart,
            **diagnostics,
        }, diagnostics
    finally:
        if processed is not None:
            processed.cleanup()
        downloaded.cleanup()


def probe_video(ffprobe: str, path: Path) -> dict[str, Any]:
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
    if proc.returncode != 0:
        return {"probe_error": proc.stderr.decode("utf-8", "replace")[-1000:]}
    try:
        raw = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return {"probe_error": "invalid ffprobe json"}
    raw_streams = raw.get("streams") if isinstance(raw, dict) else None
    streams = raw_streams if isinstance(raw_streams, list) else []
    raw_format = raw.get("format") if isinstance(raw, dict) else None
    format_data = raw_format if isinstance(raw_format, dict) else {}
    video_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"),
        {},
    )
    audio_stream = next(
        (s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"),
        None,
    )
    duration = _float_or_none(video_stream.get("duration")) or _float_or_none(
        format_data.get("duration")
    )
    fps = _fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    return {
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "duration_ms": int(duration * 1000) if duration is not None else 0,
        "fps": fps,
        "has_audio": audio_stream is not None,
        "probe": raw,
    }


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _fps(value: Any) -> float | None:
    if not isinstance(value, str) or "/" not in value:
        return _float_or_none(value)
    left, right = value.split("/", 1)
    try:
        denom = float(right)
        if denom == 0:
            return None
        return float(left) / denom
    except (TypeError, ValueError):
        return None


def _looks_faststart(data: bytes) -> bool:
    offset = 0
    moov_offset: int | None = None
    mdat_offset: int | None = None
    data_len = len(data)
    while offset + 8 <= data_len:
        size = int.from_bytes(data[offset : offset + 4], "big")
        box_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > data_len:
                break
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = data_len - offset
        if size < header_size:
            break
        if box_type == b"moov" and moov_offset is None:
            moov_offset = offset
        elif box_type == b"mdat" and mdat_offset is None:
            mdat_offset = offset
        if moov_offset is not None and mdat_offset is not None:
            break
        offset += size
    return moov_offset is not None and (
        mdat_offset is None or moov_offset < mdat_offset
    )


def _looks_faststart_path(path: Path) -> bool:
    try:
        data_len = path.stat().st_size
    except OSError:
        return False
    offset = 0
    moov_offset: int | None = None
    mdat_offset: int | None = None
    with path.open("rb") as file_obj:
        while offset + 8 <= data_len:
            file_obj.seek(offset)
            header = file_obj.read(16)
            if len(header) < 8:
                break
            size = int.from_bytes(header[:4], "big")
            box_type = header[4:8]
            header_size = 8
            if size == 1:
                if len(header) < 16:
                    break
                size = int.from_bytes(header[8:16], "big")
                header_size = 16
            elif size == 0:
                size = data_len - offset
            if size < header_size:
                break
            if box_type == b"moov" and moov_offset is None:
                moov_offset = offset
            elif box_type == b"mdat" and mdat_offset is None:
                mdat_offset = offset
            if moov_offset is not None and mdat_offset is not None:
                break
            offset += size
    return moov_offset is not None and (
        mdat_offset is None or moov_offset < mdat_offset
    )


__all__ = [
    "DownloadedVideo",
    "ProcessedVideoFile",
    "UnsupportedVideoMediaError",
    "VideoArtifactWriteResult",
    "copy_video_file_exclusive",
    "copy_video_file_exclusive_result",
    "detect_video_media",
    "downloaded_video_from_bytes",
    "hash_video_file",
    "postprocess_video_bytes",
    "postprocess_video_file",
    "probe_video",
    "sniff_video_mime",
]
