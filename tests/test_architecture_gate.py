from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "check_architecture",
    ROOT / "scripts" / "check_architecture.py",
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ArchitectureViolation = MODULE.ArchitectureViolation
PackageSpec = MODULE.PackageSpec
build_package_graph = MODULE.build_package_graph
collect_violations = MODULE.collect_violations
compare_violations = MODULE.compare_violations
compare_cycles = MODULE.compare_cycles
strongly_connected_components = MODULE.strongly_connected_components


def test_architecture_gate_detects_relative_cycle(tmp_path: Path) -> None:
    package = tmp_path / "app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "first.py").write_text(
        "from .second import value\n",
        encoding="utf-8",
    )
    (package / "second.py").write_text(
        "from .first import value\n",
        encoding="utf-8",
    )

    graph = build_package_graph(PackageSpec("api", package, "app"))

    assert strongly_connected_components(graph.edges) == [
        ("app.first", "app.second"),
    ]


def test_architecture_gate_finds_lower_layer_route_import(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    (package / "services").mkdir(parents=True)
    (package / "routes").mkdir()
    for path in (
        package / "__init__.py",
        package / "services/__init__.py",
        package / "routes/__init__.py",
        package / "routes/jobs.py",
    ):
        path.write_text("", encoding="utf-8")
    (package / "services/submit.py").write_text(
        "from ..routes.jobs import enqueue\n",
        encoding="utf-8",
    )

    original_root = MODULE.ROOT
    MODULE.ROOT = tmp_path
    try:
        violations, cycles = collect_violations((PackageSpec("api", package, "app"),))
    finally:
        MODULE.ROOT = original_root

    assert cycles == {}
    assert list(violations.values()) == [
        ArchitectureViolation(
            "api-lower-to-routes",
            "app/services/submit.py",
            "app.routes.jobs",
        )
    ]


def test_architecture_baseline_only_allows_debt_to_shrink() -> None:
    baseline = {"known", "removed"}
    assert compare_violations({"known"}, baseline) == []
    assert compare_violations({"known", "new"}, baseline) == [
        "new architecture violation: new"
    ]
    assert compare_cycles({"known"}, baseline) == []
    assert compare_cycles({"known", "new"}, baseline) == ["new architecture cycle: new"]
