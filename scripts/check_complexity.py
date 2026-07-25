#!/usr/bin/env python3
"""Fail when production-code complexity grows beyond the checked-in baseline."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "scripts" / "complexity-baseline.json"
DEFAULT_PATHS = (
    "apps/worker/app",
    "apps/api/app",
    "packages/core/lumen_core",
)
DEFAULT_LINE_PATHS = (
    "apps/worker/app",
    "apps/api/app",
    "packages/core/lumen_core",
    "apps/web/src",
    "scripts/update.sh",
    "scripts/update",
)
MAX_COMPLEXITY = 15
MAX_FILE_LINES = 1500
MAX_SHELL_FILE_LINES = 599
MAX_FUNCTION_LINES = 200
MAX_FUNCTION_PARAMETERS = 12
MAX_NESTING_DEPTH = 6
SOURCE_SUFFIXES = frozenset({".py", ".sh", ".ts", ".tsx"})
MESSAGE_RE = re.compile(
    r"`(?P<name>[^`]+)` is too complex "
    r"\((?P<complexity>\d+) > (?P<limit>\d+)\)"
)


@dataclass(frozen=True)
class ComplexityBudget:
    max_complexity: int
    count: int


@dataclass(frozen=True)
class MetricBudget:
    value: int


class _FunctionIdentityVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.counts: dict[str, int] = {}
        self.by_location: dict[tuple[int, str], str] = {}

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        qualified = ".".join([*self.scope, node.name])
        occurrence = self.counts.get(qualified, 0) + 1
        self.counts[qualified] = occurrence
        identity = qualified if occurrence == 1 else f"{qualified}#{occurrence}"
        self.by_location[(node.lineno, node.name)] = identity
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self,
        node: ast.AsyncFunctionDef,
    ) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def function_identities(path: Path) -> dict[tuple[int, str], str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    visitor = _FunctionIdentityVisitor()
    visitor.visit(tree)
    return visitor.by_location


def _function_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    arguments = node.args
    return (
        len(arguments.posonlyargs)
        + len(arguments.args)
        + len(arguments.kwonlyargs)
        + int(arguments.vararg is not None)
        + int(arguments.kwarg is not None)
    )


class _NestingVisitor(ast.NodeVisitor):
    _NESTING_NODES = (
        ast.AsyncFor,
        ast.AsyncWith,
        ast.For,
        ast.If,
        ast.Match,
        ast.Try,
        ast.While,
        ast.With,
    )

    def __init__(self) -> None:
        self.depth = 0
        self.max_depth = 0

    def generic_visit(self, node: ast.AST) -> None:
        nested = isinstance(node, self._NESTING_NODES)
        if nested:
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
        super().generic_visit(node)
        if nested:
            self.depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(  # noqa: N802
        self,
        node: ast.AsyncFunctionDef,
    ) -> None:
        return


class _FunctionMetricVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.counts: dict[str, int] = {}
        self.metrics: dict[str, dict[str, MetricBudget]] = {
            "function_lines": {},
            "function_parameters": {},
            "nesting_depth": {},
        }

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        qualified = ".".join([*self.scope, node.name])
        occurrence = self.counts.get(qualified, 0) + 1
        self.counts[qualified] = occurrence
        identity = qualified if occurrence == 1 else f"{qualified}#{occurrence}"
        key = f"{self.path}::{identity}"
        end_line = int(node.end_lineno or node.lineno)
        function_lines = end_line - int(node.lineno) + 1
        if function_lines > MAX_FUNCTION_LINES:
            self.metrics["function_lines"][key] = MetricBudget(function_lines)
        parameters = _function_parameters(node)
        if parameters > MAX_FUNCTION_PARAMETERS:
            self.metrics["function_parameters"][key] = MetricBudget(parameters)
        nesting = _NestingVisitor()
        for statement in node.body:
            nesting.visit(statement)
        if nesting.max_depth > MAX_NESTING_DEPTH:
            self.metrics["nesting_depth"][key] = MetricBudget(nesting.max_depth)

        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(  # noqa: N802
        self,
        node: ast.AsyncFunctionDef,
    ) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


def collect_python_metrics(
    paths: tuple[str, ...],
) -> dict[str, dict[str, MetricBudget]]:
    metrics: dict[str, dict[str, MetricBudget]] = {
        "function_lines": {},
        "function_parameters": {},
        "nesting_depth": {},
    }
    for raw_path in paths:
        path = ROOT / raw_path
        candidates = [path] if path.is_file() else path.rglob("*.py")
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix != ".py":
                continue
            try:
                tree = ast.parse(
                    candidate.read_text(encoding="utf-8"),
                    filename=str(candidate),
                )
            except (OSError, SyntaxError):
                continue
            relative = candidate.relative_to(ROOT).as_posix()
            visitor = _FunctionMetricVisitor(relative)
            visitor.visit(tree)
            for dimension, findings in visitor.metrics.items():
                metrics[dimension].update(findings)
    return {
        dimension: dict(sorted(findings.items()))
        for dimension, findings in metrics.items()
    }


def collect_oversized_files(paths: tuple[str, ...]) -> dict[str, int]:
    oversized: dict[str, int] = {}
    for raw_path in paths:
        path = ROOT / raw_path
        candidates = [path] if path.is_file() else path.rglob("*")
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix not in SOURCE_SUFFIXES:
                continue
            line_count = len(candidate.read_text(encoding="utf-8").splitlines())
            threshold = (
                MAX_SHELL_FILE_LINES if candidate.suffix == ".sh" else MAX_FILE_LINES
            )
            if line_count > threshold:
                relative = candidate.relative_to(ROOT).as_posix()
                oversized[relative] = line_count
    return dict(sorted(oversized.items()))


def collect_violations(paths: tuple[str, ...]) -> dict[str, ComplexityBudget]:
    command = [
        "ruff",
        "check",
        *paths,
        "--select",
        "C901",
        "--config",
        f"lint.mccabe.max-complexity={MAX_COMPLEXITY}",
        "--output-format",
        "json",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"ruff complexity scan failed with {result.returncode}")

    findings = json.loads(result.stdout or "[]")
    violations: dict[str, ComplexityBudget] = {}
    identity_cache: dict[Path, dict[tuple[int, str], str]] = {}
    for finding in findings:
        match = MESSAGE_RE.fullmatch(str(finding.get("message") or ""))
        if match is None:
            continue
        absolute_filename = Path(str(finding["filename"])).resolve()
        filename = absolute_filename.relative_to(ROOT)
        location = finding.get("location")
        row = int(location.get("row") or 0) if isinstance(location, dict) else 0
        name = match.group("name")
        identities = identity_cache.setdefault(
            absolute_filename,
            function_identities(absolute_filename),
        )
        identity = identities.get((row, name), name)
        key = f"{filename.as_posix()}::{identity}"
        violations[key] = ComplexityBudget(
            max_complexity=int(match.group("complexity")),
            count=1,
        )
    return dict(sorted(violations.items()))


def load_baseline(
    path: Path,
) -> tuple[
    dict[str, ComplexityBudget],
    dict[str, int],
    dict[str, dict[str, MetricBudget]],
]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != 6:
        raise ValueError(
            f"unsupported complexity baseline version: {raw.get('version')}"
        )
    if raw.get("max_complexity") != MAX_COMPLEXITY:
        raise ValueError(
            "complexity baseline threshold does not match "
            f"{MAX_COMPLEXITY}: {raw.get('max_complexity')}"
        )
    complexity = {
        key: ComplexityBudget(
            max_complexity=int(value["max_complexity"]),
            count=int(value["count"]),
        )
        for key, value in raw.get("violations", {}).items()
    }
    if raw.get("max_file_lines") != MAX_FILE_LINES:
        raise ValueError(
            "file-size baseline threshold does not match "
            f"{MAX_FILE_LINES}: {raw.get('max_file_lines')}"
        )
    if raw.get("max_shell_file_lines") != MAX_SHELL_FILE_LINES:
        raise ValueError(
            "shell file-size baseline threshold does not match "
            f"{MAX_SHELL_FILE_LINES}: {raw.get('max_shell_file_lines')}"
        )
    oversized_files = {
        str(key): int(value) for key, value in raw.get("oversized_files", {}).items()
    }
    expected_thresholds = {
        "function_lines": MAX_FUNCTION_LINES,
        "function_parameters": MAX_FUNCTION_PARAMETERS,
        "nesting_depth": MAX_NESTING_DEPTH,
    }
    if raw.get("metric_thresholds") != expected_thresholds:
        raise ValueError(
            "complexity metric thresholds do not match: "
            f"expected {expected_thresholds!r}, "
            f"got {raw.get('metric_thresholds')!r}"
        )
    metrics = {
        str(dimension): {
            str(key): MetricBudget(int(value)) for key, value in findings.items()
        }
        for dimension, findings in raw.get("metrics", {}).items()
    }
    for dimension in expected_thresholds:
        metrics.setdefault(dimension, {})
    return complexity, oversized_files, metrics


def compare_budgets(
    current: dict[str, ComplexityBudget],
    baseline: dict[str, ComplexityBudget],
) -> list[str]:
    errors: list[str] = []
    for key, budget in current.items():
        allowed = baseline.get(key)
        if allowed is None:
            errors.append(
                f"new complexity violation: {key} "
                f"(complexity={budget.max_complexity}, count={budget.count})"
            )
            continue
        if budget.max_complexity > allowed.max_complexity:
            errors.append(
                f"complexity grew: {key} "
                f"{allowed.max_complexity} -> {budget.max_complexity}"
            )
        if budget.count > allowed.count:
            errors.append(
                f"violation count grew: {key} {allowed.count} -> {budget.count}"
            )
    return errors


def compare_file_budgets(
    current: dict[str, int],
    baseline: dict[str, int],
) -> list[str]:
    errors: list[str] = []
    for path, line_count in current.items():
        allowed = baseline.get(path)
        if allowed is None:
            threshold = MAX_SHELL_FILE_LINES if path.endswith(".sh") else MAX_FILE_LINES
            errors.append(
                f"new oversized source file: {path} ({line_count} > {threshold} lines)"
            )
        elif line_count > allowed:
            errors.append(
                f"oversized source file grew: {path} {allowed} -> {line_count}"
            )
    return errors


def compare_metric_budgets(
    current: dict[str, dict[str, MetricBudget]],
    baseline: dict[str, dict[str, MetricBudget]],
) -> list[str]:
    errors: list[str] = []
    for dimension, findings in current.items():
        allowed_findings = baseline.get(dimension, {})
        for key, budget in findings.items():
            allowed = allowed_findings.get(key)
            if allowed is None:
                errors.append(
                    f"new {dimension} violation: {key} (value={budget.value})"
                )
            elif budget.value > allowed.value:
                errors.append(
                    f"{dimension} grew: {key} {allowed.value} -> {budget.value}"
                )
    return errors


def write_baseline(
    path: Path,
    violations: dict[str, ComplexityBudget],
    oversized_files: dict[str, int],
    metrics: dict[str, dict[str, MetricBudget]],
) -> None:
    payload: dict[str, Any] = {
        "version": 6,
        "max_complexity": MAX_COMPLEXITY,
        "max_file_lines": MAX_FILE_LINES,
        "max_shell_file_lines": MAX_SHELL_FILE_LINES,
        "metric_thresholds": {
            "function_lines": MAX_FUNCTION_LINES,
            "function_parameters": MAX_FUNCTION_PARAMETERS,
            "nesting_depth": MAX_NESTING_DEPTH,
        },
        "metrics": {
            dimension: {key: budget.value for key, budget in findings.items()}
            for dimension, findings in metrics.items()
        },
        "oversized_files": oversized_files,
        "violations": {
            key: {
                "max_complexity": value.max_complexity,
                "count": value.count,
            }
            for key, value in violations.items()
        },
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="replace the baseline with the current production-code violations",
    )
    parser.add_argument("paths", nargs="*", default=list(DEFAULT_PATHS))
    args = parser.parse_args()

    scan_paths = tuple(args.paths)
    current = collect_violations(scan_paths)
    oversized_files = collect_oversized_files(DEFAULT_LINE_PATHS)
    metrics = collect_python_metrics(scan_paths)
    if args.update_baseline:
        write_baseline(args.baseline, current, oversized_files, metrics)
        metric_count = sum(len(findings) for findings in metrics.values())
        print(
            f"updated {args.baseline.relative_to(ROOT)} "
            f"({len(current)} complexity entries, "
            f"{len(oversized_files)} oversized files, "
            f"{metric_count} multi-dimensional findings)"
        )
        return 0

    baseline, line_baseline, metric_baseline = load_baseline(args.baseline)
    errors = compare_budgets(current, baseline)
    errors.extend(compare_file_budgets(oversized_files, line_baseline))
    errors.extend(compare_metric_budgets(metrics, metric_baseline))
    if errors:
        print("Complexity budget failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Refactor the new growth. Only intentional baseline reductions should "
            "use --update-baseline.",
            file=sys.stderr,
        )
        return 1

    improved = len(set(baseline) - set(current))
    metric_count = sum(len(findings) for findings in metrics.values())
    metric_removed = sum(
        len(set(metric_baseline.get(dimension, {})) - set(findings))
        for dimension, findings in metrics.items()
    )
    print(
        "Complexity budget passed: "
        f"{len(current)} grandfathered violations, {improved} removed; "
        f"{len(oversized_files)} grandfathered oversized files; "
        f"{metric_count} multi-dimensional findings, {metric_removed} removed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
