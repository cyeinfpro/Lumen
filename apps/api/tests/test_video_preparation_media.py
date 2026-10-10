"""Opt-in real-media smoke; uses only existing local ffmpeg/ffprobe."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

import pytest

from app.services.video_preparation import PreparationClaim
from app.services.video_preparation_posters import (
    install_preparation_poster,
    poster_slot,
    render_preparation_poster,
    validate_poster,
)
from app.video_reference_videos import inspect_video_reference_original

pytestmark = pytest.mark.skipif(
    os.environ.get("LUMEN_PREPARATION_MEDIA_TEST") != "1",
    reason="real-media smoke is explicitly scheduled separately",
)


def test_real_two_second_original_generates_valid_hash_bound_poster(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("existing ffmpeg/ffprobe unavailable")
    key = "u/synthetic-user/vref/synthetic-video/original.mp4"
    source = tmp_path / key
    source.parent.mkdir(parents=True)
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=24:d=2",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(source),
        ],
        timeout=30,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    inspected = inspect_video_reference_original(
        storage_root=str(tmp_path),
        storage_key=key,
        size_bytes=len(original),
        sha256=digest,
    )
    assert (inspected["width"], inspected["height"], inspected["duration_ms"]) == (
        320,
        180,
        2000,
    )
    claim = PreparationClaim(
        "synthetic-video",
        "synthetic-user",
        key,
        digest,
        len(original),
        "synthetic-claim",
        9999999999,
    )
    slot = poster_slot(claim)
    data = render_preparation_poster(claim, slot, storage_root=str(tmp_path))
    info = validate_poster(data)
    slot = {**slot, "sha256": info["sha256"], "rendered_size_bytes": info["size_bytes"]}
    installed = install_preparation_poster(
        claim, slot, data, storage_root=str(tmp_path)
    )
    assert installed["width"] <= 640 and installed["height"] <= 640
    assert installed["state"] == "ready"
    assert source.read_bytes() == original
    assert (
        render_preparation_poster(claim, installed, storage_root=str(tmp_path)) == data
    )
