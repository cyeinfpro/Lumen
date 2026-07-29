#!/usr/bin/env python3
"""Enforce acyclic package graphs and ratchet known layer violations."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import tomllib
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from architecture_audit import (  # noqa: E402
    collect_public_api_manifest,
    collect_runtime_findings,
    compare_inventory,
    load_inventory,
)


DEFAULT_BASELINE = ROOT / "scripts" / "architecture-baseline.json"
DEFAULT_LAYER_CONFIG = ROOT / "scripts" / "architecture-layers.toml"


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


@dataclass(frozen=True)
class ArchitectureLayerConfig:
    packages: tuple[PackageSpec, ...]
    layer_rules: tuple[
        tuple[str, str, tuple[str, ...], tuple[str, ...]],
        ...,
    ]
    forbidden_rules: tuple[
        tuple[str, str, tuple[str, ...], tuple[str, ...]],
        ...,
    ]


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string list")
    return tuple(value)


def _load_rules(
    values: object,
    *,
    field: str,
) -> tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise ValueError(f"{field} must be a list")
    rules: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"{field}[{index}] must be a table")
        name = value.get("name")
        package = value.get("package")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{field}[{index}].name is invalid")
        if name in seen:
            raise ValueError(f"duplicate architecture rule: {name}")
        seen.add(name)
        if not isinstance(package, str) or not package:
            raise ValueError(f"{field}[{index}].package is invalid")
        rules.append(
            (
                package,
                name,
                _string_list(
                    value.get("sources"),
                    field=f"{field}[{index}].sources",
                ),
                _string_list(
                    value.get("targets"),
                    field=f"{field}[{index}].targets",
                ),
            )
        )
    return tuple(rules)


def load_layer_config(
    path: Path = DEFAULT_LAYER_CONFIG,
    *,
    root: Path = ROOT,
) -> ArchitectureLayerConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    packages_raw = raw.get("packages")
    if raw.get("version") != 1 or not isinstance(packages_raw, list):
        raise ValueError("unsupported architecture layer configuration")
    packages: list[PackageSpec] = []
    seen_packages: set[str] = set()
    for index, value in enumerate(packages_raw):
        if not isinstance(value, dict):
            raise ValueError(f"packages[{index}] must be a table")
        name = value.get("name")
        package = value.get("package")
        relative_root = value.get("root")
        if not all(
            isinstance(item, str) and item
            for item in (name, package, relative_root)
        ):
            raise ValueError(f"packages[{index}] is invalid")
        assert isinstance(name, str)
        assert isinstance(package, str)
        assert isinstance(relative_root, str)
        if name in seen_packages:
            raise ValueError(f"duplicate architecture package: {name}")
        seen_packages.add(name)
        packages.append(PackageSpec(name, root / relative_root, package))
    layer_rules = _load_rules(raw.get("rules"), field="rules")
    forbidden_rules = _load_rules(raw.get("forbidden"), field="forbidden")
    referenced_packages = {
        package
        for package, _name, _sources, _targets in (
            *layer_rules,
            *forbidden_rules,
        )
    }
    unknown = sorted(referenced_packages - seen_packages)
    if unknown:
        raise ValueError(f"architecture rules reference unknown packages: {unknown}")
    return ArchitectureLayerConfig(
        packages=tuple(packages),
        layer_rules=layer_rules,
        forbidden_rules=forbidden_rules,
    )


DEFAULT_LAYER_CONFIGURATION = load_layer_config()
DEFAULT_PACKAGES = DEFAULT_LAYER_CONFIGURATION.packages
LAYER_RULES = DEFAULT_LAYER_CONFIGURATION.layer_rules
FORBIDDEN_DEPENDENCY_RULES = DEFAULT_LAYER_CONFIGURATION.forbidden_rules


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
    if not spec.root.is_dir():
        raise FileNotFoundError(
            f"architecture package root is missing: {spec.name}: {spec.root}"
        )
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


def _matches_module_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
    )


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
    for package, rule, source_prefixes, target_prefixes in FORBIDDEN_DEPENDENCY_RULES:
        if (
            package == spec_name
            and _matches_module_prefix(source_module, source_prefixes)
            and _matches_module_prefix(target_module, target_prefixes)
        ):
            return rule
    for package, rule, lower_prefixes, upper_prefixes in LAYER_RULES:
        if package != spec_name:
            continue
        if source_module.startswith(lower_prefixes) and target_module.startswith(
            upper_prefixes
        ):
            return rule
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
    errors = [
        f"new architecture violation: {key}" for key in sorted(current - baseline)
    ]
    errors.extend(
        f"architecture violation baseline is stale: {key}"
        for key in sorted(baseline - current)
    )
    return errors


def compare_cycles(current: set[str], baseline: set[str]) -> list[str]:
    errors = [f"new architecture cycle: {key}" for key in sorted(current - baseline)]
    errors.extend(
        f"architecture cycle baseline is stale: {key}"
        for key in sorted(baseline - current)
    )
    return errors


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
    runtime_findings = collect_runtime_findings()
    runtime_baseline, public_api_baseline = load_inventory()
    public_api = collect_public_api_manifest()
    errors.extend(
        compare_inventory(
            set(runtime_findings),
            runtime_baseline,
            public_api,
            public_api_baseline,
        )
    )
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

    print(
        "Architecture budget passed: "
        f"{len(current_cycles)} grandfathered cycles, "
        f"{len(violations)} grandfathered boundary violations; "
        f"{len(runtime_findings)} runtime-coupling findings."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
