from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import video_artifacts, video_upstream
from app.video_artifacts import (
    InvalidVideoArtifactError,
    UnsupportedVideoMediaError,
    copy_video_file_exclusive,
    copy_video_file_exclusive_result,
    detect_video_media,
    downloaded_video_from_bytes,
    hash_video_file,
)
from app.video_upstream import _download_video_url


def _mp4_bytes(*, brand: bytes = b"mp42") -> bytes:
    return (
        b"\x00\x00\x00\x18ftyp"
        + brand
        + b"\x00\x00\x00\x00mp42isom"
        + b"\x00\x00\x00\x08mdat"
    )


def _mpeg_ts_bytes() -> bytes:
    return b"".join(b"\x47" + bytes(187) for _packet in range(3))


@pytest.mark.parametrize(
    ("prefix", "mime", "extension"),
    [
        (_mp4_bytes(), "video/mp4", ".mp4"),
        (_mp4_bytes(brand=b"M4V "), "video/x-m4v", ".m4v"),
        (b"\x1aE\xdf\xa3\x00\x00webm\x00", "video/webm", ".webm"),
        (_mpeg_ts_bytes(), "video/mp2t", ".ts"),
        (
            b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c",
            "video/x-ms-wmv",
            ".wmv",
        ),
    ],
)
def test_detect_video_media_uses_file_signature(
    prefix: bytes,
    mime: str,
    extension: str,
) -> None:
    assert detect_video_media(prefix, "application/octet-stream") == (
        mime,
        extension,
    )


def test_detect_video_media_prefers_signature_over_wrong_header() -> None:
    assert detect_video_media(_mp4_bytes(), "text/html; charset=utf-8") == (
        "video/mp4",
        ".mp4",
    )


@pytest.mark.parametrize(
    "declared_mime",
    ["video/mp4", "video/webm", "application/octet-stream", None],
)
def test_detect_video_media_rejects_unrecognized_bytes(
    declared_mime: str | None,
) -> None:
    with pytest.raises(UnsupportedVideoMediaError):
        detect_video_media(b"<html>not video</html>", declared_mime)


def test_downloaded_video_from_bytes_tracks_real_mime_and_cleans_up() -> None:
    downloaded = downloaded_video_from_bytes(
        _mp4_bytes(),
        declared_mime="video/webm",
    )

    assert downloaded.mime == "video/mp4"
    assert downloaded.extension == ".mp4"
    assert downloaded.path.read_bytes() == _mp4_bytes()

    path = downloaded.path
    downloaded.cleanup()
    assert not path.exists()


def test_postprocess_video_bytes_preserves_detected_extension_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        video_artifacts.shutil,
        "which",
        lambda name: "ffprobe" if name == "ffprobe" else None,
    )
    monkeypatch.setattr(
        video_artifacts,
        "probe_video",
        lambda _ffprobe, _path: {
            "width": 16,
            "height": 16,
            "duration_ms": 1000,
            "fps": 24.0,
            "has_audio": False,
        },
    )

    processed, diagnostics = video_artifacts.postprocess_video_bytes(_mp4_bytes())

    assert processed["mime"] == "video/mp4"
    assert processed["extension"] == ".mp4"
    assert processed["video_bytes"] == _mp4_bytes()
    assert diagnostics["output_mime"] == "video/mp4"
    assert diagnostics["output_extension"] == ".mp4"


def test_postprocess_video_bytes_rejects_missing_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_artifacts.shutil, "which", lambda _name: None)

    with pytest.raises(InvalidVideoArtifactError, match="ffprobe is required"):
        video_artifacts.postprocess_video_bytes(_mp4_bytes())


def test_postprocess_video_cleans_remux_temp_when_ffmpeg_start_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    downloaded = downloaded_video_from_bytes(_mp4_bytes())
    remux_path = tmp_path / "failed-remux.mp4"

    def temporary_path(_suffix: str) -> Path:
        remux_path.touch()
        return remux_path

    def fail_run(*_args: object, **_kwargs: object) -> object:
        raise OSError("ffmpeg unavailable")

    monkeypatch.setattr(
        video_artifacts.shutil,
        "which",
        lambda name: name,
    )
    monkeypatch.setattr(video_artifacts, "_temporary_video_path", temporary_path)
    monkeypatch.setattr(video_artifacts.subprocess, "run", fail_run)

    try:
        with pytest.raises(OSError, match="ffmpeg unavailable"):
            video_artifacts.postprocess_video_file(downloaded)
        assert not remux_path.exists()
    finally:
        downloaded.cleanup()


