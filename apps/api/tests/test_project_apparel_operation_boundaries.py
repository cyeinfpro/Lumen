from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).parents[1] / "app" / "workflows"
OPERATION_PATHS = (
    APP_ROOT / "adapters" / "operations" / "projects.py",
    APP_ROOT / "adapters" / "operations" / "apparel.py",
)
APPLICATION_PATHS = (
    APP_ROOT / "application" / "project_lifecycle.py",
    APP_ROOT / "application" / "project_candidate_rules.py",
    APP_ROOT / "application" / "apparel_library.py",
    APP_ROOT / "application" / "apparel_workflow_rules.py",
)


def test_project_and_apparel_operations_have_no_compatibility_forwarders() -> None:
    violations: list[str] = []
    for path in OPERATION_PATHS:
        source = path.read_text("utf-8")
        tree = ast.parse(source)
        if "F401" in source or "F405" in source:
            violations.append(f"{path}: compatibility noqa")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        violations.append(
                            f"{path}:{node.lineno}: private import {alias.name}"
                        )
            if (
                isinstance(node, ast.Attribute)
                and node.attr.startswith("_")
                and not node.attr.startswith("__")
                and isinstance(node.value, ast.Name)
                and node.value.id.startswith("_")
            ):
                violations.append(
                    f"{path}:{node.lineno}: private module backread "
                    f"{node.value.id}.{node.attr}"
                )
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, (ast.Name, ast.Attribute)):
                continue
            names = [
                target.id for target in node.targets if isinstance(target, ast.Name)
            ]
            if names:
                violations.append(
                    f"{path}:{node.lineno}: pure forwarding assignment {names}"
                )
    assert violations == []


def test_project_and_apparel_application_slices_do_not_import_adapters() -> None:
    violations: list[str] = []
    for path in APPLICATION_PATHS:
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            module = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else ",".join(alias.name for alias in node.names)
            )
            if module and (
                "adapters" in module
                or module.startswith("fastapi")
                or module.startswith("sqlalchemy")
            ):
                violations.append(f"{path}:{node.lineno}: {module}")
    assert violations == []
