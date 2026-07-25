#!/usr/bin/env python3
"""Inventory runtime coupling and compatibility-facade public APIs."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "docs" / "refactors" / "runtime-coupling-inventory.json"
DEFAULT_RETIREMENT_LEDGER = (
    ROOT / "docs" / "refactors" / "compatibility-facade-retirement.json"
)
DEFAULT_SOURCE_ROOTS = (
    ROOT / "packages" / "core" / "lumen_core",
    ROOT / "apps" / "api" / "app",
    ROOT / "apps" / "worker" / "app",
    ROOT / "apps" / "tgbot" / "app",
    ROOT / "image-job",
    ROOT / "scripts" / "architecture_audit.py",
    ROOT / "scripts" / "check_architecture.py",
    ROOT / "scripts" / "check_complexity.py",
)
_DYNAMIC_IMPORT_CALLS = frozenset(
    {
        "__import__",
        "import_module",
        "importlib.import_module",
        "importlib.util.spec_from_file_location",
        "spec_from_file_location",
    }
)
_SYS_MODULES_METHODS = frozenset({"clear", "pop", "popitem", "setdefault", "update"})
_MUTABLE_FACTORIES = frozenset(
    {
        "collections.defaultdict",
        "defaultdict",
        "dict",
        "list",
        "set",
        "weakref.WeakKeyDictionary",
        "weakref.WeakSet",
    }
)


@dataclass(frozen=True, order=True)
class RuntimeCouplingFinding:
    category: str
    path: str
    line: int
    symbol: str
    target: str = ""

    @property
    def key(self) -> str:
        return f"{self.category}|{self.path}|{self.symbol}|{self.target}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "line": self.line,
            "path": self.path,
            "symbol": self.symbol,
            "target": self.target,
        }


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _literal_string(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "<dynamic>"


def _target_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [name for element in node.elts for name in _target_names(element)]
    return []


def _is_sys_modules(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr == "modules"
    )


def _is_sys_modules_subscript(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and _is_sys_modules(node.value)


def _mutable_assignment(node: ast.AST) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.ListComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call):
        return _call_name(node.func) in _MUTABLE_FACTORIES
    return False


def _static_all(tree: ast.Module) -> list[str] | None:
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
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return None
        exports: list[str] = []
        for element in value.elts:
            if not (
                isinstance(element, ast.Constant) and isinstance(element.value, str)
            ):
                return None
            exports.append(element.value)
        return sorted(set(exports))
    return None


class RuntimeCouplingVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.findings: list[RuntimeCouplingFinding] = []
        self._scope_depth = 0

    def _add(
        self,
        category: str,
        node: ast.AST,
        symbol: str,
        target: str = "",
    ) -> None:
        self.findings.append(
            RuntimeCouplingFinding(
                category=category,
                path=self.relative_path,
                line=int(getattr(node, "lineno", 0)),
                symbol=symbol,
                target=target,
            )
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = ("." * node.level) + (node.module or "")
        for alias in node.names:
            if alias.name.startswith("_") and not alias.name.startswith("__"):
                self._add(
                    "private-cross-module-import",
                    node,
                    alias.asname or alias.name,
                    f"{module}:{alias.name}",
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node.func)
        if name in _DYNAMIC_IMPORT_CALLS:
            target = _literal_string(node.args[0] if node.args else None)
            self._add("dynamic-import", node, name, target)
        if (
            isinstance(node.func, ast.Attribute)
            and _is_sys_modules(node.func.value)
            and node.func.attr in _SYS_MODULES_METHODS
        ):
            target = _literal_string(node.args[0] if node.args else None)
            self._add(
                "sys-modules-mutation",
                node,
                f"sys.modules.{node.func.attr}",
                target,
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            if _is_sys_modules_subscript(target):
                self._add(
                    "sys-modules-mutation",
                    node,
                    "sys.modules[]=",
                    _literal_string(target.slice),
                )
        if self._scope_depth == 0 and _mutable_assignment(node.value):
            for target in node.targets:
                for name in _target_names(target):
                    if name == "__all__":
                        continue
                    self._add("module-mutable-state", node, name)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if _is_sys_modules_subscript(node.target):
            self._add(
                "sys-modules-mutation",
                node,
                "sys.modules[]=",
                _literal_string(node.target.slice),
            )
        if (
            self._scope_depth == 0
            and node.value is not None
            and _mutable_assignment(node.value)
        ):
            for name in _target_names(node.target):
                if name == "__all__":
                    continue
                self._add("module-mutable-state", node, name)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802
        for target in node.targets:
            if _is_sys_modules_subscript(target):
                self._add(
                    "sys-modules-mutation",
                    node,
                    "del sys.modules[]",
                    _literal_string(target.slice),
                )
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        for name in node.names:
            self._add("global-statement", node, name)

    def _visit_scoped(self, node: ast.AST) -> None:
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_scoped(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self,
        node: ast.AsyncFunctionDef,
    ) -> None:
        self._visit_scoped(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_scoped(node)


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


def collect_runtime_findings(
    roots: Iterable[Path] = DEFAULT_SOURCE_ROOTS,
) -> dict[str, RuntimeCouplingFinding]:
    findings: dict[str, RuntimeCouplingFinding] = {}
    for path in iter_python_sources(roots):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = RuntimeCouplingVisitor(relative)
        visitor.visit(tree)
        for finding in visitor.findings:
            findings[finding.key] = finding
    return dict(sorted(findings.items()))


def load_retirement_ledger(path: Path = DEFAULT_RETIREMENT_LEDGER) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != 1 or not isinstance(raw.get("facades"), list):
        raise ValueError("unsupported compatibility facade retirement ledger")
    required = {"path", "owner", "status", "retirement_condition"}
    for entry in raw["facades"]:
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise ValueError("invalid compatibility facade retirement entry")
    return raw


def collect_public_api_manifest(
    ledger_path: Path = DEFAULT_RETIREMENT_LEDGER,
) -> dict[str, list[str]]:
    ledger = load_retirement_ledger(ledger_path)
    manifest: dict[str, list[str]] = {}
    for entry in ledger["facades"]:
        relative = str(entry["path"])
        path = ROOT / relative
        if not path.is_file():
            raise ValueError(f"compatibility facade is missing: {relative}")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        exports = _static_all(tree)
        if exports is None:
            declared = entry.get("public_api")
            if not isinstance(declared, list) or not all(
                isinstance(item, str) for item in declared
            ):
                raise ValueError(
                    f"{relative} needs static __all__ or ledger public_api"
                )
            exports = sorted(set(declared))
        manifest[relative] = exports
    return dict(sorted(manifest.items()))


def load_inventory(
    path: Path = DEFAULT_INVENTORY,
) -> tuple[set[str], dict[str, list[str]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        raw.get("version") != 1
        or not isinstance(raw.get("findings"), list)
        or not isinstance(raw.get("public_api"), dict)
    ):
        raise ValueError("unsupported runtime coupling inventory")
    findings = {
        RuntimeCouplingFinding(
            category=str(item["category"]),
            path=str(item["path"]),
            line=int(item["line"]),
            symbol=str(item["symbol"]),
            target=str(item.get("target") or ""),
        ).key
        for item in raw["findings"]
    }
    public_api = {
        str(path): sorted(str(name) for name in exports)
        for path, exports in raw["public_api"].items()
    }
    return findings, public_api


def compare_inventory(
    current: set[str],
    baseline: set[str],
    current_public_api: dict[str, list[str]],
    baseline_public_api: dict[str, list[str]],
) -> list[str]:
    errors = [f"new runtime coupling: {key}" for key in sorted(current - baseline)]
    for path, expected in baseline_public_api.items():
        actual = current_public_api.get(path)
        if actual is None:
            errors.append(f"public API facade removed from manifest: {path}")
        elif actual != expected:
            errors.append(
                f"public API changed: {path} expected={expected!r} actual={actual!r}"
            )
    for path in sorted(set(current_public_api) - set(baseline_public_api)):
        errors.append(f"new public API facade lacks baseline review: {path}")
    return errors


def write_inventory(
    path: Path,
    findings: dict[str, RuntimeCouplingFinding],
    public_api: dict[str, list[str]],
) -> None:
    counts: dict[str, int] = {}
    for finding in findings.values():
        counts[finding.category] = counts.get(finding.category, 0) + 1
    payload = {
        "category_counts": dict(sorted(counts.items())),
        "findings": [finding.as_dict() for finding in findings.values()],
        "public_api": public_api,
        "version": 1,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--retirement-ledger",
        type=Path,
        default=DEFAULT_RETIREMENT_LEDGER,
    )
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    findings = collect_runtime_findings()
    public_api = collect_public_api_manifest(args.retirement_ledger)
    if args.update_baseline:
        write_inventory(args.inventory, findings, public_api)
        print(
            f"updated {args.inventory.relative_to(ROOT)} "
            f"({len(findings)} runtime findings, "
            f"{len(public_api)} public API facades)"
        )
        return 0

    baseline, baseline_public_api = load_inventory(args.inventory)
    errors = compare_inventory(
        set(findings),
        baseline,
        public_api,
        baseline_public_api,
    )
    if errors:
        print("Runtime coupling budget failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Remove the new coupling or explicitly review and regenerate the "
            "inventory; existing entries may only shrink.",
            file=sys.stderr,
        )
        return 1

    removed = len(baseline - set(findings))
    print(
        "Runtime coupling budget passed: "
        f"{len(findings)} grandfathered findings, {removed} removed; "
        f"{len(public_api)} facade APIs verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
