"""Normalize storyboard streams before concatenation; never silently drop audio."""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile


def _run(args: list[str], *, timeout: int = 600) -> bytes:
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("ffmpeg_concat_failed: " + result.stderr.decode("utf-8", "replace")[-1200:])
    return result.stdout


def _probe(path: Path, ffprobe: str) -> dict:
    return json.loads(_run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(path)], timeout=30))


def _video_info(probe: dict) -> tuple[dict, float]:
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("ffmpeg_concat_missing_video")
    duration = float(video.get("duration") or probe.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("ffmpeg_concat_invalid_duration")
    return video, duration


def concat_segments(segment_paths: list[Path]) -> bytes:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg_missing")
    if not segment_paths:
        raise RuntimeError("ffmpeg_concat_empty")
    probes = [_probe(path, ffprobe) for path in segment_paths]
    videos = [_video_info(probe) for probe in probes]
    has_audio = any(any(s.get("codec_type") == "audio" for s in p["streams"]) for p in probes)
    first = videos[0][0]
    width = max(2, int(first["width"]) // 2 * 2)
    height = max(2, int(first["height"]) // 2 * 2)
    # A common frame rate/timebase and raster are required by concat, even if
    # every input has an audio stream. All original segment audio is retained.
    with tempfile.TemporaryDirectory(prefix="lumen-storyboard-") as tmp:
        root = Path(tmp)
        normalized = []
        for index, (path, probe, (_video, duration)) in enumerate(zip(segment_paths, probes, videos, strict=True)):
            output = root / f"segment-{index}.mp4"
            audio = any(s.get("codec_type") == "audio" for s in probe["streams"])
            args = [ffmpeg, "-v", "error", "-y", "-i", str(path)]
            if has_audio and not audio:
                args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            args += ["-map", "0:v:0", "-vf",
                     f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,setpts=PTS-STARTPTS",
                     "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
            if has_audio:
                args += ["-map", "0:a:0" if audio else "1:a:0",
                         "-af", "aresample=48000,asetpts=PTS-STARTPTS,apad",
                         "-c:a", "aac", "-ar", "48000", "-ac", "2"]
            else:
                args += ["-an"]
            args += ["-t", str(duration), "-video_track_timescale", "15360", str(output)]
            _run(args)
            normalized.append(output)
        listing = root / "concat.txt"
        # Paths here are generated names inside our temporary directory.
        listing.write_text("\n".join(f"file '{p.name}'" for p in normalized) + "\n", encoding="utf-8")
        output = root / "assembly.mp4"
        args = [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "1",
                "-i", str(listing), "-map", "0:v:0"]
        if has_audio:
            args += ["-map", "0:a:0"]
        args += ["-c", "copy", "-movflags", "+faststart", str(output)]
        _run(args)
        assembled = _probe(output, ffprobe)
        _stream, duration = _video_info(assembled)
        if has_audio and not any(s.get("codec_type") == "audio" for s in assembled["streams"]):
            raise RuntimeError("ffmpeg_concat_missing_audio")
        expected_duration = sum(duration for _video, duration in videos)
        if abs(duration - expected_duration) > max(0.25, len(videos) / 15):
            raise RuntimeError("ffmpeg_concat_duration_mismatch")
        return output.read_bytes()
