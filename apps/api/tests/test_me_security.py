from __future__ import annotations

import io
from pathlib import Path

from app.config import settings
from app.routes import me


def test_export_storage_path_stays_under_storage_root(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    old_root = settings.storage_root
    settings.storage_root = str(root)
    try:
        assert me._fs_path_safe("u/user_1/image.png") == (
            root / "u/user_1/image.png"
        ).resolve()
        assert me._fs_path_safe("") is None
        assert me._fs_path_safe("   ") is None
        assert me._fs_path_safe("bad\x00name.png") is None
        assert me._fs_path_safe(str(root / "u/user_1/image.png")) is None
        assert me._fs_path_safe("../storage_sibling/image.png") is None
        assert me._fs_path_safe("/u/user_1/image.png") is None
    finally:
        settings.storage_root = old_root


def test_export_storage_path_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    old_root = settings.storage_root
    settings.storage_root = str(root)
    try:
        assert me._fs_path_safe("link/image.png") is None
    finally:
        settings.storage_root = old_root


def test_export_tempfile_iterator_closes_on_early_close() -> None:
    tmp = io.BytesIO(b"export-data")
    gen = me._iter_tempfile_and_close(tmp)

    assert next(gen) == b"export-data"
    gen.close()

    assert tmp.closed is True
