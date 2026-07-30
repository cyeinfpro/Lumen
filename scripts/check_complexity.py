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
    "image-job/image_job",
)
DEFAULT_LINE_PATHS = (
    "apps/worker/app",
    "apps/api/app",
    "packages/core/lumen_core",
    "image-job/image_job",
    "apps/web/src",
    "scripts/update.sh",
    "scripts/update",
)
DEFAULT_ROLE_LINE_PATHS = (
    "apps/worker/app",
    "apps/api/app",
    "packages/core/lumen_core",
    "image-job/image_job",
    "apps/web/src",
    "scripts/install.sh",
    "scripts/lumenctl.sh",
    "scripts/lib.sh",
)
MAX_COMPLEXITY = 15
MAX_FILE_LINES = 1500
MAX_SHELL_FILE_LINES = 400
MAX_ROUTE_CONTROLLER_LINES = 800
MAX_SERVICE_ADAPTER_LINES = 1000
MAX_REACT_COMPONENT_LINES = 800
MAX_REACT_CONTROLLER_LINES = 600
MAX_SHELL_ENTRYPOINT_LINES = 600
MAX_GENERAL_MODULE_LINES = 1000
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


@dataclass(frozen=True)
class RoleFileBudget:
    role: str
    line_count: int
    limit: int


class ComplexityScanError(RuntimeError):
    def __init__(self, failures: dict[str, str]) -> None:
        ordered = dict(sorted(failures.items()))
        self.failures = ordered
        self.unscanned_files = tuple(ordered)
        details = "\n".join(f"- {path}: {reason}" for path, reason in ordered.items())
        super().__init__(f"unscanned files:\n{details}")


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _scan_failure(path: Path, error: BaseException) -> ComplexityScanError:
    return ComplexityScanError(
        {_display_path(path): f"{type(error).__name__}: {error}"}
    )


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
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise _scan_failure(path, exc) from exc
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
    failures: dict[str, str] = {}
    for raw_path in paths:
        path = ROOT / raw_path
        try:
            candidates = [path] if path.is_file() else list(path.rglob("*.py"))
        except OSError as exc:
            failures.update(_scan_failure(path, exc).failures)
            continue
        for candidate in candidates:
            try:
                is_file = candidate.is_file()
            except OSError as exc:
                failures.update(_scan_failure(candidate, exc).failures)
                continue
            if not is_file or candidate.suffix != ".py":
                continue
            try:
                tree = ast.parse(
                    candidate.read_text(encoding="utf-8"),
                    filename=str(candidate),
                )
            except (OSError, SyntaxError, UnicodeError) as exc:
                failures.update(_scan_failure(candidate, exc).failures)
                continue
            relative = _display_path(candidate)
            visitor = _FunctionMetricVisitor(relative)
            visitor.visit(tree)
            for dimension, findings in visitor.metrics.items():
                metrics[dimension].update(findings)
    if failures:
        raise ComplexityScanError(failures)
    return {
        dimension: dict(sorted(findings.items()))
        for dimension, findings in metrics.items()
    }


def collect_oversized_files(paths: tuple[str, ...]) -> dict[str, int]:
    oversized: dict[str, int] = {}
    failures: dict[str, str] = {}
    for raw_path in paths:
        path = ROOT / raw_path
        try:
            candidates = [path] if path.is_file() else list(path.rglob("*"))
        except OSError as exc:
            failures.update(_scan_failure(path, exc).failures)
            continue
        for candidate in candidates:
            try:
                is_file = candidate.is_file()
            except OSError as exc:
                failures.update(_scan_failure(candidate, exc).failures)
                continue
            if not is_file or candidate.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                failures.update(_scan_failure(candidate, exc).failures)
                continue
            line_count = len(source.splitlines())
            threshold = (
                MAX_SHELL_FILE_LINES if candidate.suffix == ".sh" else MAX_FILE_LINES
            )
            if line_count > threshold:
                relative = _display_path(candidate)
                oversized[relative] = line_count
    if failures:
        raise ComplexityScanError(failures)
    return dict(sorted(oversized.items()))


