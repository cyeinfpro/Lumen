from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest


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
load_layer_config = MODULE.load_layer_config
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


def test_architecture_gate_blocks_workflow_v2_legacy_dependencies(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    paths = (
        package / "__init__.py",
        package / "workflows/__init__.py",
        package / "workflows/application/__init__.py",
        package / "routes/__init__.py",
        package / "routes/workflow_routes/__init__.py",
        package / "workflow_services/__init__.py",
        package / "workflow_services/serialization.py",
        package / "workflow_domain/__init__.py",
        package / "workflow_domain/legacy.py",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (package / "workflows/application/queries.py").write_text(
        "from ...workflow_services import serialization\n",
        encoding="utf-8",
    )
    (package / "routes/workflow_routes/projects.py").write_text(
        "from ...workflow_domain import legacy\n",
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
            "workflow-v2-to-legacy",
            "app/routes/workflow_routes/projects.py",
            "app.workflow_domain.legacy",
        ),
        ArchitectureViolation(
            "workflow-v2-to-legacy",
            "app/workflows/application/queries.py",
            "app.workflow_services.serialization",
        ),
    ]


def test_architecture_gate_blocks_workflow_domain_upward_dependencies(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    paths = (
        package / "__init__.py",
        package / "workflows/__init__.py",
        package / "workflows/domain/__init__.py",
        package / "workflows/application/__init__.py",
        package / "workflows/adapters/__init__.py",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (package / "workflows/domain/policy.py").write_text(
        "from ..application import submit\n"
        "from ..adapters import sqlalchemy_repository\n",
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
            "workflow-domain-layering",
            "app/workflows/domain/policy.py",
            "app.workflows.adapters",
        ),
        ArchitectureViolation(
            "workflow-domain-layering",
            "app/workflows/domain/policy.py",
            "app.workflows.application",
        ),
    ]


def test_architecture_gate_blocks_workflow_http_to_adapter_shortcuts(
    tmp_path: Path,
) -> None:
    package = tmp_path / "app"
    paths = (
        package / "__init__.py",
        package / "routes/__init__.py",
        package / "routes/workflow_routes/__init__.py",
        package / "workflows/__init__.py",
        package / "workflows/adapters/__init__.py",
        package / "workflows/adapters/repository.py",
        package / "workflows/transport/__init__.py",
        package / "workflows/transport/http/__init__.py",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    (package / "routes/workflow_routes/projects.py").write_text(
        "from ...workflows.adapters import repository\n",
        encoding="utf-8",
    )
    (package / "workflows/transport/http/poster.py").write_text(
        "from ...adapters import repository\n",
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
            "workflow-http-to-adapters",
            "app/routes/workflow_routes/projects.py",
            "app.workflows.adapters.repository",
        ),
        ArchitectureViolation(
            "workflow-http-to-adapters",
            "app/workflows/transport/http/poster.py",
            "app.workflows.adapters.repository",
        ),
    ]


def test_architecture_graph_scan_fails_closed_on_missing_package(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"

    try:
        build_package_graph(PackageSpec("image-job", missing, "image_job"))
    except FileNotFoundError as error:
        assert "architecture package root is missing" in str(error)
    else:
        raise AssertionError("missing architecture package root was silently skipped")


def test_architecture_baseline_requires_debt_reduction_to_be_recorded() -> None:
    baseline = {"known", "removed"}
    assert compare_violations({"known"}, baseline) == [
        "architecture violation baseline is stale: removed"
    ]
    assert compare_violations({"known", "new"}, baseline) == [
        "new architecture violation: new",
        "architecture violation baseline is stale: removed",
    ]
    assert compare_cycles({"known"}, baseline) == [
        "architecture cycle baseline is stale: removed"
    ]
    assert compare_cycles({"known", "new"}, baseline) == [
        "new architecture cycle: new",
        "architecture cycle baseline is stale: removed",
    ]


def test_architecture_layers_are_loaded_from_toml() -> None:
    config = load_layer_config(ROOT / "scripts" / "architecture-layers.toml")

    assert {package.name for package in config.packages} == {
        "api",
        "core",
        "image-job",
        "tgbot",
        "worker",
    }
    assert {rule[1] for rule in config.layer_rules} == {
        "api-lower-to-routes",
        "image-job-lower-to-http",
        "worker-lower-to-tasks",
    }
    assert "workflow-domain-layering" in {
        rule[1] for rule in config.forbidden_rules
    }


def test_architecture_layers_reject_unknown_package(tmp_path: Path) -> None:
    config = tmp_path / "layers.toml"
    config.write_text(
        """
version = 1

[[packages]]
name = "api"
package = "app"
root = "apps/api/app"

[[rules]]
name = "bad"
package = "missing"
sources = ["app.services"]
targets = ["app.routes"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown packages"):
        load_layer_config(config, root=tmp_path)
