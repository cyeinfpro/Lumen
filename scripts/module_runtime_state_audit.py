#!/usr/bin/env python3
"""Ratchet module-level mutable runtime instances in API and worker packages.

The main architecture audit catches literal list/dict/set state and global
statements. Stateful dataclass instances can otherwise hide the same process
ownership behind a harmless-looking constructor. This gate inventories those
instances and requires every grandfathered module to have an explicit owner and
retirement condition.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "docs" / "refactors" / "module-runtime-state-ledger.json"
DEFAULT_SOURCE_ROOTS = (
    ROOT / "apps" / "api" / "app",
    ROOT / "apps" / "worker" / "app",
)


@dataclass(frozen=True, order=True)
class ModuleRuntimeFinding:
    path: str
    line: int
    symbol: str
    class_name: str

    @property
    def key(self) -> str:
        return f"{self.path}|{self.symbol}|{self.class_name}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_name": self.class_name,
            "line": self.line,
            "path": self.path,
            "symbol": self.symbol,
        }


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for element in node.elts for name in _target_names(element)]
    return []


def _dataclass_is_frozen(decorator: ast.AST) -> bool:
    if not isinstance(decorator, ast.Call):
        return False
    if _call_name(decorator.func).rsplit(".", 1)[-1] != "dataclass":
        return False
    for keyword in decorator.keywords:
        if keyword.arg != "frozen":
            continue
        return isinstance(keyword.value, ast.Constant) and keyword.value.value is True
    return False


def _mutable_local_dataclasses(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef):
            continue
        decorated = False
        frozen = False
        for decorator in statement.decorator_list:
            name = (
                _call_name(decorator.func)
                if isinstance(decorator, ast.Call)
                else _call_name(decorator)
            )
            if name.rsplit(".", 1)[-1] != "dataclass":
                continue
            decorated = True
            frozen = _dataclass_is_frozen(decorator)
            break
        if decorated and not frozen:
            names.add(statement.name)
    return names


def iter_python_sources(
    roots: Iterable[Path] = DEFAULT_SOURCE_ROOTS,
) -> Iterable[Path]:
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            yield root
            continue
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" not in path.parts and "tests" not in path.parts:
                yield path


def collect_module_runtime_findings(
    roots: Iterable[Path] = DEFAULT_SOURCE_ROOTS,
    *,
    root: Path = ROOT,
) -> dict[str, ModuleRuntimeFinding]:
    findings: dict[str, ModuleRuntimeFinding] = {}
    for path in iter_python_sources(roots):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        mutable_classes = _mutable_local_dataclasses(tree)
        if not mutable_classes:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.as_posix()
        for statement in tree.body:
            value: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(statement, ast.Assign):
                value = statement.value
                targets = list(statement.targets)
            elif isinstance(statement, ast.AnnAssign):
                value = statement.value
                targets = [statement.target]
            if not isinstance(value, ast.Call):
                continue
            class_name = _call_name(value.func)
            if class_name not in mutable_classes:
                continue
            for target in targets:
                for symbol in _target_names(target):
                    finding = ModuleRuntimeFinding(
                        path=relative,
                        line=int(getattr(statement, "lineno", 0)),
                        symbol=symbol,
                        class_name=class_name,
                    )
                    findings[finding.key] = finding
    return dict(sorted(findings.items()))


def load_ledger(path: Path = DEFAULT_LEDGER) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    modules = raw.get("modules")
    if raw.get("version") != 1 or not isinstance(modules, list):
        raise ValueError("unsupported module runtime state ledger")
    required = {"path", "owner", "max_instances", "retirement_condition"}
    normalized: dict[str, dict[str, Any]] = {}
    for item in modules:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("invalid module runtime state ledger entry")
        module_path = str(item["path"])
        max_instances = item["max_instances"]
        if not isinstance(max_instances, int) or max_instances < 0:
            raise ValueError(f"invalid max_instances for {module_path}")
        symbols = item.get("symbols")
        if symbols is not None and (
            not isinstance(symbols, list)
            or not all(isinstance(symbol, str) and symbol for symbol in symbols)
        ):
            raise ValueError(f"invalid symbols for {module_path}")
        if module_path in normalized:
            raise ValueError(f"duplicate module runtime state entry: {module_path}")
        normalized[module_path] = {
            **item,
            "symbols": sorted(set(symbols or [])),
        }
    max_total = raw.get("max_total")
    if not isinstance(max_total, int) or max_total < 0:
        raise ValueError("module runtime state ledger needs non-negative max_total")
    return {"max_total": max_total, "modules": normalized, "version": 1}


def audit_runtime_state(
    findings: dict[str, ModuleRuntimeFinding],
    ledger: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    grouped: dict[str, list[ModuleRuntimeFinding]] = defaultdict(list)
    for finding in findings.values():
        grouped[finding.path].append(finding)
    allowed: dict[str, dict[str, Any]] = ledger["modules"]

    if len(findings) > ledger["max_total"]:
        errors.append(
            f"module runtime state total grew: current={len(findings)} "
            f"budget={ledger['max_total']}"
        )

    for path, path_findings in sorted(grouped.items()):
        entry = allowed.get(path)
        if entry is None:
            for finding in sorted(path_findings):
                errors.append(f"unowned module runtime state: {finding.key}")
            continue
        if len(path_findings) > entry["max_instances"]:
            errors.append(
                f"module runtime state budget grew: {path} "
                f"current={len(path_findings)} budget={entry['max_instances']}"
            )
        expected_symbols = set(entry.get("symbols") or [])
        if expected_symbols:
            actual_symbols = {finding.symbol for finding in path_findings}
            for symbol in sorted(actual_symbols - expected_symbols):
                errors.append(f"unexpected module runtime symbol: {path}|{symbol}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args()

    findings = collect_module_runtime_findings()
    ledger = load_ledger(args.ledger)
    errors = audit_runtime_state(findings, ledger)
    if errors:
        print("Module runtime state budget failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Move the state behind an application-owned lifecycle, or explicitly "
            "review its owner and retirement condition in the ledger.",
            file=sys.stderr,
        )
        return 1

    print(
        "Module runtime state budget passed: "
        f"{len(findings)} grandfathered mutable runtime instances across "
        f"{len({finding.path for finding in findings.values()})} modules."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
