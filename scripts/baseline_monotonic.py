#!/usr/bin/env python3
"""Reject governance baseline growth relative to the merge-base commit."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
JSON_PATHS = (
    "scripts/architecture-baseline.json",
    "scripts/complexity-baseline.json",
    "docs/refactors/compatibility-facade-retirement.json",
    "docs/refactors/runtime-coupling-inventory.json",
    "docs/refactors/module-runtime-state-ledger.json",
)

GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )


def resolve_merge_base(
    *,
    root: Path = ROOT,
    base_ref: str | None = None,
    runner: GitRunner = _run_git,
) -> str:
    candidate = (
        base_ref
        or os.environ.get("GOVERNANCE_BASE_SHA")
        or (
            f"origin/{os.environ['GITHUB_BASE_REF']}"
            if os.environ.get("GITHUB_BASE_REF")
            else "origin/main"
        )
    )
    result = runner(("merge-base", "HEAD", candidate), root)
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip() or result.stdout.strip() or result.returncode
        raise RuntimeError(f"cannot resolve merge-base for {candidate}: {detail}")
    return result.stdout.strip()


def read_base_text(
    path: str,
    *,
    merge_base: str,
    root: Path = ROOT,
    runner: GitRunner = _run_git,
    missing_ok: bool = False,
) -> str | None:
    result = runner(("show", f"{merge_base}:{path}"), root)
    if result.returncode == 0:
        return result.stdout
    if missing_ok:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or result.returncode
    raise RuntimeError(f"cannot read {path} at {merge_base}: {detail}")


def _finding_key(item: dict[str, Any]) -> str:
    return "|".join(
        str(item.get(field) or "")
        for field in ("category", "path", "symbol", "target")
    )


def compare_architecture(current: dict[str, Any], base: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("violations", "cycles"):
        current_values = {str(value) for value in current.get(field, [])}
        base_values = {str(value) for value in base.get(field, [])}
        for value in sorted(current_values - base_values):
            errors.append(f"architecture baseline grew: {field}:{value}")
    return errors


def _numeric_budget(
    current: dict[str, Any],
    base: dict[str, Any],
    *,
    label: str,
) -> list[str]:
    errors: list[str] = []
    for key, value in current.items():
        if key not in base:
            errors.append(f"{label} baseline added entry: {key}")
            continue
        if int(value) > int(base[key]):
            errors.append(f"{label} baseline grew: {key} {base[key]} -> {value}")
    return errors


def compare_complexity(current: dict[str, Any], base: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in (
        "max_complexity",
        "max_file_lines",
        "max_shell_file_lines",
    ):
        if int(current.get(field, 0)) > int(base.get(field, 0)):
            errors.append(
                f"complexity threshold grew: {field} "
                f"{base.get(field)} -> {current.get(field)}"
            )
    errors.extend(
        _numeric_budget(
            current.get("metric_thresholds", {}),
            base.get("metric_thresholds", {}),
            label="metric threshold",
        )
    )
    errors.extend(
        _numeric_budget(
            current.get("oversized_files", {}),
            base.get("oversized_files", {}),
            label="oversized file",
        )
    )
    for key, value in current.get("violations", {}).items():
        allowed = base.get("violations", {}).get(key)
        if not isinstance(allowed, dict):
            errors.append(f"complexity baseline added entry: {key}")
            continue
        for field in ("max_complexity", "count"):
            if int(value.get(field, 0)) > int(allowed.get(field, 0)):
                errors.append(
                    f"complexity baseline grew: {key}:{field} "
                    f"{allowed.get(field)} -> {value.get(field)}"
                )
    for dimension, findings in current.get("metrics", {}).items():
        errors.extend(
            _numeric_budget(
                findings,
                base.get("metrics", {}).get(dimension, {}),
                label=dimension,
            )
        )
    return errors


def compare_runtime_inventory(
    current: dict[str, Any],
    base: dict[str, Any],
    *,
    merge_base: str | None = None,
    root: Path = ROOT,
    runner: GitRunner = _run_git,
) -> list[str]:
    current_findings = {
        _finding_key(item)
        for item in current.get("findings", [])
        if isinstance(item, dict)
    }
    base_findings = {
        _finding_key(item)
        for item in base.get("findings", [])
        if isinstance(item, dict)
    }
    errors = [
        f"runtime coupling baseline grew: {key}"
        for key in sorted(current_findings - base_findings)
    ]
    base_api = base.get("public_api", {})
    for path, exports in current.get("public_api", {}).items():
        if path not in base_api:
            source = (
                read_base_text(
                    path,
                    merge_base=merge_base,
                    root=root,
                    runner=runner,
                    missing_ok=True,
                )
                if merge_base is not None
                else None
            )
            if source is not None and set(exports).issubset(
                _static_all_symbols(source)
            ):
                continue
            errors.append(f"facade baseline added path: {path}")
            continue
        added = set(exports) - set(base_api[path])
        for name in sorted(added):
            errors.append(f"facade public API grew: {path}:{name}")
    return errors


def _static_all_symbols(source: str) -> set[str]:
    tree = ast.parse(source)
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
        if not isinstance(statement.value, (ast.List, ast.Tuple)):
            return set()
        return {
            str(element.value)
            for element in statement.value.elts
            if isinstance(element, ast.Constant)
            and isinstance(element.value, str)
        }
    return set()


def compare_facade_ledger(
    current: dict[str, Any],
    base: dict[str, Any],
    *,
    merge_base: str,
    root: Path = ROOT,
    runner: GitRunner = _run_git,
) -> list[str]:
    errors: list[str] = []
    base_entries = {
        str(item["path"]): item
        for item in base.get("facades", [])
        if isinstance(item, dict) and item.get("path")
    }
    for entry in current.get("facades", []):
        if not isinstance(entry, dict):
            errors.append("facade ledger contains invalid entry")
            continue
        path = str(entry.get("path") or "")
        allowed = base_entries.get(path)
        if allowed is not None:
            if (
                "caller_count" in allowed
                and int(entry.get("caller_count", 0))
                > int(allowed.get("caller_count", 0))
            ):
                errors.append(
                    f"facade caller budget grew: {path} "
                    f"{allowed.get('caller_count')} -> {entry.get('caller_count')}"
                )
            continue
        source = read_base_text(
            path,
            merge_base=merge_base,
            root=root,
            runner=runner,
            missing_ok=True,
        )
        if source is None:
            errors.append(f"facade ledger added new source: {path}")
    return errors


def _module_symbols(source: str) -> set[str]:
    tree = ast.parse(source)
    symbols: set[str] = set()
    for statement in tree.body:
        targets: list[ast.AST] = []
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        for target in targets:
            if isinstance(target, ast.Name):
                symbols.add(target.id)
    return symbols


def compare_runtime_ledger(
    current: dict[str, Any],
    base: dict[str, Any],
    *,
    merge_base: str,
    root: Path = ROOT,
    runner: GitRunner = _run_git,
) -> list[str]:
    errors: list[str] = []
    base_modules = {
        str(item["path"]): item
        for item in base.get("modules", [])
        if isinstance(item, dict) and item.get("path")
    }
    added_instances = 0
    for entry in current.get("modules", []):
        if not isinstance(entry, dict):
            errors.append("runtime ledger contains invalid entry")
            continue
        path = str(entry.get("path") or "")
        symbols = {str(value) for value in entry.get("symbols", [])}
        allowed = base_modules.get(path)
        if allowed is not None:
            if int(entry.get("max_instances", 0)) > int(
                allowed.get("max_instances", 0)
            ):
                errors.append(
                    f"runtime ledger budget grew: {path} "
                    f"{allowed.get('max_instances')} -> {entry.get('max_instances')}"
                )
            for symbol in sorted(symbols - set(allowed.get("symbols", []))):
                errors.append(f"runtime ledger added symbol: {path}|{symbol}")
            continue

        source = read_base_text(
            path,
            merge_base=merge_base,
            root=root,
            runner=runner,
            missing_ok=True,
        )
        if source is None:
            errors.append(f"runtime ledger added new module state: {path}")
            continue
        existing_symbols = _module_symbols(source)
        missing_symbols = symbols - existing_symbols
        if missing_symbols:
            for symbol in sorted(missing_symbols):
                errors.append(f"runtime ledger added new symbol: {path}|{symbol}")
            continue
        added_instances += int(entry.get("max_instances", 0))

    allowed_total = int(base.get("max_total", 0)) + added_instances
    if int(current.get("max_total", 0)) > allowed_total:
        errors.append(
            "runtime ledger total grew beyond pre-existing hidden state: "
            f"allowed={allowed_total} current={current.get('max_total')}"
        )
    return errors


def audit_baselines(
    *,
    root: Path = ROOT,
    base_ref: str | None = None,
    runner: GitRunner = _run_git,
) -> tuple[str, list[str]]:
    merge_base = resolve_merge_base(root=root, base_ref=base_ref, runner=runner)
    current = {
        path: json.loads((root / path).read_text(encoding="utf-8"))
        for path in JSON_PATHS
    }
    base = {
        path: json.loads(
            read_base_text(path, merge_base=merge_base, root=root, runner=runner)
            or "{}"
        )
        for path in JSON_PATHS
    }
    errors = compare_architecture(
        current["scripts/architecture-baseline.json"],
        base["scripts/architecture-baseline.json"],
    )
    errors.extend(
        compare_complexity(
            current["scripts/complexity-baseline.json"],
            base["scripts/complexity-baseline.json"],
        )
    )
    errors.extend(
        compare_runtime_inventory(
            current["docs/refactors/runtime-coupling-inventory.json"],
            base["docs/refactors/runtime-coupling-inventory.json"],
            merge_base=merge_base,
            root=root,
            runner=runner,
        )
    )
    errors.extend(
        compare_facade_ledger(
            current["docs/refactors/compatibility-facade-retirement.json"],
            base["docs/refactors/compatibility-facade-retirement.json"],
            merge_base=merge_base,
            root=root,
            runner=runner,
        )
    )
    errors.extend(
        compare_runtime_ledger(
            current["docs/refactors/module-runtime-state-ledger.json"],
            base["docs/refactors/module-runtime-state-ledger.json"],
            merge_base=merge_base,
            root=root,
            runner=runner,
        )
    )
    return merge_base, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref")
    args = parser.parse_args(argv)
    try:
        merge_base, errors = audit_baselines(base_ref=args.base_ref)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
        print(f"Baseline monotonic audit failed closed: {exc}", file=sys.stderr)
        return 1
    if errors:
        print(f"Baseline monotonic audit failed against {merge_base}:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Baseline monotonic audit passed against merge-base {merge_base}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
