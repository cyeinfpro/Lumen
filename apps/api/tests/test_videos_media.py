from __future__ import annotations

import io
import hashlib
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request, UploadFile
from PIL import Image as PILImage
from lumen_core.schemas import VideoCreateIn, VideoPriceOptionOut, VideoReferenceMediaIn
from lumen_core.video_providers import VideoProviderDefinition

from app.routes import events, videos
from app.volcano_asset_media import VOLCANO_ASSET_VIDEO_KIND
from app.video_reference_images import (
    VIDEO_REFERENCE_IMAGE_KIND,
    VideoReferenceImageError,
    make_video_reference_jpeg,
)
from app.video_reference_videos import (
    VIDEO_REFERENCE_VIDEO_KIND,
    VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT,
    _fit_even_dimensions,
)


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/videos/video-1/binary",
            "headers": headers or [],
            "client": ("127.0.0.1", 12345),
        }
    )


async def _body(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_reference_video_upload_inspection_hashes_stream_and_rewinds() -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + (b"x" * 1024)
    upload = UploadFile(file=io.BytesIO(payload), filename="reference.mp4")

    size, sha, header = await videos._inspect_reference_video_upload(upload)

    assert size == len(payload)
    assert sha == hashlib.sha256(payload).hexdigest()
    assert header == payload[:12]
    assert upload.file.tell() == 0


@pytest.mark.asyncio
async def test_reference_video_upload_inspection_stops_at_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(videos, "_VIDEO_REFERENCE_UPLOAD_MAX_BYTES", 10)
    upload = UploadFile(file=io.BytesIO(b"01234567890"), filename="large.mp4")

    with pytest.raises(HTTPException) as exc_info:
        await videos._inspect_reference_video_upload(upload)

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_reference_video_dedupe_repairs_missing_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"\x00\x00\x00\x18ftypisom" + (b"x" * 256)
    upload = UploadFile(
        file=io.BytesIO(payload),
        filename="reference.mp4",
        headers={"content-type": "video/mp4"},
    )
    existing = SimpleNamespace(
        id="video-1",
        user_id="user-1",
        owner_generation_id=None,
        storage_key="u/user-1/vref/video-1/original.mp4",
        poster_storage_key=None,
        mime="video/mp4",
        width=0,
        height=0,
        duration_ms=0,
        fps=None,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        etag=hashlib.sha256(payload).hexdigest(),
        has_audio=False,
        faststart=False,
        visibility="private",
        metadata_jsonb={"source": "uploaded_reference"},
        created_at=datetime.now(timezone.utc),
    )

    class Result:
        def __init__(self, value: Any = None) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self) -> None:
            self.results = [Result(), Result(existing)]
            self.committed = False
            self.rolled_back = False

        async def execute(self, _statement: Any) -> Result:
            return self.results.pop(0)

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            self.rolled_back = True

        async def refresh(self, _value: Any) -> None:
            return None

    monkeypatch.setattr(videos.settings, "storage_root", str(tmp_path))
    db = Db()

    out = await videos.upload_reference_video(
        SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
        db,  # type: ignore[arg-type]
        upload,
    )

    repaired = tmp_path / existing.storage_key
    assert out.id == existing.id
    assert repaired.read_bytes() == payload
    assert db.committed is True
    assert db.rolled_back is False