def _is_test_source(path: Path) -> bool:
    return (
        "__tests__" in path.parts
        or "tests" in path.parts
        or ".test." in path.name
        or ".spec." in path.name
    )


def _is_controller_name(path: Path) -> bool:
    stem = path.stem.lower()
    return (
        "controller" in stem
        or stem.startswith("use-")
        or (
            path.stem.startswith("use")
            and len(path.stem) > 3
            and path.stem[3].isupper()
        )
        or "hooks" in path.parts
    )


def source_role_budget(path: Path) -> tuple[str, int]:
    relative = _display_path(path)
    parts = Path(relative).parts
    if path.suffix == ".sh":
        return "shell entrypoint", MAX_SHELL_ENTRYPOINT_LINES
    if path.suffix == ".py":
        if "services" in parts or "adapters" in parts or "service" in path.stem.lower():
            return "Python service/adapter", MAX_SERVICE_ADAPTER_LINES
        if (
            parts[:4] == ("apps", "api", "app", "routes")
            or "controller" in path.stem.lower()
        ):
            return "Python route/controller", MAX_ROUTE_CONTROLLER_LINES
        return "general module", MAX_GENERAL_MODULE_LINES
    if path.suffix in {".ts", ".tsx"} and _is_controller_name(path):
        return "React hook/controller", MAX_REACT_CONTROLLER_LINES
    if path.suffix == ".tsx":
        return "React page/component", MAX_REACT_COMPONENT_LINES
    return "general module", MAX_GENERAL_MODULE_LINES


def collect_role_ceiling_violations(
    paths: tuple[str, ...],
) -> dict[str, RoleFileBudget]:
    violations: dict[str, RoleFileBudget] = {}
    failures: dict[str, str] = {}
    for raw_path in paths:
        path = ROOT / raw_path
        try:
            candidates = [path] if path.is_file() else list(path.rglob("*"))
        except OSError as exc:
            failures.update(_scan_failure(path, exc).failures)
            continue
        for candidate in candidates:
            try:
                is_file = candidate.is_file()
            except OSError as exc:
                failures.update(_scan_failure(candidate, exc).failures)
                continue
            if (
                not is_file
                or candidate.suffix not in SOURCE_SUFFIXES
                or _is_test_source(candidate)
            ):
                continue
            try:
                source = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                failures.update(_scan_failure(candidate, exc).failures)
                continue
            line_count = len(source.splitlines())
            role, limit = source_role_budget(candidate)
            if line_count > limit:
                violations[_display_path(candidate)] = RoleFileBudget(
                    role=role,
                    line_count=line_count,
                    limit=limit,
                )
    if failures:
        raise ComplexityScanError(failures)
    return dict(sorted(violations.items()))


