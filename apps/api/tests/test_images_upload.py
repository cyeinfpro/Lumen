from __future__ import annotations

import errno
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from PIL import Image as PILImage

from app.canvas_services import asset_ref_service
from app.config import settings
from app.routes import images
from app.volcano_asset_media import VOLCANO_ASSET_IMAGE_KIND
from app.video_reference_images import VIDEO_REFERENCE_IMAGE_KIND
from lumen_core.models import AuditLog, Image


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, result):
        self.result = result
        self.added = []
        self.committed = False
        self.flushed = False

    async def execute(self, _stmt):
        return _ScalarResult(self.result)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


class _UploadFile:
    filename = "reference.png"

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._read = False

    async def read(self, _size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._data


def _request(method: str = "DELETE") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _png_bytes(
    mode: str,
    size: tuple[int, int],
    color,
) -> bytes:
    buf = io.BytesIO()
    PILImage.new(mode, size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size: tuple[int, int], color) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


def test_storage_path_rejects_traversal(tmp_path: Path) -> None:
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    try:
        with pytest.raises(Exception) as excinfo:
            images._fs_path("../escape.png")
        assert getattr(excinfo.value, "status_code", None) == 400
        with pytest.raises(Exception) as abs_excinfo:
            images._fs_path(str(tmp_path / "escape.png"))
        assert getattr(abs_excinfo.value, "status_code", None) == 400
    finally:
        settings.storage_root = old


def test_storage_free_space_guard_rejects_low_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    monkeypatch.setenv("LUMEN_MIN_STORAGE_FREE_BYTES", "1000")
    monkeypatch.setattr(
        images.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=1000),
    )
    try:
        images._ensure_storage_free_space(0)
        with pytest.raises(Exception) as excinfo:
            images._ensure_storage_free_space(1)
        assert getattr(excinfo.value, "status_code", None) == 507
    finally:
        settings.storage_root = old


@pytest.mark.asyncio
async def test_sweep_orphan_image_files_dry_run_and_delete(tmp_path: Path) -> None:
    kept = tmp_path / "u" / "user-1" / "uploads" / "img-1.png"
    kept_variant = tmp_path / "u" / "user-1" / "uploads" / "img-1.display2048.webp"
    kept_ref = tmp_path / "u" / "user-1" / "uploads" / "img-1.ref.webp"
    orphan = tmp_path / "u" / "user-1" / "uploads" / "orphan.webp"
    generated_orphan = tmp_path / "u" / "user-1" / "g" / "gen-1" / "orig.png"
    video = tmp_path / "u" / "user-1" / "vref" / "video-1" / "original.mp4"
    workflow = tmp_path / "workflows" / "run-1" / "artifact.json"
    for path in (
        kept,
        kept_variant,
        kept_ref,
        orphan,
        generated_orphan,
        video,
        workflow,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")

    class _RowsResult:
        def __init__(self, rows=None, values=None):
            self.rows = rows or []
            self.values = values or []

        def all(self):
            return self.rows

        def scalars(self):
            class _ScalarList:
                def __init__(self, values):
                    self.values = values

                def all(self):
                    return self.values

            return _ScalarList(self.values)

    class _SweepDb:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _stmt):
            self.calls += 1
            if self.calls % 2 == 1:
                return _RowsResult(
                    rows=[
                        (
                            "u/user-1/uploads/img-1.png",
                            {
                                "normalized_ref": {
                                    "storage_key": "u/user-1/uploads/img-1.ref.webp"
                                }
                            },
                        )
                    ]
                )
            return _RowsResult(values=["u/user-1/uploads/img-1.display2048.webp"])

    db = _SweepDb()
    dry_run = await images.sweep_orphan_image_files(
        db,  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=True,
    )
    assert dry_run["orphans"] == [
        "u/user-1/uploads/orphan.webp",
        "u/user-1/g/gen-1/orig.png",
    ]
    assert orphan.exists()
    assert generated_orphan.exists()
    assert video.exists()
    assert workflow.exists()

    deleted = await images.sweep_orphan_image_files(
        db,  # type: ignore[arg-type]
        storage_root=str(tmp_path),
        dry_run=False,
    )
    assert deleted["deleted"] == 2
    assert not orphan.exists()
    assert not generated_orphan.exists()
    assert kept.exists()
    assert kept_variant.exists()
    assert kept_ref.exists()
    assert video.exists()
    assert workflow.exists()


