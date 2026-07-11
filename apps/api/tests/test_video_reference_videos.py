from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import video_reference_videos
from app.video_reference_videos import VideoReferenceVideoError


@pytest.mark.parametrize("streams", [None, {}, "not-a-list"])
def test_probe_video_rejects_non_list_streams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    streams: object,
) -> None:
    payload = json.dumps({"streams": streams}).encode()

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=payload,
            stderr=b"",
        )

    monkeypatch.setattr(video_reference_videos.subprocess, "run", fake_run)

    with pytest.raises(VideoReferenceVideoError) as exc_info:
        video_reference_videos._probe_video("ffprobe", tmp_path / "source.mp4")

    assert exc_info.value.code == "invalid_video"
    assert exc_info.value.message == "reference video has no video stream"
