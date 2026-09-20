"""Preset copies cannot block API traffic or leave files after cancellation."""
from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import threading
from types import SimpleNamespace

from PIL import Image as PILImage
import pytest

from app.workflows.adapters import library_materialization as materialization


class FakeDb:
    def __init__(self, existing=None, fail_flush=False):
        self.existing = existing
        self.fail_flush = fail_flush
        self.rows = []
        self.loop_thread = threading.get_ident()

    async def connection(self):
        assert threading.get_ident() == self.loop_thread
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, _statement):
        assert threading.get_ident() == self.loop_thread
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, row):
        assert threading.get_ident() == self.loop_thread
        self.rows.append(row)

    async def flush(self):
        assert threading.get_ident() == self.loop_thread
        if self.fail_flush:
            raise RuntimeError("database unavailable")


@pytest.fixture
def preset(tmp_path, monkeypatch):
    source_key = "presets/model.png"
    source = tmp_path / source_key
    source.parent.mkdir()
    buffer = BytesIO()
    PILImage.new("RGB", (3, 2)).save(buffer, format="PNG")
    source.write_bytes(buffer.getvalue())
    monkeypatch.setattr(materialization, "_storage_path", lambda key: tmp_path / key)

    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    monkeypatch.setattr(materialization, "_write_bytes_replace", write)
    return tmp_path, source, {"id": "preset:model:1", "image_storage_key": source_key}


@pytest.mark.asyncio
async def test_preset_storage_work_is_off_loop_but_database_work_is_not(preset, monkeypatch):
    root, source, item = preset
    loop_thread = threading.get_ident()
    original = materialization._copy_preset_binary_sync

    def copy(*args):
        assert threading.get_ident() != loop_thread
        return original(*args)

    monkeypatch.setattr(materialization, "_copy_preset_binary_sync", copy)
    db = FakeDb()
    image = await materialization.create_user_image_from_preset(db, user_id="owner", item=item)
    data = source.read_bytes()
    assert (root / image.storage_key).read_bytes() == data
    assert (image.width, image.height, image.size_bytes) == (3, 2, len(data))
    assert image.sha256 == hashlib.sha256(data).hexdigest()
    assert image.visibility == "private"
    assert db.rows == [image]


@pytest.mark.asyncio
async def test_existing_preset_copy_does_not_touch_storage(preset, monkeypatch):
    _root, _source, item = preset
    expected = SimpleNamespace(id="existing")

    def unexpected(*_args):
        pytest.fail("existing private image should not be copied again")

    monkeypatch.setattr(materialization, "_copy_preset_binary_sync", unexpected)
    assert await materialization.create_user_image_from_preset(
        FakeDb(existing=expected), user_id="owner", item=item,
    ) is expected


@pytest.mark.asyncio
async def test_failed_flush_removes_uncommitted_copy_without_deleting_preset(preset):
    root, source, item = preset
    with pytest.raises(RuntimeError, match="database unavailable"):
        await materialization.create_user_image_from_preset(
            FakeDb(fail_flush=True), user_id="owner", item=item,
        )
    assert source.is_file()
    assert not list((root / "u").rglob("*.png"))


@pytest.mark.asyncio
async def test_missing_binary_has_no_database_insert(preset):
    _root, source, item = preset
    source.unlink()
    db = FakeDb()
    with pytest.raises(Exception) as error:
        await materialization.create_user_image_from_preset(db, user_id="owner", item=item)
    assert error.value.status_code == 404
    assert not db.rows


@pytest.mark.asyncio
async def test_cancelled_copy_drains_writer_before_cleanup_and_keeps_loop_responsive(preset, monkeypatch):
    root, source, item = preset
    started, release = threading.Event(), threading.Event()
    original = materialization._copy_preset_binary_sync
    loop_thread = threading.get_ident()

    def blocked_copy(*args):
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(timeout=5), "event loop did not release the background writer"
        return original(*args)

    monkeypatch.setattr(materialization, "_copy_preset_binary_sync", blocked_copy)
    db = FakeDb()
    task = asyncio.create_task(materialization.create_user_image_from_preset(
        db, user_id="owner", item=item,
    ))
    try:
        for _ in range(200):
            if started.is_set() or task.done():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cleanup must wait for the uncancellable file writer"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not db.rows
    assert source.is_file()
    assert not list((root / "u").rglob("*.png"))