def test_upload_write_is_atomic_and_rejects_conflict(tmp_path: Path) -> None:
    path = tmp_path / "image.png"

    images._write_new_file_atomic(path, b"first")

    assert path.read_bytes() == b"first"
    with pytest.raises(FileExistsError):
        images._write_new_file_atomic(path, b"second")
    assert path.read_bytes() == b"first"


def test_upload_write_falls_back_when_hardlink_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "image.png"

    def raise_eperm(_src: Path, _dst: Path) -> None:
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(images.os, "link", raise_eperm)

    images._write_new_file_atomic(path, b"first")

    assert path.read_bytes() == b"first"
    with pytest.raises(FileExistsError):
        images._write_new_file_atomic(path, b"second")
    assert path.read_bytes() == b"first"


@pytest.mark.asyncio
async def test_upload_rolls_back_original_and_normalized_ref_on_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingCommitDb:
        def __init__(self) -> None:
            self.added: list[Any] = []
            self.rolled_back = False

        def add(self, value: Any) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            self.added[-1].id = "img-upload"

        async def commit(self) -> None:
            raise RuntimeError("commit failed")

        async def rollback(self) -> None:
            self.rolled_back = True

    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(images, "_check_upload_rate_limit", no_rate_limit)
    monkeypatch.setattr(images, "_ensure_storage_free_space", lambda _size: None)
    db = _FailingCommitDb()

    with pytest.raises(RuntimeError, match="commit failed"):
        await images.upload_image(
            SimpleNamespace(id="user-1"),
            db,  # type: ignore[arg-type]
            file=_UploadFile(_png_bytes("RGB", (16, 16), (10, 20, 30))),  # type: ignore[arg-type]
            purpose=None,
        )

    assert db.rolled_back is True
    assert not (tmp_path / "u" / "user-1" / "uploads" / "img-upload.png").exists()
    assert not (tmp_path / "u" / "user-1" / "uploads" / "img-upload.ref.webp").exists()


def test_binary_open_rejects_final_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform does not support O_NOFOLLOW")
    target = tmp_path / "target.png"
    target.write_bytes(b"target")
    link = tmp_path / "link.png"
    link.symlink_to(target)

    with pytest.raises(Exception) as excinfo:
        images._open_regular_file_no_symlink(link)

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_path"


def test_storage_path_keeps_final_symlink_visible_to_binary_open(
    tmp_path: Path,
) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform does not support O_NOFOLLOW")
    old = settings.storage_root
    root = tmp_path / "storage"
    target = root / "u" / "victim" / "uploads" / "secret.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"secret")
    link = root / "u" / "attacker" / "uploads" / "alias.png"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)

    settings.storage_root = str(root)
    try:
        path = images._fs_path("u/attacker/uploads/alias.png")
        assert path == link
        with pytest.raises(Exception) as excinfo:
            images._open_regular_file_no_symlink(path)
    finally:
        settings.storage_root = old

    assert getattr(excinfo.value, "status_code", None) == 400
    assert excinfo.value.detail["error"]["code"] == "invalid_path"


def test_upload_limiter_is_always_on() -> None:
    assert images.UPLOADS_LIMITER.always_on is True


def test_upload_limiter_allows_composer_batch_burst() -> None:
    assert images.UPLOADS_LIMITER.initial_tokens >= 4


def test_upload_mime_constants_only_allow_images() -> None:
    assert "text/plain" not in images.ALLOWED_MIME
    assert images.EXT_BY_MIME == {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }
    assert images.NORMALIZABLE_UPLOAD_MIME == {"image/mpo", "image/x-mpo"}


def test_upload_limits_are_bounded() -> None:
    assert images.MAX_BYTES == 50 * 1024 * 1024
    assert images.MAX_LONG_SIDE == 4096
    assert images.PILImage.MAX_IMAGE_PIXELS == images.MAX_IMAGE_PIXELS


def test_volcano_asset_upload_still_enforces_absolute_long_side_limit() -> None:
    with pytest.raises(Exception) as excinfo:
        images._enforce_pixel_limit(
            (images.VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE + 1, 1),
            max_long_side=images.VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE,
        )

    assert getattr(excinfo.value, "status_code", None) == 413


