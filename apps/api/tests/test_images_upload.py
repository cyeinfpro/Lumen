from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Request
from PIL import Image as PILImage

from app.config import settings
from app.routes import images
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


def test_upload_limiter_is_always_on() -> None:
    assert images.UPLOADS_LIMITER.always_on is True


def test_upload_mime_constants_only_allow_images() -> None:
    assert "text/plain" not in images.ALLOWED_MIME
    assert images.EXT_BY_MIME == {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }


def test_upload_limits_are_bounded() -> None:
    assert images.MAX_BYTES == 50 * 1024 * 1024
    assert images.MAX_LONG_SIDE == 4096
    assert images.PILImage.MAX_IMAGE_PIXELS == images.MAX_IMAGE_PIXELS


def test_decompression_bomb_error_maps_to_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_fp):
        raise images.PILImage.DecompressionBombError("too many pixels")

    monkeypatch.setattr(images.PILImage, "open", boom)

    with pytest.raises(Exception) as excinfo:
        images._open_image_bytes(b"not-used")

    assert getattr(excinfo.value, "status_code", None) == 413
    assert excinfo.value.detail["error"]["code"] == "too_many_pixels"


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
        "u/user_1/g/gen_1/orig.display2048.webp"
    )


def test_make_display_variant_downsizes_and_encodes_webp(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    PILImage.new("RGB", (3000, 1500), color=(20, 40, 60)).save(src, format="PNG")

    data, size = images._make_display_variant(src)

    assert size == (2048, 1024)
    assert data.startswith(b"RIFF")
    assert b"WEBP" in data[:16]


@pytest.mark.asyncio
async def test_delete_image_writes_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_write_audit(db, **kwargs):
        db.add(AuditLog(**kwargs))
        await db.flush()

    monkeypatch.setattr(images, "write_audit", fake_write_audit)
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
