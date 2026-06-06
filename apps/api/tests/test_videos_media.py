from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from lumen_core.schemas import VideoReferenceMediaIn

from app.routes import events, videos


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


def test_video_duration_options_include_smart_duration() -> None:
    assert (
        videos._duration_options(  # noqa: SLF001
            {"seedance-2.0": {"t2v": {"720p:5": 60_000, "720p:15": 180_000}}}
        )[0]
        == -1
    )


def test_reference_video_action_requires_image_and_video_pricing_paths() -> None:
    model = "seedance-2.0"

    assert not videos._has_video_price(  # noqa: SLF001
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


@pytest.mark.asyncio
async def test_input_image_snapshot_prefers_retry_snapshot_when_available() -> None:
    class Db:
        async def execute(self, _statement):
            raise AssertionError("db lookup should not happen when snapshot exists")

    snapshot = ("u/user-1/v/video-1/first-frame.png", "sha256")

    assert (
        await videos._input_image_snapshot(  # noqa: SLF001
            Db(),  # type: ignore[arg-type]
            user_id="user-1",
            image_id="image-1",
            fallback_snapshot=snapshot,
        )
        == snapshot
    )


def test_create_video_generation_maps_billing_error() -> None:
    source = inspect.getsource(videos._create_video_generation_record)  # noqa: SLF001

    assert "except billing_core.BillingError as exc" in source


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


@pytest.mark.asyncio
async def test_reference_media_snapshots_default_labels_are_per_kind() -> None:
    class Db:
        async def execute(self, _statement):
            raise AssertionError("url reference should not query db")

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[
            VideoReferenceMediaIn(kind="video", url="https://example.com/ref.mp4"),
            VideoReferenceMediaIn(kind="image", url="https://example.com/ref.png"),
        ],
    )

    assert [(item["kind"], item["label"]) for item in snapshots] == [
        ("video", "Video 1"),
        ("image", "Image 1"),
    ]


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


def test_validate_reference_url_accepts_public_url_or_asset_uri() -> None:
    assert (
        videos._validate_reference_url("https://example.com/ref.mp4")  # noqa: SLF001
        == "https://example.com/ref.mp4"
    )
    assert (
        videos._validate_reference_url("asset://asset-1")  # noqa: SLF001
        == "asset://asset-1"
    )

    with pytest.raises(Exception) as excinfo:
        videos._validate_reference_url("ftp://example.com/ref.mp4")  # noqa: SLF001
    assert getattr(excinfo.value, "status_code", None) == 422


@pytest.mark.asyncio
async def test_reference_media_snapshots_adds_public_url_for_uploaded_video() -> None:
    class Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(
                id="video-1",
                storage_key="u/user-1/vref/video-1/original.mp4",
                sha256="sha",
                mime="video/mp4",
                metadata_jsonb={},
                deleted_at=None,
            )

    class Db:
        async def execute(self, _statement):
            return Result()

    snapshots = await videos._reference_media_snapshots(  # noqa: SLF001
        Db(),  # type: ignore[arg-type]
        user_id="user-1",
        items=[VideoReferenceMediaIn(kind="video", video_id="video-1")],
        reference_public_base_url="https://lumen.example",
    )

    assert snapshots[0]["url"].startswith(
        "https://lumen.example/api/videos/reference/video-1/binary?token="
    )


def test_cancel_video_generation_only_auto_cancels_queued_rows() -> None:
    source = inspect.getsource(videos.cancel_video_generation)
    compact_source = " ".join(source.split())

    assert (
        "row.status == VideoGenerationStatus.QUEUED.value and not row.provider_task_id"
        in compact_source
    )


def test_retry_video_generation_reuses_only_valid_reference_snapshots() -> None:
    source = inspect.getsource(videos.retry_video_generation)

    assert "valid_reference_snapshots.append(item)" in source
    assert "reference_media_snapshot=valid_reference_snapshots" in source


def test_list_video_generations_batches_video_lookup() -> None:
    source = inspect.getsource(videos.list_video_generations)

    assert "Video.owner_generation_id.in_(generation_ids)" in source
    assert "videos_by_generation_id" in source


def test_events_task_ids_include_video_generation_id() -> None:
    assert events._task_ids_from_payload(  # noqa: SLF001
        {"video_generation_id": "video-1", "generation_id": "gen-1"}
    ) == {"video-1", "gen-1"}


@pytest.mark.asyncio
async def test_events_validate_channels_accepts_owned_video_generation() -> None:
    class Result:
        def __init__(self, row):
            self.row = row

        def first(self):
            return self.row

    class Db:
        async def execute(self, statement):
            sql = str(statement)
            if "FROM video_generations" in sql:
                return Result(("video-1",))
            return Result(None)

    clean = await events._validate_channels(  # noqa: SLF001
        ["task:video-1"],
        "user-1",
        Db(),  # type: ignore[arg-type]
    )

    assert clean == ["task:video-1"]


@pytest.mark.asyncio
async def test_events_validate_channels_rejects_unowned_video_generation() -> None:
    class Result:
        def first(self):
            return None

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
