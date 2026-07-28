from __future__ import annotations

import ast
from pathlib import Path

from app import video_upstream_service as video_upstream
from app.video_upstream_parts import adapters, parsing


_VIDEO_CLUSTER_PATHS = (
    Path(__file__).parents[1] / "app" / "video_upstream_service.py",
    Path(__file__).parents[1] / "app" / "video_artifacts.py",
    Path(__file__).parents[1] / "app" / "video_upstream_parts",
    Path(__file__).parents[1]
    / "app"
    / "tasks"
    / "video_generation_parts"
    / "entrypoints.py",
    Path(__file__).parents[1] / "app" / "tasks" / "video_generation_parts",
)


def test_video_upstream_service_exposes_adapter_contracts() -> None:
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


def test_int_parser_keeps_full_precision_for_large_string_tokens() -> None:
    """上游把 token 数当字符串回传时不得走 float：低位会被抹掉。

    该值直接进视频结算金额，int(float(...)) 的舍入等于凭空改写上游用量。
    """
    raw = "1234567890123456789"

    assert int(float(raw)) == 1234567890123456768  # 旧实现的错值，作为对照
    assert parsing._int_or_none(raw) == 1234567890123456789


def test_int_parser_rejects_non_finite_and_negative_values() -> None:
    assert parsing._int_or_none("nan") is None
    assert parsing._int_or_none("inf") is None
    assert parsing._int_or_none("-1") is None
    assert parsing._int_or_none(True) is None
    assert parsing._int_or_none(None) is None
    assert parsing._int_or_none("not-a-number") is None
    # 既有形态保持不变：整数、百分号后缀、小数字符串都按截断取整。
    assert parsing._int_or_none(42) == 42
    assert parsing._int_or_none(" 75% ") == 75
    assert parsing._int_or_none("12.9") == 12


def test_duration_tokens_round_up_so_platform_never_eats_the_remainder() -> None:
    """时长换算 token 只能向上取整：抹掉的尾数就是平台替用户垫付的成本。"""
    # 1_000_000 token/秒，小数第七位起就产生不足一个 token 的尾数。
    raw = "5.0000001"

    tokens = parsing._duration_usage_total_tokens({"usage": {"duration": raw}})

    assert tokens == 5_000_001  # 旧的 ROUND_HALF_UP 会算成 5_000_000
    # 整秒时长不受影响，不会凭空多收。
    assert (
        parsing._duration_usage_total_tokens({"usage": {"duration": "5"}}) == 5_000_000
    )
    assert parsing._duration_usage_total_tokens({"usage": {"duration": 0}}) == 0


def test_video_upstream_production_modules_stay_below_file_size_budget() -> None:
    root = Path(__file__).parents[1] / "app"
    paths = [
        root / "video_upstream_service.py",
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
