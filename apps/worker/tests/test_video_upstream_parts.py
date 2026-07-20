from __future__ import annotations

from pathlib import Path

from app import video_upstream
from app.video_upstream_parts import adapters, parsing


def test_video_upstream_facade_reexports_adapter_contracts() -> None:
    assert video_upstream.VolcanoSeedanceAdapter is adapters.VolcanoSeedanceAdapter
    assert video_upstream.VideoSubmitRequest.__module__.endswith(".contracts")
    assert video_upstream.VideoUpstreamError.__module__.endswith(".contracts")


def test_video_url_parser_keeps_nested_result_collection_compatibility() -> None:
    payload = {
        "output": {
            "results": [{"url": "https://cdn.example/output.mp4"}],
        }
    }

    assert parsing._video_url(payload) == "https://cdn.example/output.mp4"
    assert video_upstream._video_url(payload) == "https://cdn.example/output.mp4"


def test_explicit_video_url_parser_accepts_video_url_collections() -> None:
    payload = {"data": {"video_urls": ["https://cdn.example/output.mp4"]}}

    assert parsing._explicit_video_result_url(payload) == (
        "https://cdn.example/output.mp4"
    )


def test_video_upstream_production_modules_stay_below_file_size_budget() -> None:
    root = Path(__file__).parents[1] / "app"
    paths = [
        root / "video_upstream.py",
        *sorted((root / "video_upstream_parts").glob("*.py")),
    ]

    assert (
        max(len(path.read_text(encoding="utf-8").splitlines()) for path in paths)
        <= 1500
    )
