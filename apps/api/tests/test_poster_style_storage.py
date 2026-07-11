from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.poster_styles import storage


@pytest.mark.parametrize(
    "storage_key",
    ["", "\x00bad", "/absolute/path", "../escape"],
)
def test_storage_path_rejects_invalid_keys(
    tmp_path: Path,
    storage_key: str,
) -> None:
    with pytest.raises(HTTPException):
        storage.resolve_storage_path(storage_key, root=tmp_path.resolve())


def test_storage_json_round_trip_is_atomic_and_bounded(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    storage.write_json_atomic(path, {"value": "ok"}, max_bytes=1024)

    assert storage.read_json_file(path, {}, max_bytes=1024) == {"value": "ok"}
    assert not list(tmp_path.glob("*.tmp"))

    with pytest.raises(ValueError, match="exceeds"):
        storage.write_json_atomic(path, {"value": "too-large"}, max_bytes=4)


def test_storage_json_missing_returns_a_copy(tmp_path: Path) -> None:
    default = {"items": []}
    result = storage.read_json_file(
        tmp_path / "missing.json",
        default,
        max_bytes=1024,
    )

    assert result == default
    assert result is not default


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("cover.jpg", "image/jpeg"),
        ("cover.JPEG", "image/jpeg"),
        ("cover.png", "image/png"),
        ("cover.webp", "image/webp"),
        ("cover.bin", "application/octet-stream"),
    ],
)
def test_guess_mime(filename: str, expected: str) -> None:
    assert storage.guess_mime(Path(filename)) == expected