@pytest.mark.asyncio
async def test_upload_image_passes_volcano_asset_dimension_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopAfterPrepare(RuntimeError):
        pass

    captured: dict[str, Any] = {}

    async def no_rate_limit(*_args: Any, **_kwargs: Any) -> None:
        return None

    def fake_prepare(
        _staged: Any,
        _filename: str | None,
        **kwargs: Any,
    ) -> Any:
        captured.update(kwargs)
        raise StopAfterPrepare

    monkeypatch.setattr(images, "_check_upload_rate_limit", no_rate_limit)
    monkeypatch.setattr(images, "_ensure_storage_free_space", lambda _size: None)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(
        images.upload_pipeline,
        "prepare_image_upload",
        fake_prepare,
    )

    with pytest.raises(StopAfterPrepare):
        await images.upload_image(
            SimpleNamespace(id="user-1"),
            object(),  # type: ignore[arg-type]
            file=_UploadFile(b"image"),  # type: ignore[arg-type]
            purpose="volcano_asset",
        )

    assert (
        captured["max_long_side"]
        == images.VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE
    )


def test_mask_filename_requests_strict_preflight() -> None:
    assert images._upload_requests_mask_preflight(None, "mask.png")
    assert images._upload_requests_mask_preflight(None, "mask_123.png")
    assert images._upload_requests_mask_preflight("inpaint_mask", "photo.png")
    assert not images._upload_requests_mask_preflight(None, "reference.png")


def test_explicit_pixel_limit_maps_to_413() -> None:
    with pytest.raises(Exception) as excinfo:
        images._enforce_pixel_limit((images.MAX_IMAGE_PIXELS + 1, 1))

    assert getattr(excinfo.value, "status_code", None) == 413
    assert excinfo.value.detail["error"]["code"] == "too_many_pixels"


def test_display_variant_key_is_next_to_original() -> None:
    img = Image(
        id="img_1",
        user_id="user_1",
        source="generated",
        storage_key="u/user_1/g/gen_1/orig.png",
        mime="image/png",
        width=3840,
        height=2160,
        size_bytes=123,
        sha256="abc",
        visibility="private",
    )

    assert images._variant_key_for_image(img, images.DISPLAY_VARIANT) == (
        "u/user_1/g/gen_1/img_1.display2048.webp"
    )


def test_display_variant_key_uses_image_id_to_avoid_stem_collisions() -> None:
    first = Image(
        id="img_1",
        user_id="user_1",
        source="generated",
        storage_key="u/user_1/g/gen_1/orig.png",
        mime="image/png",
        width=1,
        height=1,
        size_bytes=1,
        sha256="abc",
        visibility="private",
    )
    second = Image(
        id="img_2",
        user_id="user_1",
        source="generated",
        storage_key="u/user_1/g/gen_1/orig.jpg",
        mime="image/jpeg",
        width=1,
        height=1,
        size_bytes=1,
        sha256="def",
        visibility="private",
    )

    assert images._variant_key_for_image(  # noqa: SLF001
        first, images.DISPLAY_VARIANT
    ) != images._variant_key_for_image(second, images.DISPLAY_VARIANT)  # noqa: SLF001


