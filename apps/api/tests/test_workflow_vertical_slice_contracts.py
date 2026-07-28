from __future__ import annotations

import ast
from pathlib import Path


WORKFLOW_ROOT = Path(__file__).parents[1] / "app" / "workflows"
MIGRATED_APPLICATION_MODULES = (
    WORKFLOW_ROOT / "application" / "create_run.py",
    WORKFLOW_ROOT / "application" / "queries.py",
    WORKFLOW_ROOT / "application" / "upsert_project.py",
    WORKFLOW_ROOT / "application" / "commands.py",
    WORKFLOW_ROOT / "application" / "project_lifecycle.py",
)
WORKFLOW_PORT_MODULES = tuple(
    sorted(
        path
        for path in (WORKFLOW_ROOT / "ports").glob("*.py")
        if path.name != "__init__.py"
    )
)


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_migrated_workflow_application_has_no_transport_or_orm_imports() -> None:
    violations = {
        str(path): sorted(_import_roots(path) & {"fastapi", "sqlalchemy"})
        for path in MIGRATED_APPLICATION_MODULES
        if _import_roots(path) & {"fastapi", "sqlalchemy"}
    }
    assert violations == {}


def test_workflow_ports_have_no_opaque_or_framework_types() -> None:
    forbidden_names = {
        "Any",
        "AsyncSession",
        "BackgroundTasks",
        "Request",
        "User",
        "object",
    }
    violations: list[str] = []
    for path in WORKFLOW_PORT_MODULES:
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{path}:{node.lineno}:{node.id}")
    assert violations == []
