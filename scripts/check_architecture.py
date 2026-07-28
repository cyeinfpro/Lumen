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
    PackageSpec("image-job", ROOT / "image-job/image_job", "image_job"),
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


# 分层规则：(package, rule 名, 下层包前缀, 上层包前缀)。
#
# 口径必须覆盖**每一个**下层目录，漏一个就等于给它开了后门 —— 本文件之前只列了
# api 的 services/canvas_services/workflow_services，于是 `app.images.application`
# 反向 import `app.routes._image_delivery` 这类违规完全不在检测视野里（审计新-2/新-18）。
# 新增下层目录时同步加进来；每条规则当前均为 0 违规，任何新增都会直接失败。
LAYER_RULES: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "api",
        "api-lower-to-routes",
        (
            "app.services",
            "app.canvas_services",
            "app.workflow_services",
            "app.workflow_domain",
            "app.workflows",
            "app.images",
        ),
        ("app.routes",),
    ),
    (
        "worker",
        "worker-lower-to-tasks",
        (
            "app.services",
            "app.background_removal",
            "app.upstream_parts",
            "app.video_upstream_parts",
            "app.upstream_clients",
            "app.provider_runtime",
            "app.reconciliation",
            "app.outbox",
            "app.locks",
            "app.jobs",
            "app.task_runtime",
        ),
        ("app.tasks",),
    ),
    (
        "image-job",
        "image-job-lower-to-http",
        (
            "image_job.domain",
            "image_job.application",
            "image_job.ports",
            "image_job.adapters",
        ),
        (
            "image_job.api",
            "image_job.route_handlers",
            "image_job.app_factory",
        ),
    ),
)

FORBIDDEN_DEPENDENCY_RULES: tuple[
    tuple[str, str, tuple[str, ...], tuple[str, ...]], ...
] = (
    (
        "api",
        "workflow-v2-to-legacy",
        (
            "app.main",
            "app.routes",
            "app.services",
            "app.workflows",
        ),
        (
            "app.workflow_services",
            "app.workflow_domain",
        ),
    ),
    (
        "api",
        "workflow-domain-layering",
        ("app.workflows.domain",),
        (
            "app.routes",
            "app.workflows.adapters",
            "app.workflows.application",
            "app.workflows.composition",
            "app.workflows.transport",
        ),
    ),
    (
        "api",
        "workflow-http-to-adapters",
        (
            "app.routes.workflow_routes",
            "app.workflows.transport",
        ),
        ("app.workflows.adapters",),
    ),
)


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