def test_reference_video_atomic_writer_streams_from_file_object(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    source = io.BytesIO(b"streamed-video")

    videos._write_new_file_atomic(path, source)

    assert path.read_bytes() == b"streamed-video"


@pytest.mark.asyncio
async def test_video_media_response_serves_full_file(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")

    response = videos._media_response(  # noqa: SLF001
        _request(),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
    )

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == "10"
    assert response.headers["etag"] == '"abc123"'
    assert "immutable" in response.headers["cache-control"]
    assert await _body(response) == b"0123456789"


@pytest.mark.asyncio
async def test_video_media_response_supports_single_byte_range(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")

    response = videos._media_response(  # noqa: SLF001
        _request([(b"range", b"bytes=2-5")]),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"
    assert await _body(response) == b"2345"


def test_video_media_response_rejects_invalid_range(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")

    response = videos._media_response(  # noqa: SLF001
        _request([(b"range", b"bytes=20-30")]),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


@pytest.mark.asyncio
async def test_reference_video_binary_serves_upstream_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "u/user-1/vref/video-1/original.mov"
    variant = tmp_path / "u/user-1/vref/video-1/video-1.safe.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    variant.write_bytes(b"variant")
    token = "token-1234567890"
    video = SimpleNamespace(
        id="video-1",
        storage_key="u/user-1/vref/video-1/original.mov",
        mime="video/quicktime",
        etag="orig-etag",
        sha256="orig-sha",
        updated_at=datetime.now(timezone.utc),
        deleted_at=None,
        metadata_jsonb={
            "reference_access_token": token,
            "reference_access_token_expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            "upstream_reference_video_variant": {
                "kind": VIDEO_REFERENCE_VIDEO_KIND,
                "storage_key": "u/user-1/vref/video-1/video-1.safe.mp4",
                "sha256": "variant-sha",
            },
        },
    )

    class Result:
        def scalar_one_or_none(self):
            return video

    class Db:
        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(videos.settings, "storage_root", str(tmp_path))

    response = await videos.reference_video_binary(  # noqa: SLF001
        "video-1",
        _request(),
        Db(),  # type: ignore[arg-type]
        token=token,
        variant=VIDEO_REFERENCE_VIDEO_KIND,
    )

    assert response.status_code == 200
    assert response.headers["content-length"] == "7"
    assert response.headers["etag"] == '"variant-sha"'
    assert await _body(response) == b"variant"


@pytest.mark.asyncio
async def test_reference_video_binary_serves_volcano_asset_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "u/user-1/vref/video-1/original.mov"
    variant = tmp_path / "u/user-1/vref/video-1/video-1.volcano.mp4"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original")
    variant.write_bytes(b"volcano-variant")
    token = "token-1234567890"
    video = SimpleNamespace(
        id="video-1",
        storage_key="u/user-1/vref/video-1/original.mov",
        mime="video/quicktime",
        etag="orig-etag",
        sha256="orig-sha",
        updated_at=datetime.now(timezone.utc),
        deleted_at=None,
        metadata_jsonb={
            "reference_access_token": token,
            "reference_access_token_expires_at": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            "volcano_asset_video_variant": {
                "kind": VOLCANO_ASSET_VIDEO_KIND,
                "storage_key": "u/user-1/vref/video-1/video-1.volcano.mp4",
                "sha256": hashlib.sha256(b"volcano-variant").hexdigest(),
            },
        },
    )

    class Result:
        def scalar_one_or_none(self):
            return video

    class Db:
        async def execute(self, _statement):
            return Result()

    monkeypatch.setattr(videos.settings, "storage_root", str(tmp_path))

    response = await videos.reference_video_binary(  # noqa: SLF001
        "video-1",
        _request(),
        Db(),  # type: ignore[arg-type]
        token=token,
        variant=VOLCANO_ASSET_VIDEO_KIND,
    )

    assert response.status_code == 200
    assert response.headers["content-length"] == str(len(b"volcano-variant"))
    assert response.headers["etag"] == (
        f'"{hashlib.sha256(b"volcano-variant").hexdigest()}"'
    )
    assert response.headers["content-disposition"] == (
        'inline; filename="lumen-asset-video-1.mp4"'
    )
    assert await _body(response) == b"volcano-variant"

    named = await videos.reference_video_binary_named(  # noqa: SLF001
        "video-1",
        "lumen-asset-video-1.mp4",
        _request(),
        Db(),  # type: ignore[arg-type]
        token=token,
        variant=VOLCANO_ASSET_VIDEO_KIND,
    )
    assert named.status_code == 200
    assert named.headers["content-type"].startswith("video/mp4")


@pytest.mark.asyncio
async def test_video_media_response_can_force_attachment_download(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")

    response = videos._media_response(  # noqa: SLF001
        _request(),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
        download_filename="lumen-video-video-1.mp4",
    )

    assert response.status_code == 200
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="lumen-video-video-1.mp4"'
    )
    assert await _body(response) == b"0123456789"


def test_video_media_response_honors_if_none_match(tmp_path: Path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")

    response = videos._media_response(  # noqa: SLF001
        _request([(b"if-none-match", b'"abc123"')]),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
    )

    assert response.status_code == 304
    assert response.headers["etag"] == '"abc123"'


def test_video_media_response_honors_weak_multi_and_wildcard_etags(
    tmp_path: Path,
) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"0123456789")

    multi = videos._media_response(  # noqa: SLF001
        _request([(b"if-none-match", b'"other", W/"abc123"')]),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
    )
    wildcard = videos._media_response(  # noqa: SLF001
        _request([(b"if-none-match", b"*")]),
        path,
        media_type="video/mp4",
        etag="abc123",
        last_modified=None,
        immutable=True,
    )

    assert multi.status_code == 304
    assert wildcard.status_code == 304


def _volcano_tos_url(
    *,
    signed_at: str = "20260627T092821Z",
    expires_s: int = 86400,
) -> str:
    return (
        "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/"
        "doubao-seedance-2-0/output.mp4"
        "?X-Tos-Algorithm=TOS4-HMAC-SHA256"
        "&X-Tos-Credential=AKLT%2F20260627%2Fcn-beijing%2Ftos%2Frequest"
        f"&X-Tos-Date={signed_at}"
        f"&X-Tos-Expires={expires_s}"
        "&X-Tos-Signature=abc123"
        "&X-Tos-SignedHeaders=host"
    )


def test_temporary_video_download_exposes_unexpired_volcano_tos_url() -> None:
    row = SimpleNamespace(
        provider_kind="volcano",
        upstream_response={"content": {"video_url": _volcano_tos_url()}},
    )

    out = videos._temporary_video_download_out(  # noqa: SLF001
        row,
        now=datetime(2026, 6, 27, 9, 29, 21, tzinfo=timezone.utc),
    )

    assert out is not None
    assert out.source == "volcano"
    assert out.url == _volcano_tos_url()
    assert out.expires_at == datetime(2026, 6, 28, 9, 28, 21, tzinfo=timezone.utc)
    assert out.expires_in_s == 86_340


def test_temporary_video_download_hides_expired_or_near_expired_urls() -> None:
    row = SimpleNamespace(
        provider_kind="volcano",
        upstream_response={"content": {"video_url": _volcano_tos_url()}},
    )

    out = videos._temporary_video_download_out(  # noqa: SLF001
        row,
        now=datetime(2026, 6, 28, 9, 27, 31, tzinfo=timezone.utc),
    )

    assert out is None


def test_temporary_video_download_requires_volcano_tos_signature() -> None:
    unsigned = SimpleNamespace(
        provider_kind="volcano",
        upstream_response={"content": {"video_url": "https://cdn.example/output.mp4"}},
    )
    spoofed = SimpleNamespace(
        provider_kind="volcano",
        upstream_response={
            "content": {
                "video_url": _volcano_tos_url().replace(
                    "ark-acg-cn-beijing.tos-cn-beijing.volces.com",
                    "cdn.example",
                )
            }
        },
    )
    other_provider = SimpleNamespace(
        provider_kind="omni_flash",
        upstream_response={"content": {"video_url": _volcano_tos_url()}},
    )

    assert (
        videos._temporary_video_download_out(  # noqa: SLF001
            unsigned,
            now=datetime(2026, 6, 27, 9, 29, 21, tzinfo=timezone.utc),
        )
        is None
    )
    assert (
        videos._temporary_video_download_out(  # noqa: SLF001
            spoofed,
            now=datetime(2026, 6, 27, 9, 29, 21, tzinfo=timezone.utc),
        )
        is None
    )
    assert (
        videos._temporary_video_download_out(  # noqa: SLF001
            other_provider,
            now=datetime(2026, 6, 27, 9, 29, 21, tzinfo=timezone.utc),
        )
        is None
    )


def test_generation_elapsed_ms_uses_finished_at_for_terminal_rows() -> None:
    row = SimpleNamespace(
        created_at=datetime(2026, 6, 27, 9, 14, 35, tzinfo=timezone.utc),
        finished_at=datetime(2026, 6, 27, 9, 28, 50, tzinfo=timezone.utc),
    )

    assert videos._generation_elapsed_ms(row) == 855_000  # noqa: SLF001


def test_generation_elapsed_ms_uses_now_for_active_rows() -> None:
    row = SimpleNamespace(
        created_at=datetime(2026, 6, 27, 9, 14, 35, tzinfo=timezone.utc),
        finished_at=None,
    )

    assert (
        videos._generation_elapsed_ms(  # noqa: SLF001
            row,
            now=datetime(2026, 6, 27, 9, 15, 5, 500000, tzinfo=timezone.utc),
        )
        == 30_500
    )


def test_video_duration_options_include_smart_duration() -> None:
    assert (
        videos._duration_options(  # noqa: SLF001
            {"seedance-2.0": {"t2v": {"720p:5": 60_000, "720p:15": 180_000}}}
        )[0]
        == -1
    )


def test_reference_action_accepts_any_reference_pricing_path() -> None:
    model = "seedance-2.0"

    assert videos._has_video_price(  # noqa: SLF001
        {(model, "t2v", None)},
        model=model,
        action="t2v",
    )
    assert not videos._has_video_price(  # noqa: SLF001
        {(model, "t2v", "720p")},
        model=model,
        action="t2v",
    )
    assert videos._has_video_price(  # noqa: SLF001
        {(model, "reference_image", "720p")},
        model=model,
        action="reference",
        resolutions=["720p"],
    )
    assert videos._has_video_price(  # noqa: SLF001
        {(model, "reference_image", "720p"), (model, "reference_video", "720p")},
        model=model,
        action="reference",
        resolutions=["720p"],
    )
    assert videos._has_video_price(  # noqa: SLF001
        {(model, "reference", None)},
        model=model,
        action="reference",
        resolutions=["1080p"],
    )
    assert not videos._has_video_price(  # noqa: SLF001
        {(model, "reference_image", "480p"), (model, "reference_video", "480p")},
        model=model,
        action="reference",
        resolutions=["720p"],
    )


def test_video_cursor_requires_timezone() -> None:
    with pytest.raises(HTTPException) as excinfo:
        videos._decode_cursor("2026-06-10T12:00:00|row-1")  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_cursor"


def test_seedance_20_resolution_options_match_official_model_limits() -> None:
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "seedance-2.0-fast",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "seedance-2.0",
        upstream_model="doubao-seedance-2-0-fast-260128",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "seedance-2.0",
        upstream_model="doubao-seedance-2-0-mini-260128",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "seedance-2.0-mini",
        upstream_model="doubao-seedance-2-0-mini-260615",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "seedance-2.0",
        upstream_model="doubao-seedance-2-0-260128",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p", "1080p", "4k"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "video-ds-2.0",
        upstream_model="video-ds-2.0",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p", "1080p", "4k"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "video-ds-2.0-fast",
        upstream_model="video-ds-2.0-fast",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["480p", "720p"]


def test_omni_flash_duration_options_are_model_specific() -> None:
    assert videos._duration_options_for_model(  # noqa: SLF001
        "omni-flash",
        available_durations=[-1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    ) == [6, 7, 8, 9, 10]
    assert videos._duration_options_for_model(  # noqa: SLF001
        "seedance-2.0",
        available_durations=[5, 6],
    ) == [-1, 5, 6]


def test_happyhorse_resolution_options_exclude_480p() -> None:
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "happyhorse-1.0",
        available_resolutions=["480p", "720p", "1080p"],
    ) == ["720p", "1080p"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "hh",
        upstream_model="happyhorse-1.0-t2v",
        available_resolutions=["480p", "720p", "1080p"],
    ) == ["720p", "1080p"]


def test_omni_flash_resolution_options_exclude_480p() -> None:
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "omni-flash",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["720p", "1080p", "4k"]
    assert videos._video_resolution_options_for_model(  # noqa: SLF001
        "video",
        upstream_model="gemini_omni_flash",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["720p", "1080p", "4k"]


def test_volcano_newapi_resolution_options_are_720p_only() -> None:
    assert videos._video_resolution_options_for_provider(  # noqa: SLF001
        "volcano_newapi",
        "video-ds-2.0",
        upstream_model="video-ds-2.0",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["720p"]
    assert videos._video_resolution_options_for_provider(  # noqa: SLF001
        "volcano_newapi",
        "video-ds-2.0-fast",
        upstream_model="video-ds-2.0-fast",
        available_resolutions=["480p", "720p", "1080p", "4k"],
    ) == ["720p"]


@pytest.mark.asyncio
async def test_video_options_exposes_billing_model_for_provider_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-fast-260128"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {"seedance-2.0-fast": {"t2v": {"720p:5": 108_900}}}

    async def provider_state(_db):
        return [provider], []

    async def price_options(_db):
        return [
            VideoPriceOptionOut(
                model="seedance-2.0-fast",
                action="t2v",
                resolution="720p",
                variant="t2v_720p",
                price=videos._money(37_000_000),  # noqa: SLF001
                enabled=True,
            )
        ]

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)
    monkeypatch.setattr(videos, "_video_price_options", price_options)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        object(),
    )

    assert options.enabled is True
    assert len(options.models) == 1
    assert options.models[0].model == "seedance-2.0"
    assert options.models[0].billing_model == "seedance-2.0-fast"
    assert options.models[0].billing_models == {"t2v": "seedance-2.0-fast"}


@pytest.mark.asyncio
async def test_video_options_exposes_seedance_20_standard_4k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "seedance-2.0": {
                "t2v": {
                    "720p:5": 108_900,
                    "1080p:5": 242_942,
                    "4k:5": 971_924,
                }
            }
        }

    async def provider_state(_db):
        return [provider], []

    async def price_options(_db):
        return [
            VideoPriceOptionOut(
                model="seedance-2.0",
                action="t2v",
                resolution="720p",
                variant="t2v_720p",
                price=videos._money(46_000_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="seedance-2.0",
                action="t2v",
                resolution="1080p",
                variant="t2v_1080p",
                price=videos._money(51_000_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="seedance-2.0",
                action="t2v",
                resolution="4k",
                variant="t2v_4k",
                price=videos._money(26_000_000),  # noqa: SLF001
                enabled=True,
            ),
        ]

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)
    monkeypatch.setattr(videos, "_video_price_options", price_options)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        object(),
    )

    assert options.enabled is True
    assert len(options.models) == 1
    assert options.models[0].model == "seedance-2.0"
    assert options.models[0].actions == ["t2v"]
    assert options.models[0].resolutions == ["720p", "1080p", "4k"]


@pytest.mark.asyncio
async def test_video_options_exposes_seedance_20_mini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={
            "seedance-2.0-mini:t2v": "doubao-seedance-2-0-mini-260615",
            "seedance-2.0-mini:i2v": "doubao-seedance-2-0-mini-260615",
            "seedance-2.0-mini:reference": "doubao-seedance-2-0-mini-260615",
        },
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "seedance-2.0-mini": {
                "t2v": {"480p:4": 51_429, "720p:4": 108_900},
                "i2v": {"480p:4": 51_429, "720p:4": 108_900},
                "reference_image": {"480p:4": 51_429, "720p:4": 108_900},
                "reference_video": {"480p:4": 190_000, "720p:4": 411_668},
            }
        }

    async def provider_state(_db):
        return [provider], []

    async def price_options(_db):
        return [
            VideoPriceOptionOut(
                model="seedance-2.0-mini",
                action=action,  # type: ignore[arg-type]
                resolution=resolution,
                variant=f"{action}_{resolution}",
                price=videos._money(
                    14_000_000 if action == "reference_video" else 23_000_000
                ),  # noqa: SLF001
                enabled=True,
            )
            for action in (
                "t2v",
                "i2v",
                "reference",
                "reference_image",
                "reference_video",
            )
            for resolution in ("480p", "720p")
        ]

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)
    monkeypatch.setattr(videos, "_video_price_options", price_options)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        object(),
    )

    assert options.enabled is True
    assert len(options.models) == 1
    assert options.models[0].model == "seedance-2.0-mini"
    assert options.models[0].billing_model == "seedance-2.0-mini"
    assert set(options.models[0].actions) == {"t2v", "i2v", "reference"}
    assert options.models[0].durations_s == [-1, 4]
    assert options.models[0].resolutions == ["480p", "720p"]
    assert options.models[0].reference_media_limits == {
        "image": 9,
        "video": 3,
        "audio": 3,
    }


@pytest.mark.asyncio
async def test_video_options_exposes_happyhorse_reference_with_image_pricing_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VideoProviderDefinition(
        name="dashscope",
        kind="dashscope",
        base_url="https://dashscope-intl.aliyuncs.com",
        api_key="sk-test",
        models={
            "happyhorse-1.0:t2v": "happyhorse-1.0-t2v",
            "happyhorse-1.0:i2v": "happyhorse-1.0-i2v",
            "happyhorse-1.0:reference": "happyhorse-1.0-r2v",
        },
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "happyhorse-1.0": {
                "t2v": {"720p:3": 3_000_000},
                "i2v": {"720p:3": 3_000_000},
                "reference_image": {"720p:3": 3_000_000},
            }
        }

    async def provider_state(_db):
        return [provider], []

    async def price_options(_db):
        return [
            VideoPriceOptionOut(
                model="happyhorse-1.0",
                action="t2v",
                resolution="720p",
                variant="t2v_720p",
                price=videos._money(1_008_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="happyhorse-1.0",
                action="i2v",
                resolution="720p",
                variant="i2v_720p",
                price=videos._money(1_008_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="happyhorse-1.0",
                action="reference_image",
                resolution="720p",
                variant="reference_image_720p",
                price=videos._money(1_008_000),  # noqa: SLF001
                enabled=True,
            ),
        ]

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)
    monkeypatch.setattr(videos, "_video_price_options", price_options)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        object(),
    )

    assert options.enabled is True
    assert len(options.models) == 1
    assert options.models[0].model == "happyhorse-1.0"
    assert set(options.models[0].actions) == {"t2v", "i2v", "reference"}
    assert options.models[0].durations_s == [-1, 3]
    assert options.models[0].resolutions == ["720p"]
    assert options.models[0].reference_media_limits == {"image": 9}


@pytest.mark.asyncio
async def test_video_options_disables_byok_without_loading_wallet_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("BYOK options must not load wallet video runtime")

    monkeypatch.setattr(videos, "_video_enabled", unexpected)
    monkeypatch.setattr(videos, "_video_hold_estimates", unexpected)
    monkeypatch.setattr(videos, "_video_provider_state", unexpected)
    monkeypatch.setattr(videos, "_video_price_options", unexpected)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1", account_mode="byok"),
        object(),
    )

    assert options.enabled is False
    assert options.models == []
    assert options.unavailable_reason == "account_mode_forbidden"


