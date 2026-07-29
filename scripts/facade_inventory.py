#!/usr/bin/env python3
"""Discover compatibility facades and verify their retirement ledger."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "docs" / "refactors" / "compatibility-facade-retirement.json"
SOURCE_ROOTS = (
    (ROOT / "packages" / "core" / "lumen_core", "lumen_core"),
    (ROOT / "apps" / "api" / "app", "app"),
    (ROOT / "apps" / "worker" / "app", "app"),
    (ROOT / "apps" / "tgbot" / "app", "app"),
    (ROOT / "image-job" / "image_job", "image_job"),
)


@dataclass(frozen=True, order=True)
class FacadeFinding:
    path: str
    module: str
    public_api: tuple[str, ...]
    reason: str
    caller_count: int = 0


def _static_all(tree: ast.Module) -> tuple[str, ...] | None:
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in targets
        ):
            continue
        value = statement.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            return None
        names = [
            element.value
            for element in value.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)
        ]
        return tuple(sorted(set(names)))
    return None


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            names.update(
                alias.asname or alias.name.split(".", 1)[0]
                for alias in statement.names
            )
        elif isinstance(statement, ast.ImportFrom):
            names.update(
                alias.asname or alias.name
                for alias in statement.names
                if alias.name != "*"
            )
    return names


def _module_name(path: Path, source_root: Path, package: str) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join((package, *parts))


def _is_test_source(path: Path) -> bool:
    return (
        any(part in {"test", "tests", "__tests__"} for part in path.parts)
        or path.name.startswith("test_")
        or ".test." in path.name
        or ".spec." in path.name
    )


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _facade_reason(path: Path, tree: ast.Module) -> tuple[str, tuple[str, ...]] | None:
    public_api = _static_all(tree)
    if path.name == "compat.py":
        return "compat-module", public_api or ()
    if any(
        isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == "__getattr__"
        for statement in tree.body
    ):
        return "dynamic-getattr", public_api or ()
    definitions = [
        statement
        for statement in tree.body
        if isinstance(
            statement,
            (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        )
    ]
    if (
        path.name != "__init__.py"
        and public_api
        and not definitions
        and set(public_api).issubset(_imported_names(tree))
    ):
        return "re-export", public_api
    return None


def _absolute_import_targets(
    source_module: str,
    statement: ast.Import | ast.ImportFrom,
) -> set[str]:
    if isinstance(statement, ast.Import):
        return {alias.name for alias in statement.names}
    if statement.level:
        package_parts = source_module.split(".")[:-1]
        keep = max(0, len(package_parts) - statement.level + 1)
        prefix = package_parts[:keep]
        if statement.module:
            prefix.extend(statement.module.split("."))
        base = ".".join(prefix)
    else:
        base = statement.module or ""
    targets = {base} if base else set()
    targets.update(
        ".".join(part for part in (base, alias.name) if part)
        for alias in statement.names
        if alias.name != "*"
    )
    return targets


def _caller_count(
    module: str,
    *,
    roots: Iterable[tuple[Path, str]],
) -> int:
    callers: set[Path] = set()
    for source_root, package in roots:
        for path in source_root.rglob("*.py"):
            if _is_test_source(path):
                continue
            source_module = _module_name(path, source_root, package)
            if source_module == module:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for statement in tree.body:
                if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                    continue
                if module in _absolute_import_targets(source_module, statement):
                    callers.add(path)
                    break
    return len(callers)


def discover_facades(
    roots: Iterable[tuple[Path, str]] = SOURCE_ROOTS,
) -> dict[str, FacadeFinding]:
    resolved_roots = tuple(roots)
    findings: dict[str, FacadeFinding] = {}
    for source_root, package in resolved_roots:
        if not source_root.exists():
            continue
        for path in sorted(source_root.rglob("*.py")):
            if _is_test_source(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            result = _facade_reason(path, tree)
            if result is None:
                continue
            reason, public_api = result
            module = _module_name(path, source_root, package)
            relative = _display_path(path)
            findings[relative] = FacadeFinding(
                path=relative,
                module=module,
                public_api=public_api,
                reason=reason,
                caller_count=_caller_count(
                    module,
                    roots=(
                        resolved_roots
                        if package == "lumen_core"
                        else ((source_root, package),)
                    ),
                ),
            )
    return dict(sorted(findings.items()))


def load_ledger(path: Path = DEFAULT_LEDGER) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    facades = raw.get("facades")
    if raw.get("version") != 1 or not isinstance(facades, list):
        raise ValueError("unsupported compatibility facade retirement ledger")
    required = {
        "caller_count",
        "owner",
        "path",
        "retirement_condition",
        "status",
    }
    result: dict[str, dict[str, Any]] = {}
    for item in facades:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("invalid compatibility facade retirement entry")
        path_value = str(item["path"])
        if path_value in result:
            raise ValueError(f"duplicate compatibility facade: {path_value}")
        result[path_value] = item
    return result


def audit_facades(
    findings: dict[str, FacadeFinding],
    ledger: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    for path, finding in findings.items():
        entry = ledger.get(path)
        if entry is None:
            errors.append(f"unregistered compatibility facade: {path}")
            continue
        if int(entry.get("caller_count", -1)) != finding.caller_count:
            errors.append(
                f"facade caller count is stale: {path} "
                f"ledger={entry.get('caller_count')} current={finding.caller_count}"
            )
    for path in sorted(set(ledger) - set(findings)):
        errors.append(f"stale compatibility facade entry: {path}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args()
    try:
        findings = discover_facades()
        ledger = load_ledger(args.ledger)
        errors = audit_facades(findings, ledger)
    except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as exc:
        print(f"Facade inventory failed closed: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("Facade retirement inventory failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Facade retirement inventory passed: {len(findings)} facades registered."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
