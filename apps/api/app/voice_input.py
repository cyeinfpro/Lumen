"""Validate and crop uploads before a reference is sent to a voice provider."""

from __future__ import annotations

import io
import json
import math
import subprocess
import sys
import tempfile
import wave
from array import array
from pathlib import Path
from threading import Event

from .media_process import cancellable_media_thread, run_media_process

MAX_SOURCE_BYTES = 10_000_000
MAX_SOURCE_MS = 60_000
MIN_REFERENCE_MS = 3_000
MAX_REFERENCE_MS = 20_000
TARGET_SAMPLE_RATE = 16_000
MAX_DURATION_DRIFT_MS = 100
_PROBE_SECONDS = 8
_DECODE_SECONDS = 12
_MAX_SINGLE_ALLOC = "33554432"


class VoiceInputError(ValueError):
    """Safe, stable code for rejecting an upload without exposing media diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _demuxer(data: bytes) -> tuple[str, tuple[str, ...]]:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav", ()
    if data.startswith(b"ID3") or (
        len(data) >= 4
        and data[0] == 0xFF
        and data[1] & 0xE0 == 0xE0
        and data[1] & 0x06 != 0
        and data[2] & 0xF0 not in (0x00, 0xF0)
        and data[2] & 0x0C != 0x0C
    ):
        return "mp3", ()
    if len(data) >= 16 and data[4:8] == b"ftyp":
        # The MOV demuxer also supports audio in the ISO BMFF (M4A) container.
        # Never resolve external data references, including absolute paths.
        return "mov", ("-enable_drefs", "0", "-use_absolute_path", "0")
    raise VoiceInputError("VOICE_INPUT_FORMAT")


def _format_ms(value: int) -> str:
    return f"{value // 1_000}.{value % 1_000:03d}"


def _positive_duration(value: object) -> int:
    if not isinstance(value, str | float | int):
        raise ValueError("unavailable duration")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid duration")
    return math.ceil(seconds * 1_000)


def _quality(
    pcm: bytes, duration_ms: int, channels: int, sample_rate: int, channel: int
) -> dict[str, int | list[str]]:
    values = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    peak = max(map(abs, values), default=0)
    if peak < 7:
        raise VoiceInputError("VOICE_INPUT_SILENT")

    warnings: list[str] = []
    if sum(abs(value) >= 32_760 for value in values) * 500 >= len(values):
        warnings.append("clipping")

    block_size = TARGET_SAMPLE_RATE // 10
    blocks = [
        values[start : start + block_size] for start in range(0, len(values), block_size)
    ]
    silent_blocks = sum(
        sum(value * value for value in block) < len(block) * 45**2
        for block in blocks
    )
    if silent_blocks * 2 >= len(blocks):
        warnings.append("mostly_silence")
    if channels > 1:
        warnings.append("first_channel_only" if channel == 0 else "second_channel_only")
    if sample_rate < TARGET_SAMPLE_RATE:
        warnings.append("low_source_sample_rate")
    return {"duration_ms": duration_ms, "warnings": warnings}


def _normalize(
    data: bytes, start_ms: int, end_ms: int, channel: int, *, cancel_event: Event
) -> tuple[bytes, dict[str, int | list[str]]]:
    demuxer, options = _demuxer(data)
    try:
        with tempfile.TemporaryDirectory(prefix="lumen-voice-") as directory:
            source = Path(directory) / "audio"
            source.write_bytes(data)
            # Content selects a fixed demuxer; a private temp path is the only
            # input argument. No playlists, network protocols, or caller paths.
            config = [
                "-v", "error", "-max_alloc", _MAX_SINGLE_ALLOC,
                "-protocol_whitelist", "file", "-f", demuxer, *options,
            ]
            probe = run_media_process(
                [
                    "ffprobe", *config, "-select_streams", "a:0",
                    "-show_entries", "stream=duration,channels,sample_rate:format=duration",
                    "-of", "json", str(source),
                ],
                cancel_event=cancel_event, timeout=_PROBE_SECONDS,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode != 0 or len(probe.stdout) > 8_192:
                raise VoiceInputError("VOICE_INPUT_DECODE")
            metadata = json.loads(probe.stdout)
            stream = metadata["streams"][0]
            measured = [
                _positive_duration(value)
                for value in (stream.get("duration"), metadata.get("format", {}).get("duration"))
                if value not in (None, "N/A")
            ]
            if not measured:
                raise VoiceInputError("VOICE_INPUT_DECODE")
            channels = int(stream["channels"])
            sample_rate = int(stream["sample_rate"])
            if not 1 <= channels <= 8 or not 4_000 <= sample_rate <= 192_000:
                raise VoiceInputError("VOICE_INPUT_DECODE")
            if channel >= channels:
                raise VoiceInputError("VOICE_INPUT_CHANNEL")
            if max(measured) > MAX_SOURCE_MS:
                raise VoiceInputError("VOICE_INPUT_SOURCE_LENGTH")
            if end_ms > min(measured) + MAX_DURATION_DRIFT_MS:
                raise VoiceInputError("VOICE_INPUT_CROP")
            length = end_ms - start_ms
            max_pcm_bytes = length * TARGET_SAMPLE_RATE * 2 // 1_000

            render = run_media_process(
                [
                    # Seek at the input so decoding a short reference near
                    # the end does not traverse the entire preceding clip.
                    # When transcoding, accurate seek discards pre-roll.
                    "ffmpeg", "-nostdin", *config, "-threads", "1",
                    "-ss", _format_ms(start_ms), "-i", str(source), "-t", _format_ms(length),
                    "-map", "0:a:0", "-af", f"pan=mono|c0=c{channel}", "-ar", str(TARGET_SAMPLE_RATE),
                    "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
                ],
                cancel_event=cancel_event, timeout=_DECODE_SECONDS,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            pcm = render.stdout
    except VoiceInputError:
        raise
    except (FileNotFoundError, PermissionError):
        raise VoiceInputError("VOICE_INPUT_UNAVAILABLE") from None
    except subprocess.TimeoutExpired:
        raise VoiceInputError("VOICE_INPUT_TIMEOUT") from None
    except (OSError, TypeError, ValueError, KeyError, IndexError, OverflowError, AttributeError):
        raise VoiceInputError("VOICE_INPUT_DECODE") from None

    if render.returncode != 0 or not pcm or len(pcm) % 2 or len(pcm) > max_pcm_bytes:
        raise VoiceInputError("VOICE_INPUT_DECODE")
    duration_ms = len(pcm) * 1_000 // (TARGET_SAMPLE_RATE * 2)
    if duration_ms < MIN_REFERENCE_MS or duration_ms < length - MAX_DURATION_DRIFT_MS:
        raise VoiceInputError("VOICE_INPUT_CROP")

    quality = _quality(pcm, duration_ms, channels, sample_rate, channel)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, TARGET_SAMPLE_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return output.getvalue(), quality


async def normalize_voice_sample(
    data: bytes, start_ms: int, end_ms: int, channel: int = 0
) -> tuple[bytes, dict[str, int | list[str]]]:
    """Return only the selected single-channel PCM16 clip and sound quality hints."""
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise VoiceInputError("VOICE_INPUT_SIZE")
    if (
        type(start_ms) is not int
        or type(end_ms) is not int
        or start_ms < 0
        or end_ms > MAX_SOURCE_MS
        or not MIN_REFERENCE_MS <= end_ms - start_ms <= MAX_REFERENCE_MS
    ):
        raise VoiceInputError("VOICE_INPUT_CROP")
    if type(channel) is not int or channel not in (0, 1):
        raise VoiceInputError("VOICE_INPUT_CHANNEL")
    return await cancellable_media_thread(_normalize, data, start_ms, end_ms, channel)