def test_make_display_variant_downsizes_and_encodes_webp(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    PILImage.new("RGB", (3000, 1500), color=(20, 40, 60)).save(src, format="PNG")

    data, size = images._make_display_variant(src)

    assert size == (2048, 1024)
    assert data.startswith(b"RIFF")
    assert b"WEBP" in data[:16]


@pytest.mark.asyncio
async def test_reference_image_binary_serves_video_reference_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    ref_key = "u/user-1/uploads/image-1.video_ref_2048_jpg.jpg"
    ref_path = tmp_path / ref_key
    ref_path.parent.mkdir(parents=True)
    ref_path.write_bytes(b"jpeg-bytes")

    async def fake_ensure(_db, image_arg, *, storage_root: str):
        assert storage_root == str(tmp_path)
        return SimpleNamespace(
            image_id=image_arg.id,
            kind=VIDEO_REFERENCE_IMAGE_KIND,
            storage_key=ref_key,
        )

    monkeypatch.setattr(images, "ensure_video_reference_image_variant", fake_ensure)
    img = SimpleNamespace(
        id="image-1",
        metadata_jsonb={"video_reference_access_token": "x" * 16},
        storage_key="u/user-1/uploads/image-1.png",
        mime="image/png",
        sha256="orig-sha",
        deleted_at=None,
        updated_at=datetime.now(timezone.utc),
    )
    try:
        response = await images.reference_image_binary(
            "image-1",
            _request("GET"),
            _Db(img),  # type: ignore[arg-type]
            token="x" * 16,
            variant=VIDEO_REFERENCE_IMAGE_KIND,
        )
    finally:
        settings.storage_root = old

    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["content-length"] == str(len(b"jpeg-bytes"))
    assert response.headers["etag"] == f'"image-1-{VIDEO_REFERENCE_IMAGE_KIND}"'


@pytest.mark.asyncio
async def test_reference_image_binary_serves_volcano_asset_variant(
    tmp_path: Path,
) -> None:
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    ref_key = "u/user-1/uploads/image-1.volcano_asset_img_v1.jpg"
    ref_path = tmp_path / ref_key
    ref_path.parent.mkdir(parents=True)
    ref_path.write_bytes(b"volcano-jpeg")

    img = SimpleNamespace(
        id="image-1",
        metadata_jsonb={"video_reference_access_token": "x" * 16},
        storage_key="u/user-1/uploads/image-1.png",
        mime="image/png",
        sha256="orig-sha",
        deleted_at=None,
        updated_at=datetime.now(timezone.utc),
    )
    variant = SimpleNamespace(
        image_id=img.id,
        kind=VOLCANO_ASSET_IMAGE_KIND,
        storage_key=ref_key,
    )

    class SequenceDb:
        def __init__(self) -> None:
            self.results = iter((img, variant))

        async def execute(self, _stmt):
            return _ScalarResult(next(self.results))

    try:
        response = await images.reference_image_binary(
            "image-1",
            _request("GET"),
            SequenceDb(),  # type: ignore[arg-type]
            token="x" * 16,
            variant=VOLCANO_ASSET_IMAGE_KIND,
        )
        named = await images.reference_image_binary_named(
            "image-1",
            "lumen-asset-image-1.jpg",
            _request("GET"),
            SequenceDb(),  # type: ignore[arg-type]
            token="x" * 16,
            variant=VOLCANO_ASSET_IMAGE_KIND,
        )
    finally:
        settings.storage_root = old

    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["content-length"] == str(len(b"volcano-jpeg"))
    assert response.headers["etag"] == f'"image-1-{VOLCANO_ASSET_IMAGE_KIND}"'
    assert response.headers["content-disposition"] == (
        'inline; filename="lumen-asset-image-1.jpg"'
    )
    assert named.status_code == 200
    assert named.headers["content-type"].startswith("image/jpeg")


@pytest.mark.asyncio
async def test_delete_image_writes_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))
        await db.flush()

    async def no_canvas_reference(_db, **_kwargs):
        return None

    monkeypatch.setattr(images, "write_audit", fake_write_audit)
    monkeypatch.setattr(
        asset_ref_service,
        "ensure_asset_not_canvas_referenced",
        no_canvas_reference,
    )
    img = SimpleNamespace(
        id="img-1",
        source="uploaded",
        owner_generation_id=None,
        deleted_at=None,
    )
    db = _Db(img)

    result = await images.delete_image(
        "img-1",
        _request(),
        SimpleNamespace(id="user-1", email="user@example.com"),
        db,  # type: ignore[arg-type]
    )

    audits = [row for row in db.added if isinstance(row, AuditLog)]
    assert result == {"ok": True}
    assert img.deleted_at is not None
    assert db.committed is True
    assert db.flushed is True
    assert len(audits) == 1
    assert audits[0].event_type == "image.delete"
    assert audits[0].user_id == "user-1"
    assert audits[0].details["image_id"] == "img-1"


