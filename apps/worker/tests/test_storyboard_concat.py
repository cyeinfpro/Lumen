"""Real small-media regressions for mixed storyboard audio layouts."""
from array import array
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from app.storyboard_concat import concat_segments


@pytest.mark.parametrize("audio_layout", [(False, True), (True, False), (True, True), (False, False)])
def test_concat_preserves_segment_audio_and_duration(tmp_path: Path, audio_layout):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for real media validation")
    segments = []
    for index, audio in enumerate(audio_layout):
        path = tmp_path / f"segment-{index}.mp4"
        args = [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
                "-i", f"color=c=blue:s={64 + index * 16}x64:r={24 + index * 6}:d=0.5"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.5"]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "0.5"]
        if audio:
            args += ["-c:a", "aac"]
        subprocess.run([*args, str(path)], check=True, capture_output=True, timeout=30)
        segments.append(path)
    result = tmp_path / "result.mp4"
    result.write_bytes(concat_segments(segments))
    probe = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(result)],
        check=True, capture_output=True, timeout=30,
    ).stdout)
    assert abs(float(probe["format"]["duration"]) - 1.0) < 0.15
    assert any(s["codec_type"] == "audio" for s in probe["streams"]) == any(audio_layout)
    if any(audio_layout):
        pcm = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(result), "-map", "0:a:0", "-f", "s16le",
             "-ac", "1", "-ar", "48000", "-"],
            check=True, capture_output=True, timeout=30,
        ).stdout
        samples = array("h", pcm)
        # Avoid codec priming and the splice boundary; check each segment interior.
        for index, audio in enumerate(audio_layout):
            window = samples[int((index * 0.5 + 0.15) * 48000):int((index * 0.5 + 0.35) * 48000)]
            mean_amplitude = sum(abs(value) for value in window) / len(window)
            assert (mean_amplitude > 200) == audio


def test_concat_rejects_empty_input(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: name)
    with pytest.raises(RuntimeError, match="ffmpeg_concat_empty"):
        concat_segments([])