def role_ceiling_errors(
    violations: dict[str, RoleFileBudget],
) -> list[str]:
    return [
        f"role ceiling exceeded: {path} "
        f"({budget.role}, {budget.line_count} > {budget.limit} lines)"
        for path, budget in violations.items()
    ]


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
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ComplexityScanError(
            {raw_path: f"{type(exc).__name__}: {exc}" for raw_path in sorted(paths)}
        ) from exc
    if result.returncode not in {0, 1}:
        print(result.stdout, end="", file=sys.stderr)
        print(result.stderr, end="", file=sys.stderr)
        raise RuntimeError(f"ruff complexity scan failed with {result.returncode}")

    findings = json.loads(result.stdout or "[]")
    violations: dict[str, ComplexityBudget] = {}
    identity_cache: dict[Path, dict[tuple[int, str], str]] = {}
    failures: dict[str, str] = {}
    for finding in findings:
        match = MESSAGE_RE.fullmatch(str(finding.get("message") or ""))
        if match is None:
            filename = Path(str(finding.get("filename") or "<unknown>"))
            if not filename.is_absolute():
                filename = ROOT / filename
            failures[_display_path(filename)] = (
                f"ruff {finding.get('code') or 'scan error'}: "
                f"{finding.get('message') or 'unknown scanner error'}"
            )
            continue
        absolute_filename = Path(str(finding["filename"])).resolve()
        filename = _display_path(absolute_filename)
        location = finding.get("location")
        row = int(location.get("row") or 0) if isinstance(location, dict) else 0
        name = match.group("name")
        if absolute_filename not in identity_cache:
            try:
                identity_cache[absolute_filename] = function_identities(
                    absolute_filename
                )
            except ComplexityScanError as exc:
                failures.update(exc.failures)
                continue
        identities = identity_cache[absolute_filename]
        identity = identities.get((row, name), name)
        key = f"{filename}::{identity}"
        violations[key] = ComplexityBudget(
            max_complexity=int(match.group("complexity")),
            count=1,
        )
    if failures:
        raise ComplexityScanError(failures)
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
        elif budget.max_complexity < allowed.max_complexity:
            errors.append(
                f"complexity baseline is stale: {key} "
                f"{allowed.max_complexity} -> {budget.max_complexity}"
            )
        if budget.count > allowed.count:
            errors.append(
                f"violation count grew: {key} {allowed.count} -> {budget.count}"
            )
        elif budget.count < allowed.count:
            errors.append(
                f"violation-count baseline is stale: {key} "
                f"{allowed.count} -> {budget.count}"
            )
    for key in sorted(baseline.keys() - current.keys()):
        errors.append(f"complexity baseline is stale: {key} is no longer a violation")
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
        elif line_count < allowed:
            errors.append(
                f"oversized-file baseline is stale: {path} {allowed} -> {line_count}"
            )
    for path in sorted(baseline.keys() - current.keys()):
        errors.append(
            f"oversized-file baseline is stale: {path} is no longer oversized"
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
            elif budget.value < allowed.value:
                errors.append(
                    f"{dimension} baseline is stale: {key} "
                    f"{allowed.value} -> {budget.value}"
                )
    for dimension, allowed_findings in baseline.items():
        findings = current.get(dimension, {})
        for key in sorted(allowed_findings.keys() - findings.keys()):
            errors.append(
                f"{dimension} baseline is stale: {key} is no longer a violation"
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
    failures: dict[str, str] = {}
    try:
        current = collect_violations(scan_paths)
    except ComplexityScanError as exc:
        failures.update(exc.failures)
        current = {}
    try:
        oversized_files = collect_oversized_files(DEFAULT_LINE_PATHS)
    except ComplexityScanError as exc:
        failures.update(exc.failures)
        oversized_files = {}
    try:
        metrics = collect_python_metrics(scan_paths)
    except ComplexityScanError as exc:
        failures.update(exc.failures)
        metrics = {
            "function_lines": {},
            "function_parameters": {},
            "nesting_depth": {},
        }
    try:
        role_violations = collect_role_ceiling_violations(DEFAULT_ROLE_LINE_PATHS)
    except ComplexityScanError as exc:
        failures.update(exc.failures)
        role_violations = {}
    if failures:
        print("Complexity scan failed closed; unscanned files:", file=sys.stderr)
        for path, reason in sorted(failures.items()):
            print(f"- {path}: {reason}", file=sys.stderr)
        return 1

    role_errors = role_ceiling_errors(role_violations)
    if role_errors:
        print("Complexity budget failed:", file=sys.stderr)
        for error in role_errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Role ceilings are hard limits and cannot be grandfathered.",
            file=sys.stderr,
        )
        return 1

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

    metric_count = sum(len(findings) for findings in metrics.values())
    print(
        "Complexity budget passed: "
        f"{len(current)} grandfathered violations; "
        f"{len(oversized_files)} grandfathered oversized files; "
        f"{metric_count} multi-dimensional findings; "
        "0 role-ceiling violations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
