#!/usr/bin/env python3
"""Reject Alembic operations that are unsafe for blue/green deploys.

The linter only inspects upgrade() bodies. Downgrade paths are allowed to drop
objects because they are not executed during a rolling update.
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


BREAKING_METHODS = {
    "drop_column": "drop column",
    "drop_table": "drop table",
    "rename_column": "rename column",
    "rename_table": "rename table",
}
ALEMBIC_VERSION_NUM_MAX = 32


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id == "op":
            return func.attr
    return None


def _literal_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _literal_none(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


class UpgradeVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[Violation] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node)
        if name in BREAKING_METHODS:
            self.violations.append(
                Violation(self.path, node.lineno, f"op.{name} is a breaking {BREAKING_METHODS[name]} operation")
            )
        elif name == "alter_column":
            nullable = _literal_bool(_keyword(node, "nullable"))
            server_default = _keyword(node, "server_default")
            if nullable is False and (server_default is None or _literal_none(server_default)):
                self.violations.append(
                    Violation(
                        self.path,
                        node.lineno,
                        "op.alter_column(nullable=False) must set a server_default or be split into expand/contract",
                    )
                )
        elif name == "create_check_constraint":
            not_valid = _literal_bool(_keyword(node, "postgresql_not_valid"))
            if not_valid is not True:
                self.violations.append(
                    Violation(
                        self.path,
                        node.lineno,
                        "op.create_check_constraint must use a NOT VALID/VALIDATE pattern for rolling deploys",
                    )
                )
        self.generic_visit(node)


def _upgrade_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upgrade"
    ]


def _module_string_assignment(tree: ast.AST, name: str) -> tuple[str, int] | None:
    for node in getattr(tree, "body", []):
        value: ast.AST | None = None
        lineno = getattr(node, "lineno", 1)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value = node.value
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value, lineno
    return None


def lint_file(path: Path) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 1, f"syntax error: {exc.msg}")]
    out: list[Violation] = []
    revision = _module_string_assignment(tree, "revision")
    if revision is not None:
        revision_id, line = revision
        if len(revision_id) > ALEMBIC_VERSION_NUM_MAX:
            out.append(
                Violation(
                    path,
                    line,
                    "revision id exceeds alembic_version.version_num "
                    f"VARCHAR({ALEMBIC_VERSION_NUM_MAX})",
                )
            )
    for fn in _upgrade_functions(tree):
        visitor = UpgradeVisitor(path)
        for stmt in fn.body:
            visitor.visit(stmt)
        out.extend(visitor.violations)
    return out


def _git_changed_files() -> list[Path]:
    base_ref = os.environ.get("GITHUB_BASE_REF")
    ranges: list[list[str]] = []
    if base_ref:
        ranges.append(["git", "diff", "--name-only", "--diff-filter=AM", f"origin/{base_ref}...HEAD"])
    ranges.extend(
        [
            ["git", "diff", "--name-only", "--diff-filter=AM", "--cached"],
            ["git", "diff", "--name-only", "--diff-filter=AM"],
        ]
    )
    seen: set[Path] = set()
    for cmd in ranges:
        try:
            raw = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            continue
        for line in raw.splitlines():
            path = Path(line.strip())
            if path.match("apps/api/alembic/versions/*.py"):
                seen.add(path)
    return sorted(seen)


def _has_breaking_marker(message_file: str | None) -> bool:
    if os.environ.get("LUMEN_ALLOW_BREAKING_ALEMBIC") == "1":
        return True
    if not message_file:
        return False
    try:
        text = Path(message_file).read_text(encoding="utf-8")
    except OSError:
        return False
    return "BREAKING:" in text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--allow-breaking", action="store_true")
    parser.add_argument("--commit-message-file")
    args = parser.parse_args(argv)

    files = args.files or _git_changed_files()
    files = [path for path in files if path.exists() and path.match("apps/api/alembic/versions/*.py")]
    violations = [violation for path in files for violation in lint_file(path)]
    if not violations:
        return 0
    if args.allow_breaking or _has_breaking_marker(args.commit_message_file):
        print("Alembic breaking operations acknowledged by BREAKING marker:", file=sys.stderr)
        for item in violations:
            print(f"  {item.path}:{item.line}: {item.message}", file=sys.stderr)
        return 0
    print("Unsafe Alembic migration operations found:", file=sys.stderr)
    for item in violations:
        print(f"  {item.path}:{item.line}: {item.message}", file=sys.stderr)
    print("Add a BREAKING: runbook note only for planned downtime migrations.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
