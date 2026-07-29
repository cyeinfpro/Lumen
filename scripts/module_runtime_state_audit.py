#!/usr/bin/env python3
"""Ratchet lifecycle- and concurrency-sensitive module runtime state.

The main architecture audit catches literal list/dict/set state and global
statements. This gate adds narrowly scoped detection for mutable runtime
owners: stateful dataclasses, lifecycle-managed ordinary classes, locks,
semaphores, clients, cache decorators, and equivalent TypeScript singletons.
Read-only constants and declarative framework handles are intentionally outside
this gate.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "docs" / "refactors" / "module-runtime-state-ledger.json"
DEFAULT_SOURCE_ROOTS = (
    ROOT / "apps" / "api" / "app",
    ROOT / "apps" / "worker" / "app",
    ROOT / "apps" / "web" / "src",
    ROOT / "apps" / "tgbot" / "app",
    ROOT / "image-job" / "image_job",
    ROOT / "packages" / "core" / "lumen_core",
)
_CACHE_DECORATORS = frozenset({"functools.cache", "functools.lru_cache"})
_LIFECYCLE_METHODS = frozenset(
    {
        "aclose",
        "close",
        "connect",
        "disconnect",
        "clear",
        "reset",
        "shutdown",
        "start",
        "stop",
    }
)
_PYTHON_MUTABLE_FACTORY_NAMES = frozenset(
    {
        "defaultdict",
        "dict",
        "list",
        "set",
        "WeakKeyDictionary",
        "WeakSet",
    }
)
_PYTHON_RUNTIME_TYPE_NAMES = frozenset(
    {
        "AsyncClient",
        "BoundedSemaphore",
        "Client",
        "HttpClient",
        "Lock",
        "RLock",
        "RedisClient",
        "Semaphore",
        "Thread",
        "WeakKeyDictionary",
        "WeakSet",
    }
)
_TYPESCRIPT_RUNTIME_TYPE_NAMES = (
    "AbortController",
    "AsyncClient",
    "BroadcastChannel",
    "EventSource",
    "HttpClient",
    "Lock",
    "MessageChannel",
    "Mutex",
    "QueryClient",
    "RedisClient",
    "Semaphore",
    "SharedWorker",
    "WebSocket",
    "Worker",
)
_TYPESCRIPT_SUFFIXES = frozenset({".cts", ".mts", ".ts", ".tsx"})
_TYPESCRIPT_DECLARATION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:declare\s+)?"
    r"(?P<kind>const|let|var)\s+"
    r"(?P<symbol>[A-Za-z_$][A-Za-z0-9_$]*)\b"
    r"(?P<tail>.*)$"
)
_TYPESCRIPT_NEW_RE = re.compile(
    r"^\s*new\s+"
    r"(?P<constructor>[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*)"
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


def _local_lifecycle_classes(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef):
            continue
        if any(
            isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            and member.name in _LIFECYCLE_METHODS
            for member in statement.body
        ):
            names.add(statement.name)
    return names


def _self_attribute_targets(node: ast.AST) -> list[str]:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return [node.attr]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [
            name
            for element in node.elts
            for name in _self_attribute_targets(element)
        ]
    return []


def _owns_python_runtime_state(
    value: ast.AST,
    *,
    aliases: dict[str, str],
) -> bool:
    if isinstance(
        value,
        (
            ast.Dict,
            ast.DictComp,
            ast.List,
            ast.ListComp,
            ast.Set,
            ast.SetComp,
        ),
    ):
        return True
    if not isinstance(value, ast.Call):
        return False
    canonical_name = _resolve_imported_name(_call_name(value.func), aliases)
    final_name = _runtime_type_name(canonical_name)
    return (
        final_name in _PYTHON_MUTABLE_FACTORY_NAMES
        or _is_python_runtime_type(canonical_name)
    )


def _local_stateful_classes(
    tree: ast.Module,
    *,
    aliases: dict[str, str],
) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef):
            continue
        initializer = next(
            (
                member
                for member in statement.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name == "__init__"
            ),
            None,
        )
        if initializer is None:
            continue
        for member in ast.walk(initializer):
            value: ast.AST | None = None
            targets: list[ast.AST] = []
            if isinstance(member, ast.Assign):
                value = member.value
                targets = list(member.targets)
            elif isinstance(member, ast.AnnAssign):
                value = member.value
                targets = [member.target]
            if value is None or not any(
                _self_attribute_targets(target) for target in targets
            ):
                continue
            if _owns_python_runtime_state(value, aliases=aliases):
                names.add(statement.name)
                break
    return names


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                aliases[local_name] = alias.name if alias.asname else local_name
        elif isinstance(statement, ast.ImportFrom):
            module = statement.module or ""
            for alias in statement.names:
                if alias.name == "*":
                    continue
                aliases[alias.asname or alias.name] = ".".join(
                    part for part in (module, alias.name) if part
                )
    return aliases


def _resolve_imported_name(name: str, aliases: dict[str, str]) -> str:
    prefix, separator, suffix = name.partition(".")
    imported = aliases.get(prefix)
    if imported is None:
        return name
    return f"{imported}.{suffix}" if separator else imported


def _runtime_type_name(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _is_python_runtime_type(name: str) -> bool:
    final_name = _runtime_type_name(name)
    return (
        final_name in _PYTHON_RUNTIME_TYPE_NAMES
        or final_name.endswith("Client")
        or final_name.endswith("Semaphore")
        or final_name.endswith("Lock")
    )


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


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


def _is_typescript_production_source(path: Path) -> bool:
    if any(part in {"__tests__", ".next", "node_modules"} for part in path.parts):
        return False
    name = path.name
    return not (
        ".test." in name
        or ".spec." in name
        or name.endswith((".d.ts", ".d.mts", ".d.cts"))
    )


def iter_typescript_sources(
    roots: Iterable[Path] = DEFAULT_SOURCE_ROOTS,
) -> Iterable[Path]:
    for root in roots:
        if root.is_file() and root.suffix in _TYPESCRIPT_SUFFIXES:
            if _is_typescript_production_source(root):
                yield root
            continue
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and path.suffix in _TYPESCRIPT_SUFFIXES
                and _is_typescript_production_source(path)
            ):
                yield path


def _add_finding(
    findings: dict[str, ModuleRuntimeFinding],
    *,
    path: str,
    line: int,
    symbol: str,
    class_name: str,
) -> None:
    finding = ModuleRuntimeFinding(
        path=path,
        line=line,
        symbol=symbol,
        class_name=class_name,
    )
    findings[finding.key] = finding


def _collect_python_findings(
    path: Path,
    *,
    root: Path,
) -> list[ModuleRuntimeFinding]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dataclass_names = _mutable_local_dataclasses(tree)
    lifecycle_names = _local_lifecycle_classes(tree)
    aliases = _import_aliases(tree)
    stateful_names = _local_stateful_classes(tree, aliases=aliases)
    relative = _relative_path(path, root)
    findings: dict[str, ModuleRuntimeFinding] = {}

    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in statement.decorator_list:
                target = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                decorator_name = _resolve_imported_name(_call_name(target), aliases)
                if decorator_name not in _CACHE_DECORATORS:
                    continue
                _add_finding(
                    findings,
                    path=relative,
                    line=int(getattr(decorator, "lineno", statement.lineno)),
                    symbol=statement.name,
                    class_name=decorator_name,
                )

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
        raw_name = _call_name(value.func)
        canonical_name = _resolve_imported_name(raw_name, aliases)
        if (
            raw_name in dataclass_names
            or raw_name in lifecycle_names
            or raw_name in stateful_names
        ):
            class_name = raw_name
        elif _is_python_runtime_type(canonical_name):
            class_name = canonical_name
        else:
            continue
        for target in targets:
            for symbol in _target_names(target):
                _add_finding(
                    findings,
                    path=relative,
                    line=int(getattr(statement, "lineno", 0)),
                    symbol=symbol,
                    class_name=class_name,
                )
    return sorted(findings.values())


def _sanitize_typescript(source: str) -> str:
    output: list[str] = []
    state = "code"
    index = 0
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                output.extend((" ", " "))
                state = "line-comment"
                index += 2
                continue
            if char == "/" and next_char == "*":
                output.extend((" ", " "))
                state = "block-comment"
                index += 2
                continue
            if char in {"'", '"', "`"}:
                output.append(" ")
                state = char
            else:
                output.append(char)
            index += 1
            continue
        if char == "\n":
            output.append("\n")
            if state == "line-comment":
                state = "code"
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                state = "code"
                index += 2
            else:
                output.append(" ")
                index += 1
            continue
        if char == "\\":
            output.append(" ")
            if next_char:
                output.append("\n" if next_char == "\n" else " ")
                index += 2
            else:
                index += 1
            continue
        output.append(" ")
        if char == state:
            state = "code"
        index += 1
    return "".join(output)


def _typescript_assignment_index(tail: str) -> int:
    for index, char in enumerate(tail):
        if char != "=":
            continue
        previous = tail[index - 1] if index else ""
        next_char = tail[index + 1] if index + 1 < len(tail) else ""
        if previous in {"!", "<", "=", ">"} or next_char in {"=", ">"}:
            continue
        return index
    return -1


def _typescript_runtime_type(kind: str, tail: str) -> str:
    assignment_index = _typescript_assignment_index(tail)
    annotation = tail[:assignment_index] if assignment_index >= 0 else tail
    initializer = tail[assignment_index + 1 :] if assignment_index >= 0 else ""
    new_match = _TYPESCRIPT_NEW_RE.match(initializer)
    if new_match is not None:
        constructor = new_match.group("constructor")
        if _runtime_type_name(constructor) in _TYPESCRIPT_RUNTIME_TYPE_NAMES:
            return f"typescript:new {constructor}"
    if kind not in {"let", "var"}:
        return ""
    for type_name in _TYPESCRIPT_RUNTIME_TYPE_NAMES:
        if re.search(rf"\b{re.escape(type_name)}\b", annotation):
            return f"typescript:mutable {type_name}"
    return ""


def _collect_typescript_findings(
    path: Path,
    *,
    root: Path,
) -> list[ModuleRuntimeFinding]:
    sanitized = _sanitize_typescript(path.read_text(encoding="utf-8"))
    relative = _relative_path(path, root)
    findings: dict[str, ModuleRuntimeFinding] = {}
    brace_depth = 0
    for line_number, line in enumerate(sanitized.splitlines(), start=1):
        if brace_depth == 0:
            declaration = _TYPESCRIPT_DECLARATION_RE.match(line)
            if declaration is not None:
                runtime_type = _typescript_runtime_type(
                    declaration.group("kind"),
                    declaration.group("tail"),
                )
                if runtime_type:
                    _add_finding(
                        findings,
                        path=relative,
                        line=line_number,
                        symbol=declaration.group("symbol"),
                        class_name=runtime_type,
                    )
        brace_depth += line.count("{") - line.count("}")
        brace_depth = max(0, brace_depth)
    return sorted(findings.values())


def collect_module_runtime_findings(
    roots: Iterable[Path] = DEFAULT_SOURCE_ROOTS,
    *,
    root: Path = ROOT,
) -> dict[str, ModuleRuntimeFinding]:
    findings: dict[str, ModuleRuntimeFinding] = {}
    for path in iter_python_sources(roots):
        for finding in _collect_python_findings(path, root=root):
            findings[finding.key] = finding
    for path in iter_typescript_sources(roots):
        for finding in _collect_typescript_findings(path, root=root):
            findings[finding.key] = finding
    return {finding.key: finding for finding in sorted(findings.values())}


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
    elif len(findings) < ledger["max_total"]:
        errors.append(
            f"module runtime state total baseline is stale: "
            f"current={len(findings)} budget={ledger['max_total']}"
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
        elif len(path_findings) < entry["max_instances"]:
            errors.append(
                f"module runtime state budget is stale: {path} "
                f"current={len(path_findings)} budget={entry['max_instances']}"
            )
        expected_symbols = set(entry.get("symbols") or [])
        if expected_symbols:
            actual_symbols = {finding.symbol for finding in path_findings}
            for symbol in sorted(actual_symbols - expected_symbols):
                errors.append(f"unexpected module runtime symbol: {path}|{symbol}")
            for symbol in sorted(expected_symbols - actual_symbols):
                errors.append(f"stale module runtime symbol: {path}|{symbol}")
    for path in sorted(set(allowed) - set(grouped)):
        errors.append(f"stale module runtime state entry: {path}")
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
