from __future__ import annotations

import ast
from pathlib import Path

from app import video_upstream
from app.video_upstream_parts import adapters, parsing


_VIDEO_CLUSTER_PATHS = (
    Path(__file__).parents[1] / "app" / "video_upstream.py",
    Path(__file__).parents[1] / "app" / "video_artifacts.py",
    Path(__file__).parents[1] / "app" / "video_upstream_parts",
    Path(__file__).parents[1] / "app" / "tasks" / "video_generation.py",
    Path(__file__).parents[1] / "app" / "tasks" / "video_generation_parts",
)


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


def test_video_cluster_has_no_runtime_coupling_inventory() -> None:
    findings: list[str] = []
    paths: list[Path] = []
    for path in _VIDEO_CLUSTER_PATHS:
        paths.extend(path.glob("*.py") if path.is_dir() else (path,))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                findings.extend(
                    f"{path.name}:private-import:{alias.name}"
                    for alias in node.names
                    if alias.name.startswith("_") and not alias.name.startswith("__")
                )
            elif isinstance(node, ast.Global):
                findings.extend(f"{path.name}:global:{name}" for name in node.names)
        for node in tree.body:
            value = (
                node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            )
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else (node.target,)
                if isinstance(node, ast.AnnAssign)
                else ()
            )
            if any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in targets
            ):
                continue
            if isinstance(value, (ast.Dict, ast.List, ast.Set)):
                findings.append(f"{path.name}:module-mutable-state")
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {"dict", "list", "set"}
            ):
                findings.append(f"{path.name}:module-mutable-state")

    assert findings == []