@pytest.mark.asyncio
async def test_video_options_scopes_seedance_reference_duration_by_model_action_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:reference": "doubao-seedance-2-0-260128"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "happyhorse-1.0": {"reference_image": {"1080p:3": 3_000_000}},
            "seedance-2.0": {
                "reference_image": {
                    "1080p:4": 242_942,
                    "1080p:5": 242_942,
                }
            },
        }

    async def provider_state(_db):
        return [provider], []

    async def price_options(_db):
        return [
            VideoPriceOptionOut(
                model="seedance-2.0",
                action="reference_image",
                resolution="1080p",
                variant="reference_image_1080p",
                price=videos._money(51_000_000),  # noqa: SLF001
                enabled=True,
            )
        ]

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)
    monkeypatch.setattr(videos, "_video_price_options", price_options)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        object(),
    )

    assert options.durations_s == [-1, 3, 4, 5]
    assert len(options.models) == 1
    assert options.models[0].model == "seedance-2.0"
    assert options.models[0].durations_s == [-1, 4, 5]
    assert options.models[0].durations_by_action == {"reference": [-1, 4, 5]}
    assert options.models[0].durations_by_action_resolution == {
        "reference": {"1080p": [-1, 4, 5]}
    }


@pytest.mark.asyncio
async def test_video_options_exposes_omni_flash_reference_with_image_pricing_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={
            "omni-flash:t2v": "gemini_omni_flash",
            "omni-flash:i2v": "gemini_omni_flash",
            "omni-flash:reference": "gemini_omni_flash",
        },
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "omni-flash": {
                "t2v": {
                    "720p:6": 6_000_000,
                    "1080p:6": 6_000_000,
                    "4k:6": 6_000_000,
                },
                "i2v": {
                    "720p:6": 6_000_000,
                    "1080p:6": 6_000_000,
                    "4k:6": 6_000_000,
                },
                "reference_image": {
                    "720p:6": 6_000_000,
                    "1080p:6": 6_000_000,
                    "4k:6": 6_000_000,
                },
            }
        }

    async def provider_state(_db):
        return [provider], []

    async def price_options(_db):
        return [
            VideoPriceOptionOut(
                model="omni-flash",
                action="t2v",
                resolution="720p",
                variant="t2v_720p",
                price=videos._money(1_008_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="t2v",
                resolution="1080p",
                variant="t2v_1080p",
                price=videos._money(1_728_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="t2v",
                resolution="4k",
                variant="t2v_4k",
                price=videos._money(3_456_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="i2v",
                resolution="720p",
                variant="i2v_720p",
                price=videos._money(1_008_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="i2v",
                resolution="1080p",
                variant="i2v_1080p",
                price=videos._money(1_728_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="i2v",
                resolution="4k",
                variant="i2v_4k",
                price=videos._money(3_456_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="reference_image",
                resolution="720p",
                variant="reference_image_720p",
                price=videos._money(1_008_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="reference_image",
                resolution="1080p",
                variant="reference_image_1080p",
                price=videos._money(1_728_000),  # noqa: SLF001
                enabled=True,
            ),
            VideoPriceOptionOut(
                model="omni-flash",
                action="reference_image",
                resolution="4k",
                variant="reference_image_4k",
                price=videos._money(3_456_000),  # noqa: SLF001
                enabled=True,
            ),
        ]

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)
    monkeypatch.setattr(videos, "_video_price_options", price_options)

    options = await videos.video_options(  # type: ignore[arg-type]
        SimpleNamespace(id="user-1"),
        object(),
    )

    assert options.enabled is True
    assert len(options.models) == 1
    assert options.models[0].model == "omni-flash"
    assert set(options.models[0].actions) == {"t2v", "i2v", "reference"}
    assert options.models[0].durations_s == [6, 7, 8, 9, 10]
    assert options.models[0].resolutions == ["720p", "1080p", "4k"]


@pytest.mark.asyncio
async def test_video_create_rejects_seedance_20_fast_1080p(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn.model_construct(
        action="t2v",
        model="seedance-2.0-fast",
        prompt="make a clip",
        duration_s=5,
        resolution="1080p",
        aspect_ratio="16:9",
        idempotency_key="idem-fast-1080p",
    )
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={
            "seedance-2.0-fast:t2v": "doubao-seedance-2-0-fast-260128",
        },
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "seedance-2.0-fast": {
                "t2v": {"480p:5": 60_000, "720p:5": 60_000, "1080p:5": 130_000}
            }
        }

    async def provider_state(_db):
        return [provider], []

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_billing_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)

    with pytest.raises(HTTPException) as excinfo:
        await videos._require_video_create_ready(object(), body)  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_resolution"
    assert excinfo.value.detail["error"]["details"]["available_resolutions"] == [
        "480p",
        "720p",
    ]


@pytest.mark.asyncio
async def test_video_create_rejects_seedance_20_mini_1080p(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn.model_construct(
        action="t2v",
        model="seedance-2.0-mini",
        prompt="make a clip",
        duration_s=4,
        resolution="1080p",
        aspect_ratio="16:9",
        idempotency_key="idem-mini-1080p",
    )
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={
            "seedance-2.0-mini:t2v": "doubao-seedance-2-0-mini-260615",
        },
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "seedance-2.0-mini": {
                "t2v": {
                    "480p:4": 51_429,
                    "720p:4": 108_900,
                    "1080p:4": 244_800,
                }
            }
        }

    async def provider_state(_db):
        return [provider], []

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_billing_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)

    with pytest.raises(HTTPException) as excinfo:
        await videos._require_video_create_ready(object(), body)  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_resolution"
    assert excinfo.value.detail["error"]["details"]["available_resolutions"] == [
        "480p",
        "720p",
    ]


@pytest.mark.asyncio
async def test_video_create_rejects_seedance_reference_duration_leaked_from_other_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn.model_construct(
        action="reference",
        model="seedance-2.0",
        prompt="animate these references",
        reference_media=[
            VideoReferenceMediaIn(kind="image", url="https://example.com/ref.png")
        ],
        duration_s=3,
        resolution="1080p",
        aspect_ratio="16:9",
        idempotency_key="idem-seedance-reference-3s",
    )
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:reference": "doubao-seedance-2-0-260128"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "happyhorse-1.0": {"reference_image": {"1080p:3": 3_000_000}},
            "seedance-2.0": {
                "reference_image": {
                    "1080p:4": 242_942,
                    "1080p:5": 242_942,
                }
            },
        }

    async def provider_state(_db):
        return [provider], []

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_billing_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)

    with pytest.raises(HTTPException) as excinfo:
        await videos._require_video_create_ready(object(), body)  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_duration"
    assert excinfo.value.detail["error"]["details"]["available_durations_s"] == [
        -1,
        4,
        5,
    ]


@pytest.mark.asyncio
async def test_video_create_rejects_duration_from_other_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn(
        action="reference",
        model="seedance-2.0",
        prompt="animate these references",
        reference_media=[
            VideoReferenceMediaIn(kind="image", url="https://example.com/ref.png")
        ],
        duration_s=5,
        resolution="1080p",
        aspect_ratio="16:9",
        idempotency_key="idem-seedance-reference-1080p-missing",
    )
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:reference": "doubao-seedance-2-0-260128"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "happyhorse-1.0": {"reference_image": {"1080p:3": 3_000_000}},
            "seedance-2.0": {"reference_image": {"720p:5": 108_900}},
        }

    async def provider_state(_db):
        return [provider], []

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_billing_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)

    with pytest.raises(HTTPException) as excinfo:
        await videos._require_video_create_ready(object(), body)  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_duration"
    assert excinfo.value.detail["error"]["details"]["available_durations_s"] == []


