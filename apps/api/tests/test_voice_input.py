"""Exercise voice reference validation with generated waveforms only."""

import io
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from app.voice_input import (
    MAX_SOURCE_BYTES,
    TARGET_SAMPLE_RATE,
    VoiceInputError,
    normalize_voice_sample,
)


def _wav(seconds, *, sample_rate=24_000, channel_freqs=(440,), volume=3_000):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(len(channel_freqs))
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(volume * math.sin(2 * math.pi * freq * n / sample_rate))
                )
                for n in range(sample_rate * seconds)
                for freq in channel_freqs
            )
        )
    return output.getvalue()


def _encode(data, kind, codec):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.wav"
        target = Path(directory) / f"sample.{kind}"
        source.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-c:a", codec, str(target)],
            check=True, timeout=10, capture_output=True,
        )
        return target.read_bytes()


class VoiceInputValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_invalid_crop_without_invoking_decoder(self):
        for start, end in [(-1, 3_000), (0, 2_999), (0, 20_001), (58_000, 62_000)]:
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_CROP$"):
                    await normalize_voice_sample(b"x", start, end)

    async def test_reject_invalid_channel_without_invoking_decoder(self):
        for channel in [-1, 2, True, "1"]:
            with self.subTest(channel=channel):
                with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_CHANNEL$"):
                    await normalize_voice_sample(b"x", 0, 3_000, channel=channel)

    async def test_payload_bound_and_magic_reject(self):
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_SIZE$"):
            await normalize_voice_sample(b"x" * (MAX_SOURCE_BYTES + 1), 0, 3_000)
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_FORMAT$"):
            await normalize_voice_sample(
                b"#EXTM3U\nhttps://example.com/private.wav\n", 0, 3_000
            )


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is unavailable")
class VoiceInputDecoderTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_crop_is_mono_pcm16_and_keeps_first_channel(self):
        src = _wav(5, channel_freqs=(400, 900))
        result, quality = await normalize_voice_sample(src, 1_000, 4_000)
        with wave.open(io.BytesIO(result), "rb") as stream:
            self.assertEqual((stream.getnchannels(), stream.getsampwidth()), (1, 2))
            self.assertEqual(stream.getframerate(), TARGET_SAMPLE_RATE)
            self.assertEqual(stream.getnframes(), 3 * TARGET_SAMPLE_RATE)
            pcm = list(struct.iter_unpack("<h", stream.readframes(TARGET_SAMPLE_RATE)))
        crossings = sum(a[0] < 0 <= b[0] for a, b in zip(pcm, pcm[1:]))
        self.assertLess(abs(crossings - 400), 3)
        self.assertEqual(quality["duration_ms"], 3_000)
        self.assertEqual(quality["warnings"], ["first_channel_only"])

    async def test_second_channel_specifically_uses_right_signal(self):
        src = _wav(5, channel_freqs=(400, 900))
        result, quality = await normalize_voice_sample(src, 1_000, 4_000, channel=1)
        with wave.open(io.BytesIO(result), "rb") as stream:
            pcm = list(struct.iter_unpack("<h", stream.readframes(TARGET_SAMPLE_RATE)))
        crossings = sum(a[0] < 0 <= b[0] for a, b in zip(pcm, pcm[1:]))
        self.assertLess(abs(crossings - 900), 3)
        self.assertEqual(quality["warnings"], ["second_channel_only"])

    async def test_second_channel_rejects_mono_instead_of_falling_back(self):
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_CHANNEL$"):
            await normalize_voice_sample(_wav(4), 0, 3_000, channel=1)

    async def test_accepts_mp3_and_m4a(self):
        original = _wav(5)
        for kind, codec in [("mp3", "libmp3lame"), ("m4a", "aac")]:
            with self.subTest(kind=kind):
                result, info = await normalize_voice_sample(
                    _encode(original, kind, codec), 1_000, 4_000
                )
                self.assertTrue(result.startswith(b"RIFF"))
                self.assertEqual(info["duration_ms"], 3_000)

    async def test_seeked_reference_is_from_late_requested_section(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-sections.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 8_000, 0, "NONE", "not compressed"))
                for seconds, frequency in ((50, 200), (5, 700)):
                    one_second = b"".join(
                        struct.pack("<h", int(3_000 * math.sin(2 * math.pi * frequency * n / 8_000)))
                        for n in range(8_000)
                    )
                    wav.writeframesraw(one_second * seconds)
            original = source.read_bytes()
        for data in (original, _encode(original, "m4a", "aac")):
            result, info = await normalize_voice_sample(data, 51_000, 54_000)
            self.assertEqual(info["duration_ms"], 3_000)
            with wave.open(io.BytesIO(result), "rb") as wav:
                wav.setpos(TARGET_SAMPLE_RATE)
                pcm = list(struct.iter_unpack("<h", wav.readframes(TARGET_SAMPLE_RATE)))
            crossings = sum(a[0] < 0 <= b[0] for a, b in zip(pcm, pcm[1:]))
            self.assertLess(abs(crossings - 700), 3)

    async def test_bounds_actual_duration_and_rejects_silent_samples(self):
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_SOURCE_LENGTH$"):
            await normalize_voice_sample(_wav(61, sample_rate=8_000), 0, 3_000)
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_CROP$"):
            await normalize_voice_sample(_wav(4), 1_500, 4_500)
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_SILENT$"):
            await normalize_voice_sample(_wav(4, volume=0), 0, 3_000)

    async def test_truncated_wav_error_does_not_leak_decoder_text(self):
        with self.assertRaisesRegex(VoiceInputError, "^VOICE_INPUT_DECODE$"):
            await normalize_voice_sample(b"RIFF\x00\x00\x00\x00WAVEbroken", 0, 3_000)
