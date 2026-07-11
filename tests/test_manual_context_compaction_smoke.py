from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest


_SCRIPT_GLOBALS = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "manual_context_compaction_smoke.py")
)
_validated_base_url = cast(Callable[[str], str], _SCRIPT_GLOBALS["_validated_base_url"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:8000/", "http://localhost:8000"),
        (" https://api.example.test/lumen/ ", "https://api.example.test/lumen"),
    ],
)
def test_validated_base_url_accepts_http_urls(value: str, expected: str) -> None:
    assert _validated_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "file:///tmp/lumen.sock",
        "ftp://example.test/api",
        "localhost:8000",
        "https://user:secret@example.test",
        "",
    ],
)
def test_validated_base_url_rejects_unsafe_urls(value: str) -> None:
    with pytest.raises(ValueError):
        _validated_base_url(value)
