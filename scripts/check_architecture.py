#!/usr/bin/env python3
"""Enforce acyclic package graphs and ratchet known layer violations."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "scripts" / "architecture-baseline.json"


@dataclass(frozen=True)
class PackageSpec:
    name: str
    root: Path
    package: str


@dataclass(frozen=True, order=True)
class ArchitectureViolation:
    rule: str
    source: str
    target: str

    @property
    def key(self) -> str:
        return f"{self.rule}|{self.source}|{self.target}"


@dataclass(frozen=True)
class PackageGraph:
    spec: PackageSpec
    modules: dict[str, Path]
    edges: dict[str, set[str]]


DEFAULT_PACKAGES = (
    PackageSpec("core", ROOT / "packages/core/lumen_core", "lumen_core"),
    PackageSpec("api", ROOT / "apps/api/app", "app"),
    PackageSpec("worker", ROOT / "apps/worker/app", "app"),
    PackageSpec("tgbot", ROOT / "apps/tgbot/app", "app"),
)


def module_name(spec: PackageSpec, path: Path) -> str:
    relative = path.relative_to(spec.root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((spec.package, *parts)) if parts else spec.package


def module_package(module: str, path: Path) -> list[str]:
    parts = module.split(".")
    return parts if path.name == "__init__.py" else parts[:-1]


def resolve_from_target(
    module: str,
    path: Path,
    node: ast.ImportFrom,
) -> str:
    if node.level == 0:
        return node.module or ""
    package = module_package(module, path)
    keep = max(0, len(package) - (node.level - 1))
    prefix = package[:keep]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def imported_targets(
    module: str,
    path: Path,
    tree: ast.AST,
) -> Iterable[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = resolve_from_target(module, path, node)
            for alias in node.names:
                if not base:
                    continue
                target = base if alias.name == "*" else f"{base}.{alias.name}"
                yield target, node.lineno


def resolve_internal_module(
    target: str,
    modules: dict[str, Path],
) -> str | None:
    candidate = target
    while candidate:
        if candidate in modules:
            return candidate
        candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
    return None


def build_package_graph(spec: PackageSpec) -> PackageGraph:
    modules = {
        module_name(spec, path): path
        for path in spec.root.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    edges: dict[str, set[str]] = {module: set() for module in modules}
    for source_module, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for target, _line in imported_targets(source_module, path, tree):
            resolved = resolve_internal_module(target, modules)
            if resolved is not None and resolved != source_module:
                edges[source_module].add(resolved)
    return PackageGraph(spec=spec, modules=modules, edges=edges)


def strongly_connected_components(
    edges: dict[str, set[str]],
) -> list[tuple[str, ...]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        index += 1
        indices[node] = index
        low_links[node] = index
        stack.append(node)
        on_stack.add(node)
        for target in edges.get(node, set()):
            if target not in indices:
                visit(target)
                low_links[node] = min(low_links[node], low_links[target])
            elif target in on_stack:
                low_links[node] = min(low_links[node], indices[target])
        if low_links[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            current = stack.pop()
            on_stack.remove(current)
            component.append(current)
            if current == node:
                break
        if len(component) > 1:
            components.append(tuple(sorted(component)))

    for node in sorted(edges):
        if node not in indices:
            visit(node)
    return sorted(components)


def boundary_rule(
    spec_name: str,
    source_module: str,
    target_module: str,
) -> str | None:
    if target_module == "apps" or target_module.startswith("apps."):
        return "cross-app-import"
    if spec_name == "core" and (
        target_module == "app" or target_module.startswith("app.")
    ):
        return "core-to-application"
    if spec_name == "api":
        lower_prefixes = (
            "app.services",
            "app.canvas_services",
            "app.workflow_services",
        )
        if source_module.startswith(lower_prefixes) and target_module.startswith(
            "app.routes"
        ):
            return "api-lower-to-routes"
    if spec_name == "worker":
        lower_prefixes = (
            "app.services",
            "app.background_removal",
            "app.upstream_parts",
        )
        if source_module.startswith(lower_prefixes) and target_module.startswith(
            "app.tasks"
        ):
            return "worker-lower-to-tasks"
    return None


def collect_violations(
    specs: tuple[PackageSpec, ...] = DEFAULT_PACKAGES,
) -> tuple[dict[str, ArchitectureViolation], dict[str, list[tuple[str, ...]]]]:
    violations: dict[str, ArchitectureViolation] = {}
    cycles: dict[str, list[tuple[str, ...]]] = {}
    for spec in specs:
        graph = build_package_graph(spec)
        package_cycles = strongly_connected_components(graph.edges)
        if package_cycles:
            cycles[spec.name] = package_cycles
        for source_module, path in graph.modules.items():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, _line in imported_targets(source_module, path, tree):
                resolved = resolve_internal_module(target, graph.modules) or target
                rule = boundary_rule(spec.name, source_module, resolved)
                if rule is None:
                    continue
                source = path.relative_to(ROOT).as_posix()
                violation = ArchitectureViolation(rule, source, resolved)
                violations[violation.key] = violation
    return dict(sorted(violations.items())), cycles


def cycle_keys(cycles: dict[str, list[tuple[str, ...]]]) -> set[str]:
    return {
        f"{package}|{','.join(component)}"
        for package, components in cycles.items()
        for component in components
    }


def load_baseline(path: Path) -> tuple[set[str], set[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        raw.get("version") != 1
        or not isinstance(raw.get("violations"), list)
        or not isinstance(raw.get("cycles"), list)
    ):
        raise ValueError("unsupported architecture baseline")
    return (
        {str(item) for item in raw["violations"]},
        {str(item) for item in raw["cycles"]},
    )


def compare_violations(current: set[str], baseline: set[str]) -> list[str]:
    return [f"new architecture violation: {key}" for key in sorted(current - baseline)]


def compare_cycles(current: set[str], baseline: set[str]) -> list[str]:
    return [f"new architecture cycle: {key}" for key in sorted(current - baseline)]


def write_baseline(
    path: Path,
    violations: dict[str, ArchitectureViolation],
    cycles: dict[str, list[tuple[str, ...]]],
) -> None:
    payload: dict[str, Any] = {
        "version": 1,
        "violations": sorted(violations),
        "cycles": sorted(cycle_keys(cycles)),
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    violations, cycles = collect_violations()
    if args.update_baseline:
        write_baseline(args.baseline, violations, cycles)
        print(
            f"updated {args.baseline.relative_to(ROOT)} "
            f"({len(violations)} violations, {len(cycle_keys(cycles))} cycles)"
        )
        return 0

    baseline_violations, baseline_cycles = load_baseline(args.baseline)
    current_cycles = cycle_keys(cycles)
    errors = compare_violations(set(violations), baseline_violations)
    errors.extend(compare_cycles(current_cycles, baseline_cycles))
    if errors:
        print("Architecture budget failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        print(
            "Move the dependency toward a lower layer. Only intentional baseline "
            "reductions should use --update-baseline.",
            file=sys.stderr,
        )
        return 1

    removed_violations = len(baseline_violations - set(violations))
    removed_cycles = len(baseline_cycles - current_cycles)
    print(
        "Architecture budget passed: "
        f"{len(current_cycles)} grandfathered cycles, "
        f"{len(violations)} grandfathered boundary violations; "
        f"{removed_cycles} cycles and {removed_violations} violations removed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