@pytest.mark.asyncio
async def test_video_create_accepts_seedance_20_standard_4k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn(
        action="t2v",
        model="seedance-2.0",
        prompt="make a 4k clip",
        duration_s=5,
        resolution="4k",
        aspect_ratio="16:9",
        idempotency_key="idem-standard-4k",
    )
    provider = VideoProviderDefinition(
        name="volcano-main",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {"seedance-2.0": {"t2v": {"4k:5": 971_924}}}

    async def provider_state(_db):
        return [provider], []

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_billing_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)

    selected, ready_estimates = await videos._require_video_create_ready(  # noqa: SLF001
        object(), body
    )

    assert selected is provider
    assert ready_estimates["seedance-2.0"]["t2v"]["4k:5"] == 971_924


@pytest.mark.asyncio
async def test_video_create_rejects_omni_flash_unsupported_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = VideoCreateIn(
        action="t2v",
        model="omni-flash",
        prompt="make a clip",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idem-omni-5s",
    )
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={"omni-flash:t2v": "gemini_omni_flash"},
    )

    async def enabled(_db) -> bool:
        return True

    async def estimates(_db):
        return {
            "seedance-2.0": {"t2v": {"720p:5": 60_000}},
            "omni-flash": {"t2v": {"720p:6": 6_000_000}},
        }

    async def provider_state(_db):
        return [provider], []

    monkeypatch.setattr(videos, "_video_enabled", enabled)
    monkeypatch.setattr(videos, "_billing_enabled", enabled)
    monkeypatch.setattr(videos, "_video_hold_estimates", estimates)
    monkeypatch.setattr(videos, "_video_provider_state", provider_state)

    with pytest.raises(HTTPException) as excinfo:
        await videos._require_video_create_ready(object(), body)  # noqa: SLF001

    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "invalid_duration"
    assert excinfo.value.detail["error"]["details"]["available_durations_s"] == [
        6,
        7,
        8,
        9,
        10,
    ]