@pytest.mark.asyncio
async def test_get_image_by_key_sets_cache_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    path = tmp_path / "u" / "user-1" / "img.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"image-bytes")

    async def no_rate_limit(_request: Request) -> None:
        return None

    monkeypatch.setattr(images, "_check_public_image_lookup_rate_limit", no_rate_limit)
    try:
        img = SimpleNamespace(
            storage_key="u/user-1/img.png",
            user_id="user-1",
            deleted_at=None,
            mime="image/png",
            sha256="abc123",
        )
        response = await images.get_image_by_key(
            "u/user-1/img.png",
            _request("GET"),
            SimpleNamespace(id="user-1"),
            _Db(img),  # type: ignore[arg-type]
        )
    finally:
        settings.storage_root = old

    assert response.headers["content-length"] == str(len(b"image-bytes"))
    assert response.headers["etag"] == '"abc123"'
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"


def _request_with_headers(headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
        }
    )


def test_etag_match_helper_handles_star_list_and_weak_prefix() -> None:
    """RFC 7232 §3.2 corner cases — wildcards, comma lists, and W/ prefix all
    have to be parsed correctly or the 304 short-circuit either misses
    legitimate cache hits (wasting CIFS reads) or matches incorrectly
    (returns 304 with stale body).
    """
    assert images._etag_matches_if_none_match('"abc"', "*")
    assert images._etag_matches_if_none_match('"abc"', '"abc"')
    assert images._etag_matches_if_none_match('"abc"', '"xyz", "abc"')
    assert images._etag_matches_if_none_match('"abc"', 'W/"abc"')
    assert images._etag_matches_if_none_match('W/"abc"', '"abc"')
    assert not images._etag_matches_if_none_match('"abc"', '"xyz"')
    assert not images._etag_matches_if_none_match('"abc"', "")


def test_storage_streaming_response_returns_304_on_etag_match(
    tmp_path: Path,
) -> None:
    """If-None-Match hit must skip the file open entirely — no CIFS read,
    no Python streaming, just a 304 with the cache headers.
    """
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    path = tmp_path / "u" / "img.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"never-read")
    try:
        response = images._storage_streaming_response(
            path,
            media_type="image/png",
            etag='"abc123"',
            cache_control="private, max-age=31536000, immutable",
            storage_key="u/img.png",
            request=_request_with_headers({"if-none-match": '"abc123"'}),
        )
    finally:
        settings.storage_root = old

    assert response.status_code == 304
    assert response.headers["etag"] == '"abc123"'
    assert response.headers["cache-control"] == "private, max-age=31536000, immutable"
    # 304 must not carry a body or content-length — that's the whole point.
    assert "content-length" not in {k.lower() for k in response.headers}


def test_storage_streaming_response_emits_x_accel_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LUMEN_INTERNAL_REDIRECT_ENABLED=1 + storage_key both required —
    response carries X-Accel-Redirect with the internal alias path and no body.
    nginx native sendfile takes over from there.
    """
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    path = tmp_path / "u" / "img.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"never-read-by-python")

    monkeypatch.setenv("LUMEN_INTERNAL_REDIRECT_ENABLED", "1")
    try:
        response = images._storage_streaming_response(
            path,
            media_type="image/png",
            etag='"abc"',
            cache_control="private, max-age=31536000, immutable",
            storage_key="u/img.png",
            request=_request_with_headers({}),
        )
    finally:
        settings.storage_root = old

    assert response.status_code == 200
    assert response.headers["x-accel-redirect"] == "/_internal_storage/u/img.png"
    assert response.headers["etag"] == '"abc"'
    # Body is empty — nginx supplies the bytes via sendfile.
    assert response.body == b""


def test_storage_streaming_response_falls_back_to_streaming_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env, no storage_key, no nginx — Python keeps streaming the bytes.
    The fallback is what every existing deploy uses today; make sure the
    new code didn't break it.
    """
    old = settings.storage_root
    settings.storage_root = str(tmp_path)
    path = tmp_path / "u" / "img.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"streamed-bytes")

    monkeypatch.delenv("LUMEN_INTERNAL_REDIRECT_ENABLED", raising=False)
    try:
        response = images._storage_streaming_response(
            path,
            media_type="image/png",
            etag='"abc"',
            cache_control="private, max-age=31536000, immutable",
            storage_key="u/img.png",  # provided but env disabled — must not redirect
            request=_request_with_headers({}),
        )
    finally:
        settings.storage_root = old

    assert response.status_code == 200
    assert "x-accel-redirect" not in {k.lower() for k in response.headers}
    assert response.headers["content-length"] == str(len(b"streamed-bytes"))