def test_copy_video_file_exclusive_is_idempotent_and_never_overwrites(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    destination = tmp_path / "stored" / "output.mp4"
    source.write_bytes(_mp4_bytes())
    sha256, size_bytes = hash_video_file(source)

    created = copy_video_file_exclusive_result(
        source,
        destination,
        expected_sha256=sha256,
        expected_size=size_bytes,
    )
    existing = copy_video_file_exclusive_result(
        source,
        destination,
        expected_sha256=sha256,
        expected_size=size_bytes,
    )

    assert created.size == size_bytes
    assert created.created is True
    assert existing.size == size_bytes
    assert existing.created is False
    assert (
        copy_video_file_exclusive(
            source,
            destination,
            expected_sha256=sha256,
            expected_size=size_bytes,
        )
        == size_bytes
    )

    changed = tmp_path / "changed.mp4"
    changed.write_bytes(_mp4_bytes() + b"changed")
    changed_sha256, changed_size = hash_video_file(changed)
    with pytest.raises(FileExistsError):
        copy_video_file_exclusive(
            changed,
            destination,
            expected_sha256=changed_sha256,
            expected_size=changed_size,
        )

    assert destination.read_bytes() == _mp4_bytes()


@pytest.mark.asyncio
async def test_download_video_url_streams_to_temp_file_with_real_media_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = 0

    async def resolve_target(raw_url: str, *, allow_http: bool) -> SimpleNamespace:
        assert allow_http is True
        return SimpleNamespace(url=raw_url, resolved_ips=())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "video/webm; charset=binary",
                "content-length": str(len(_mp4_bytes())),
            },
            content=_mp4_bytes(),
            request=request,
        )

    def client_factory(_target: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    def ensure_active() -> None:
        nonlocal checks
        checks += 1

    monkeypatch.setattr(video_upstream, "resolve_public_http_target", resolve_target)

    downloaded = await _download_video_url(
        "https://cdn.example.com/result",
        client_factory=client_factory,
        ensure_active=ensure_active,
    )
    try:
        assert downloaded.path.is_file()
        assert downloaded.path.read_bytes() == _mp4_bytes()
        assert downloaded.mime == "video/mp4"
        assert downloaded.extension == ".mp4"
        assert downloaded.size_bytes == len(_mp4_bytes())
        assert checks >= 3
    finally:
        downloaded.cleanup()


@pytest.mark.asyncio
async def test_download_video_url_cleans_partial_file_when_lease_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_paths: list[Path] = []
    checks = 0
    real_mkstemp = video_upstream.tempfile.mkstemp

    async def resolve_target(raw_url: str, *, allow_http: bool) -> SimpleNamespace:
        assert allow_http is True
        return SimpleNamespace(url=raw_url, resolved_ips=())

    def capture_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        fd, raw_path = real_mkstemp(*args, **kwargs)
        created_paths.append(Path(raw_path))
        return fd, raw_path

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=_mp4_bytes(),
            request=request,
        )

    def client_factory(_target: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def ensure_active() -> None:
        nonlocal checks
        checks += 1
        if checks >= 2:
            raise RuntimeError("lease lost")

    monkeypatch.setattr(video_upstream, "resolve_public_http_target", resolve_target)
    monkeypatch.setattr(video_upstream.tempfile, "mkstemp", capture_mkstemp)

    with pytest.raises(RuntimeError, match="lease lost"):
        await _download_video_url(
            "https://cdn.example.com/result.mp4",
            client_factory=client_factory,
            ensure_active=ensure_active,
        )

    assert created_paths
    assert all(not path.exists() for path in created_paths)


def test_download_video_url_has_no_in_memory_chunk_accumulator() -> None:
    source = inspect.getsource(_download_video_url)

    assert "async for chunk in response.aiter_bytes" in source
    assert "file_obj.write(chunk)" in source
    assert "chunks: list[bytes]" not in source