@pytest.mark.asyncio
async def test_input_image_snapshot_prefers_retry_snapshot_when_available() -> None:
    class Db:
        async def execute(self, _statement):
            raise AssertionError("db lookup should not happen when snapshot exists")

    snapshot = (
        "u/user-1/v/video-1/first-frame.png",
        "sha256",
        "https://example.com/i.png",
    )

    assert (
        await videos._input_image_snapshot(  # noqa: SLF001
            Db(),  # type: ignore[arg-type]
            user_id="user-1",
            image_id="image-1",
            fallback_snapshot=snapshot,
        )
        == snapshot
    )


def test_reference_image_public_url_sets_video_reference_token() -> None:
    image = SimpleNamespace(id="image-1", metadata_jsonb={})

    url = videos._reference_image_public_url(  # noqa: SLF001
        image,  # type: ignore[arg-type]
        "https://lumen.example",
    )

    assert url.startswith("https://lumen.example/api/images/reference/image-1/binary?")
    assert "token=" in url
    assert isinstance(image.metadata_jsonb["video_reference_access_token"], str)


def test_video_reference_jpeg_downsizes_and_flattens_alpha(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    PILImage.new("RGBA", (3000, 1000), (255, 0, 0, 128)).save(source)

    rendered = make_video_reference_jpeg(source)

    assert rendered.width == 2048
    assert rendered.height == 683
    with PILImage.open(io.BytesIO(rendered.data)) as out:
        assert out.format == "JPEG"
        assert out.mode == "RGB"
        assert out.size == (2048, 683)


def test_video_reference_jpeg_retries_without_optimize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.png"
    PILImage.new("RGB", (64, 32), (10, 20, 30)).save(source)
    original_save = PILImage.Image.save

    def flaky_save(self, fp, format=None, **params):  # noqa: ANN001, ANN002
        if params.get("optimize") is True:
            raise OSError("encoder optimize failed")
        return original_save(self, fp, format=format, **params)

    monkeypatch.setattr(PILImage.Image, "save", flaky_save)

    rendered = make_video_reference_jpeg(source)

    with PILImage.open(io.BytesIO(rendered.data)) as out:
        assert out.format == "JPEG"
        assert out.size == (64, 32)


def _video_provider(kind: str) -> VideoProviderDefinition:
    return VideoProviderDefinition(
        name=f"{kind}-provider",
        kind=kind,
        base_url="https://provider.example",
        api_key="key",
        models={"seedance-2.0-fast:reference": "upstream-model"},
    )


def test_volcano_third_party_prefers_reference_public_urls() -> None:
    third_party = _video_provider("volcano_third_party")
    newapi = _video_provider("volcano_newapi")
    official = _video_provider("volcano")
    dashscope = _video_provider("dashscope")
    omni_flash = _video_provider("omni_flash")

    assert videos._provider_prefers_public_media_url(third_party) is True  # noqa: SLF001
    assert videos._provider_requires_public_media(third_party) is False  # noqa: SLF001
    assert videos._provider_prefers_public_media_url(newapi) is True  # noqa: SLF001
    assert videos._provider_requires_public_media(newapi) is True  # noqa: SLF001
    assert videos._provider_prefers_public_media_url(official) is False  # noqa: SLF001
    assert videos._provider_prefers_public_media_url(dashscope) is True  # noqa: SLF001
    assert videos._provider_requires_public_media(dashscope) is True  # noqa: SLF001
    assert videos._provider_prefers_public_media_url(omni_flash) is True  # noqa: SLF001
    assert videos._provider_requires_public_media(omni_flash) is False  # noqa: SLF001


def test_volcano_newapi_reference_media_limits_match_newapi_contract() -> None:
    videos._validate_provider_reference_media(  # noqa: SLF001
        "volcano_newapi",
        [
            *({"kind": "image"} for _idx in range(4)),
            *({"kind": "video"} for _idx in range(3)),
            {"kind": "audio"},
        ],
    )

    for snapshots, code in (
        ([{"kind": "image"} for _idx in range(5)], "too_many_reference_images"),
        ([{"kind": "video"} for _idx in range(4)], "too_many_reference_videos"),
        ([{"kind": "audio"} for _idx in range(2)], "too_many_reference_audios"),
    ):
        with pytest.raises(HTTPException) as excinfo:
            videos._validate_provider_reference_media(  # noqa: SLF001
                "volcano_newapi",
                snapshots,
            )
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["error"]["code"] == code


def test_volcano_reference_audio_requires_visual_and_allows_three() -> None:
    videos._validate_provider_reference_media(  # noqa: SLF001
        "volcano",
        [
            {"kind": "image"},
            *({"kind": "audio"} for _idx in range(3)),
        ],
    )

    with pytest.raises(HTTPException) as excinfo:
        videos._validate_provider_reference_media(  # noqa: SLF001
            "volcano",
            [{"kind": "audio"}],
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "reference_audio_requires_visual"

    with pytest.raises(HTTPException) as excinfo:
        videos._validate_provider_reference_media(  # noqa: SLF001
            "volcano",
            [
                {"kind": "video"},
                *({"kind": "audio"} for _idx in range(4)),
            ],
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.detail["error"]["code"] == "too_many_reference_audios"


@pytest.mark.asyncio
async def test_reference_public_base_url_falls_back_for_preferred_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_public_base_url(*_args, **_kwargs):
        raise RuntimeError("missing public base")

    monkeypatch.setattr(videos, "resolve_public_base_url", fail_public_base_url)
    body = VideoCreateIn(
        action="reference",
        model="seedance-2.0-fast",
        prompt="make a video",
        reference_media=[VideoReferenceMediaIn(kind="image", image_id="image-1")],
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idempotency-1",
    )

    public_base = await videos._reference_public_base_url(  # noqa: SLF001
        _request(),
        SimpleNamespace(),  # type: ignore[arg-type]
        body,
        None,
        prefers_public_media_url=True,
    )

    assert public_base is None


@pytest.mark.asyncio
async def test_reference_public_base_url_still_fails_for_required_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_public_base_url(*_args, **_kwargs):
        raise RuntimeError("missing public base")

    monkeypatch.setattr(videos, "resolve_public_base_url", fail_public_base_url)
    body = VideoCreateIn(
        action="reference",
        model="happyhorse-1.0",
        prompt="make a video",
        reference_media=[VideoReferenceMediaIn(kind="image", image_id="image-1")],
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idempotency-1",
    )

    with pytest.raises(HTTPException) as excinfo:
        await videos._reference_public_base_url(  # noqa: SLF001
            _request(),
            SimpleNamespace(),  # type: ignore[arg-type]
            body,
            None,
            requires_public_media=True,
            prefers_public_media_url=True,
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"]["code"] == "video_reference_public_url_missing"


@pytest.mark.asyncio
async def test_reference_public_base_url_still_fails_for_video_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_public_base_url(*_args, **_kwargs):
        raise RuntimeError("missing public base")

    monkeypatch.setattr(videos, "resolve_public_base_url", fail_public_base_url)
    body = VideoCreateIn(
        action="reference",
        model="seedance-2.0-fast",
        prompt="make a video",
        reference_media=[VideoReferenceMediaIn(kind="video", video_id="video-1")],
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idempotency-1",
    )

    with pytest.raises(HTTPException) as excinfo:
        await videos._reference_public_base_url(  # noqa: SLF001
            _request(),
            SimpleNamespace(),  # type: ignore[arg-type]
            body,
            None,
            prefers_public_media_url=True,
        )

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"]["code"] == "video_reference_public_url_missing"


def test_create_video_generation_maps_billing_error() -> None:
    source = inspect.getsource(videos._create_video_generation_record)  # noqa: SLF001

    assert "except billing_core.BillingError as exc" in source


def test_create_video_generation_commits_video_hold_and_outbox_together() -> None:
    source = inspect.getsource(videos._create_video_generation_record)  # noqa: SLF001
    hold_idx = source.index("await billing_core.hold")
    try_idx = source.rfind("try:", 0, hold_idx)
    except_idx = source.index("except billing_core.BillingError", hold_idx)
    guarded = source[try_idx:except_idx]

    assert "db.add(vg)" in guarded
    assert "await billing_core.hold" in guarded
    assert "db.add(outbox)" in guarded
    assert "await db.flush()" in guarded
    assert "await db.commit()" in guarded
    assert guarded.index("db.add(vg)") < guarded.index("await billing_core.hold")
    assert guarded.index("await billing_core.hold") < guarded.index("db.add(outbox)")
    assert guarded.index("db.add(outbox)") < guarded.index("await db.commit()")


def test_create_video_generation_reuses_request_fingerprint() -> None:
    source = inspect.getsource(videos._create_video_generation_record)  # noqa: SLF001

    assert "request_fingerprint = _request_fingerprint(body)" in source
    assert '"request_fingerprint": request_fingerprint' in source
    assert '"reference_media_count": len(reference_snapshots)' in source
    assert "request_fingerprint=request_fingerprint" in source


def test_idempotent_replay_rejects_mismatched_fingerprint() -> None:
    row = SimpleNamespace(request_fingerprint="old", diagnostics={})

    with pytest.raises(Exception) as excinfo:
        videos._ensure_idempotent_replay_matches(row, "new")  # noqa: SLF001

    assert getattr(excinfo.value, "status_code", None) == 409
    assert excinfo.value.detail["error"]["code"] == "idempotency_request_mismatch"


def test_idempotent_replay_allows_legacy_rows_without_fingerprint() -> None:
    row = SimpleNamespace(request_fingerprint=None, diagnostics={})

    videos._ensure_idempotent_replay_matches(row, "new")  # noqa: SLF001


def test_reference_media_out_hides_internal_reference_token_url() -> None:
    ref = videos._reference_media_out(  # noqa: SLF001
        {
            "kind": "image",
            "image_id": "image-1",
            "url": "https://lumen.example/api/images/reference/image-1/binary?token=secret",
        }
    )

    assert ref is not None
    assert ref.image_id == "image-1"
    assert ref.url is None


def test_public_video_diagnostics_redacts_raw_error_messages() -> None:
    public = videos._public_video_diagnostics(  # noqa: SLF001
        {
            "billing_decision": "actual_usage_settle",
            "last_poll_error": {
                "at": "2026-06-10T00:00:00Z",
                "error_code": "provider_error",
                "message": "raw upstream secret",
                "retryable": True,
            },
            "cancel_result": {"raw": "provider details"},
        }
    )

    assert public["billing_decision"] == "actual_usage_settle"
    assert public["last_poll_error"]["error_code"] == "provider_error"
    assert "message" not in public["last_poll_error"]
    assert "cancel_result" not in public


@pytest.mark.asyncio
async def test_reference_media_snapshots_default_labels_are_per_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Db:
        async def execute(self, _statement):
            raise AssertionError("url reference should not query db")

    async def resolve(url: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(url=url)

    monkeypatch.setattr(videos, "resolve_public_http_target", resolve)

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[
            VideoReferenceMediaIn(kind="video", url="https://example.com/ref.mp4"),
            VideoReferenceMediaIn(kind="image", url="https://example.com/ref.png"),
            VideoReferenceMediaIn(kind="audio", url="https://example.com/ref.mp3"),
        ],
    )

    assert [(item["kind"], item["label"]) for item in snapshots] == [
        ("video", "Video 1"),
        ("image", "Image 1"),
        ("audio", "Audio 1"),
    ]
    assert [(item["kind"], item["ref_id"]) for item in snapshots] == [
        ("video", "ref:video:1"),
        ("image", "ref:image:1"),
        ("audio", "ref:audio:1"),
    ]


@pytest.mark.asyncio
async def test_reference_url_rejects_private_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("base_url resolves to a private address")

    monkeypatch.setattr(videos, "resolve_public_http_target", reject)

    with pytest.raises(HTTPException) as exc_info:
        await videos._resolve_reference_url(  # noqa: SLF001
            "https://127.0.0.1.nip.io/private.mp4"
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error"]["code"] == "invalid_reference_url"


@pytest.mark.asyncio
async def test_legacy_snapshot_invalid_ref_id_falls_back_to_stable_default() -> None:
    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[],
        fallback_snapshots=[
            {
                "kind": "audio",
                "url": "asset://asset-1",
                "ref_id": "legacy-invalid-ref",
            }
        ],
    )

    assert snapshots[0]["ref_id"] == "ref:audio:1"


def test_reference_upload_ext_only_accepts_official_seedance_video_formats() -> None:
    assert videos._reference_upload_ext(  # noqa: SLF001
        SimpleNamespace(content_type="video/mp4", filename="ref.mp4")
    ) == ("video/mp4", "mp4")
    assert videos._reference_upload_ext(  # noqa: SLF001
        SimpleNamespace(content_type="", filename="ref.mov")
    ) == ("video/quicktime", "mov")

    for file in (
        SimpleNamespace(content_type="video/webm", filename="ref.webm"),
        SimpleNamespace(content_type="video/x-m4v", filename="ref.m4v"),
    ):
        with pytest.raises(Exception) as excinfo:
            videos._reference_upload_ext(file)  # noqa: SLF001
        assert getattr(excinfo.value, "status_code", None) == 415


def test_reference_video_magic_requires_iso_bmff_file() -> None:
    assert videos._looks_like_reference_video(  # noqa: SLF001
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    )
    assert not videos._looks_like_reference_video(b"not-a-video")  # noqa: SLF001


def test_reference_video_fit_dimensions_stays_under_seedance_r2v_limit() -> None:
    width, height = _fit_even_dimensions(3840, 2160)

    assert width == 1920
    assert height == 1080
    assert width * height <= VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT

    portrait_width, portrait_height = _fit_even_dimensions(2160, 3840)
    assert portrait_width == 1080
    assert portrait_height == 1920
    assert portrait_width * portrait_height <= VIDEO_REFERENCE_VIDEO_PIXEL_LIMIT


def test_validate_reference_url_accepts_public_url_or_asset_uri() -> None:
    assert (
        videos._validate_reference_url("https://example.com/ref.mp4")  # noqa: SLF001
        == "https://example.com/ref.mp4"
    )
    assert (
        videos._validate_reference_url("asset://asset-1")  # noqa: SLF001
        == "asset://asset-1"
    )
    assert (
        videos._validate_reference_url("Asset://ASSET-20260609161523-STLQD")  # noqa: SLF001
        == "asset://asset-20260609161523-stlqd"
    )
    assert (
        videos._validate_reference_url(" `Asset : //ASSET-20260609161523-STLQD` ")  # noqa: SLF001
        == "asset://asset-20260609161523-stlqd"
    )
    assert (
        videos._validate_reference_url("ASSET-20260609161523-STLQD")  # noqa: SLF001
        == "asset://asset-20260609161523-stlqd"
    )

    with pytest.raises(Exception) as excinfo:
        videos._validate_reference_url("ftp://example.com/ref.mp4")  # noqa: SLF001
    assert getattr(excinfo.value, "status_code", None) == 422
    for url in (
        "http://example.com/ref.mp4",
        "https://127.0.0.1/ref.mp4",
        "https://[::ffff:127.0.0.1]/ref.mp4",
        "https://0177.0.0.1/ref.mp4",
    ):
        with pytest.raises(Exception) as excinfo:
            videos._validate_reference_url(url)  # noqa: SLF001
        assert getattr(excinfo.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_reference_media_snapshots_adds_public_url_for_uploaded_video(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = SimpleNamespace(
        id="video-1",
        storage_key="u/user-1/vref/video-1/original.mp4",
        sha256="sha",
        mime="video/mp4",
        metadata_jsonb={},
        deleted_at=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return video

    class Db:
        async def execute(self, _statement):
            return Result()

    async def fake_ensure(_db, video_arg, *, storage_root: str):
        return {
            "kind": VIDEO_REFERENCE_VIDEO_KIND,
            "storage_key": "u/user-1/vref/video-1/video-1.ref.mp4",
            "mime": "video/mp4",
            "width": 1920,
            "height": 1080,
            "size_bytes": 123,
            "sha256": "variant-sha",
        }

    monkeypatch.setattr(videos, "ensure_video_reference_video_variant", fake_ensure)

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[VideoReferenceMediaIn(kind="video", video_id="video-1")],
        reference_public_base_url="https://lumen.example",
    )

    assert snapshots[0]["url"].startswith(
        "https://lumen.example/api/videos/reference/video-1/binary?token="
    )
    assert f"variant={VIDEO_REFERENCE_VIDEO_KIND}" in snapshots[0]["url"]
    assert snapshots[0]["upstream_reference_storage_key"].endswith("video-1.ref.mp4")
    assert snapshots[0]["upstream_reference_width"] == 1920
    assert snapshots[0]["upstream_reference_height"] == 1080
    assert "reference_access_token_expires_at" in video.metadata_jsonb


@pytest.mark.asyncio
async def test_reference_media_snapshots_refreshes_legacy_video_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = SimpleNamespace(
        id="video-1",
        storage_key="u/user-1/vref/video-1/original.mov",
        sha256="sha",
        mime="video/quicktime",
        metadata_jsonb={},
        deleted_at=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return video

    class Db:
        async def execute(self, _statement):
            return Result()

    async def fake_ensure(_db, video_arg, *, storage_root: str):
        return {
            "kind": VIDEO_REFERENCE_VIDEO_KIND,
            "storage_key": "u/user-1/vref/video-1/video-1.safe.mp4",
            "mime": "video/mp4",
            "width": 1080,
            "height": 1920,
            "size_bytes": 456,
            "sha256": "safe-sha",
        }

    monkeypatch.setattr(videos, "ensure_video_reference_video_variant", fake_ensure)

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[],
        fallback_snapshots=[
            {
                "kind": "video",
                "video_id": "video-1",
                "url": "https://old.example/api/videos/reference/video-1/binary?token=old",
            }
        ],
        reference_public_base_url="https://lumen.example",
    )

    assert snapshots[0]["url"].startswith(
        "https://lumen.example/api/videos/reference/video-1/binary?token="
    )
    assert "old.example" not in snapshots[0]["url"]
    assert f"variant={VIDEO_REFERENCE_VIDEO_KIND}" in snapshots[0]["url"]
    assert snapshots[0]["upstream_reference_storage_key"].endswith("video-1.safe.mp4")


@pytest.mark.asyncio
async def test_reference_media_snapshots_adds_public_url_for_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = SimpleNamespace(
        id="image-1",
        storage_key="u/user-1/uploads/image-1.jpg",
        sha256="sha",
        mime="image/jpeg",
        metadata_jsonb={},
        deleted_at=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return image

    class Db:
        async def execute(self, _statement):
            return Result()

    ensured: list[str] = []

    async def fake_ensure(_db, image_arg, *, storage_root: str):
        ensured.append(image_arg.id)
        return SimpleNamespace(
            image_id=image_arg.id,
            kind=VIDEO_REFERENCE_IMAGE_KIND,
            storage_key="u/user-1/uploads/image-1.video_ref_2048_jpg.jpg",
            width=2048,
            height=1024,
        )

    monkeypatch.setattr(videos, "ensure_video_reference_image_variant", fake_ensure)

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[
            VideoReferenceMediaIn(
                kind="image",
                image_id="image-1",
                label="商品图",
                ref_id="ref:image:2",
            )
        ],
        reference_public_base_url="https://lumen.example",
    )

    assert snapshots[0]["url"].startswith(
        "https://lumen.example/api/images/reference/image-1/binary?token="
    )
    assert f"variant={VIDEO_REFERENCE_IMAGE_KIND}" in snapshots[0]["url"]
    assert snapshots[0]["label"] == "商品图"
    assert snapshots[0]["ref_id"] == "ref:image:2"
    assert "video_reference_access_token" in image.metadata_jsonb
    assert "video_reference_access_token_expires_at" in image.metadata_jsonb
    assert ensured == ["image-1"]


@pytest.mark.asyncio
async def test_reference_media_snapshots_refreshes_legacy_image_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = SimpleNamespace(
        id="image-1",
        storage_key="u/user-1/uploads/image-1.png",
        sha256="sha",
        mime="image/png",
        metadata_jsonb={},
        deleted_at=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return image

    class Db:
        async def execute(self, _statement):
            return Result()

    async def fake_ensure(_db, image_arg, *, storage_root: str):
        return SimpleNamespace(
            image_id=image_arg.id,
            kind=VIDEO_REFERENCE_IMAGE_KIND,
            storage_key="u/user-1/uploads/image-1.video_ref_2048_jpg.jpg",
            width=2048,
            height=2048,
        )

    monkeypatch.setattr(videos, "ensure_video_reference_image_variant", fake_ensure)

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[],
        fallback_snapshots=[
            {
                "kind": "image",
                "image_id": "image-1",
                "url": "https://old.example/api/images/reference/image-1/binary?token=old",
                "label": "",
                "ref_id": "",
            }
        ],
        reference_public_base_url="https://lumen.example",
    )

    assert snapshots[0]["url"].startswith(
        "https://lumen.example/api/images/reference/image-1/binary?token="
    )
    assert f"variant={VIDEO_REFERENCE_IMAGE_KIND}" in snapshots[0]["url"]
    assert snapshots[0]["ref_id"] == "ref:image:1"
    assert snapshots[0]["label"] == "Image 1"


@pytest.mark.asyncio
async def test_reference_media_snapshots_falls_back_to_inline_when_variant_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = SimpleNamespace(
        id="image-1",
        storage_key="u/user-1/uploads/image-1.png",
        sha256="sha",
        mime="image/png",
        metadata_jsonb={},
        deleted_at=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return image

    class Db:
        async def execute(self, _statement):
            return Result()

    async def fail_ensure(*_args, **_kwargs):
        raise VideoReferenceImageError("invalid_image", "unreadable image", 400)

    monkeypatch.setattr(videos, "ensure_video_reference_image_variant", fail_ensure)

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[VideoReferenceMediaIn(kind="image", image_id="image-1")],
        reference_public_base_url="https://lumen.example",
    )

    assert snapshots[0]["url"] is None
    assert snapshots[0]["upstream_reference_variant"] is None
    assert snapshots[0]["upstream_reference_variant_error"]["code"] == "invalid_image"


@pytest.mark.asyncio
async def test_reference_media_snapshots_raises_when_variant_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = SimpleNamespace(
        id="image-1",
        storage_key="u/user-1/uploads/image-1.png",
        sha256="sha",
        mime="image/png",
        metadata_jsonb={},
        deleted_at=None,
    )

    class Result:
        def scalar_one_or_none(self):
            return image

    class Db:
        async def execute(self, _statement):
            return Result()

    async def fail_ensure(*_args, **_kwargs):
        raise VideoReferenceImageError("invalid_image", "unreadable image", 400)

    monkeypatch.setattr(videos, "ensure_video_reference_image_variant", fail_ensure)

    with pytest.raises(HTTPException) as excinfo:
        await videos._reference_media_snapshots(  # noqa: SLF001
            Db(),  # type: ignore[arg-type]
            user_id="user-1",
            items=[VideoReferenceMediaIn(kind="image", image_id="image-1")],
            reference_public_base_url="https://lumen.example",
            required_public_media=True,
        )

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_image"


def test_cancel_video_generation_only_auto_cancels_queued_rows() -> None:
    source = inspect.getsource(videos.cancel_video_generation)
    compact_source = " ".join(source.split())

    assert (
        "row.status == VideoGenerationStatus.QUEUED.value and not row.provider_task_id"
        in compact_source
    )
    assert "tx = await billing_core.release" in source
    assert "if tx is None:" in source
    assert "await db.rollback()" in source
    assert "if balance_changed:" in source
    assert "publish_sse_event" not in source


@pytest.mark.asyncio
async def test_cancel_video_generation_rolls_back_when_hold_release_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def __init__(self, value: Any) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Any:
            return self.value

    class Db:
        def __init__(self, row: Any) -> None:
            self.row = row
            self.committed = False
            self.rolled_back = False
            self.refreshed = False

        async def execute(self, _statement: Any) -> Result:
            return Result(self.row)

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            self.rolled_back = True

        async def refresh(self, _row: Any) -> None:
            self.refreshed = True

    async def missing_release(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fail_publish(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("cancel should not publish after release failure")

    invalidated: list[str] = []

    async def invalidate(user_id: str) -> None:
        invalidated.append(user_id)

    row = SimpleNamespace(
        id="video-gen-1",
        user_id="user-1",
        status=videos.VideoGenerationStatus.QUEUED.value,
        provider_task_id=None,
        cancel_requested_at=None,
        progress_stage=videos.VideoGenerationStage.QUEUED.value,
        progress_pct=0,
        error_code=None,
        error_message=None,
        finished_at=None,
        model="seedance",
        action="text",
        provider_name="provider-a",
    )
    db = Db(row)
    monkeypatch.setattr(videos.billing_core, "release", missing_release)
    monkeypatch.setattr(videos, "publish_sse_event", fail_publish)
    monkeypatch.setattr(videos, "invalidate_balance_cache", invalidate)

    with pytest.raises(HTTPException) as excinfo:
        await videos.cancel_video_generation(
            "video-gen-1",
            SimpleNamespace(id="user-1"),  # type: ignore[arg-type]
            db,  # type: ignore[arg-type]
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["error"]["code"] == "video_hold_release_missing"
    assert db.rolled_back is True
    assert db.committed is False
    assert db.refreshed is False
    assert invalidated == []


def test_retry_video_generation_reuses_only_valid_reference_snapshots() -> None:
    source = inspect.getsource(videos.retry_video_generation)

    assert "account_mode_forbidden" in source
    assert "video_retry_not_terminal" in source
    assert ".with_for_update()" in source
    assert "row.updated_at.isoformat()" in source
    assert "valid_reference_snapshots.append(item)" in source
    assert "reference_media_snapshot=valid_reference_snapshots" in source


def test_list_video_generations_batches_video_lookup() -> None:
    source = inspect.getsource(videos.list_video_generations)

    assert "Video.owner_generation_id.in_(generation_ids)" in source
    assert "videos_by_generation_id" in source


def test_list_video_generations_next_cursor_uses_last_returned_row() -> None:
    source = inspect.getsource(videos.list_video_generations)

    assert "_encode_cursor(page[-1])" in source
    assert "_encode_cursor(rows[limit])" not in source


def test_events_task_ids_include_video_generation_id() -> None:
    assert events._task_ids_from_payload(  # noqa: SLF001
        {"video_generation_id": "video-1", "generation_id": "gen-1"}
    ) == {"video-1", "gen-1"}


@pytest.mark.asyncio
async def test_events_validate_channels_accepts_owned_video_generation() -> None:
    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    class Db:
        async def execute(self, statement):
            sql = str(statement)
            if "FROM video_generations" in sql:
                return Result(["video-1"])
            return Result([])

    clean = await events._validate_channels(  # noqa: SLF001
        ["task:video-1"],
        "user-1",
        Db(),  # type: ignore[arg-type]
    )

    assert clean == ["task:video-1"]


@pytest.mark.asyncio
async def test_events_validate_channels_rejects_unowned_video_generation() -> None:
    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Db:
        async def execute(self, _statement):
            return Result()

    with pytest.raises(Exception) as excinfo:
        await events._validate_channels(  # noqa: SLF001
            ["task:video-1"],
            "user-1",
            Db(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert excinfo.value.detail["error"]["code"] == "forbidden_channel"
