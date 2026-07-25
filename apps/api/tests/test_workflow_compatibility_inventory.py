from __future__ import annotations

import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.routes import workflows
from app.workflows.compatibility import (
    RETIRED_HIDDEN_DEPENDENCIES,
    WORKFLOW_COMPATIBILITY_EXPORTS,
)


def test_workflow_compatibility_exports_have_explicit_owners() -> None:
    assert WORKFLOW_COMPATIBILITY_EXPORTS
    assert len({item.legacy_path for item in WORKFLOW_COMPATIBILITY_EXPORTS}) == len(
        WORKFLOW_COMPATIBILITY_EXPORTS
    )
    assert all(
        item.owner_path.startswith("app.") for item in WORKFLOW_COMPATIBILITY_EXPORTS
    )
    assert (
        "app.routes.workflow_routes._facade.RouteFacade" in RETIRED_HIDDEN_DEPENDENCIES
    )


def test_workflow_http_contract_characterization() -> None:
    contracts = {
        (next(iter(route.methods or set())), route.path, route.name)
        for route in workflows.router.routes
        if isinstance(route, APIRoute)
    }
    assert (
        "POST",
        "/workflows/apparel-model-showcase",
        "create_apparel_model_showcase",
    ) in contracts
    assert (
        "GET",
        "/workflows/apparel-model-library",
        "list_apparel_model_library",
    ) in contracts
    assert (
        "POST",
        "/workflows/poster-design",
        "create_poster_design_workflow",
    ) in contracts


def test_workflow_layers_have_no_dynamic_module_lookup_or_route_imports() -> None:
    app_root = Path(__file__).parents[1] / "app"
    roots = (
        app_root / "routes" / "workflows.py",
        app_root / "workflows",
        app_root / "workflow_domain",
        app_root / "workflow_services",
        app_root / "routes" / "workflow_routes",
        app_root / "routes" / "poster_styles.py",
        *app_root.glob("routes/_apparel_*.py"),
        *app_root.glob("routes/_showcase_*.py"),
    )
    violations: list[str] = []
    for root in roots:
        paths = (root,) if root.is_file() else root.rglob("*.py")
        for path in paths:
            source = path.read_text("utf-8")
            tree = ast.parse(source)
            if path == app_root / "routes" / "workflows.py":
                line_count = len(source.splitlines())
                if line_count > 1500:
                    violations.append(
                        f"{path}: route module exceeds 1500 lines ({line_count})"
                    )
            for forbidden in (
                "sys.modules",
                "importlib",
                "export_to_facade",
                "globals()",
            ):
                if forbidden in source:
                    violations.append(f"{path}: forbidden runtime facade {forbidden}")
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name.startswith("_") and not alias.name.startswith(
                            "__"
                        ):
                            violations.append(
                                f"{path}: private import {node.module}:{alias.name}"
                            )
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                    and isinstance(node.value, ast.Name)
                    and node.value.id.startswith("_")
                ):
                    violations.append(
                        f"{path}: private module backread {node.value.id}.{node.attr}"
                    )
                if isinstance(node, ast.Global):
                    violations.append(f"{path}: global state {', '.join(node.names)}")
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not isinstance(value, (ast.Dict, ast.List, ast.Set)):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                names = [
                    target.id
                    for target in targets
                    if isinstance(target, ast.Name) and target.id != "__all__"
                ]
                if names:
                    violations.append(
                        f"{path}: module mutable state {', '.join(names)}"
                    )
            if "workflow_services" in path.parts:
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        if ".routes" in node.module or node.module.startswith(
                            "app.routes"
                        ):
                            violations.append(f"{path}: route import {node.module}")
    assert violations == []
