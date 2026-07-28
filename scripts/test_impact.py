#!/usr/bin/env python3
"""Build an explainable impact-test plan for a Git diff."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from check_architecture import (  # noqa: E402
    DEFAULT_PACKAGES,
    PackageSpec,
    build_package_graph,
)


DEFAULT_MANIFEST = ROOT / "scripts" / "test-manifest.toml"


@dataclass(frozen=True)
class Gate:
    name: str
    command: str
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    name: str
    paths: tuple[str, ...]
    commands: tuple[str, ...]
    gates: tuple[str, ...]
    risk: tuple[str, ...]
    resources: tuple[str, ...]
    fallback: bool


@dataclass(frozen=True)
class Manifest:
    max_reverse_depth: int
    exclusive_resources: tuple[str, ...]
    full_mandatory_paths: tuple[str, ...]
    gates: dict[str, Gate]
    rules: tuple[Rule, ...]


def _normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _string_list(
    value: object,
    *,
    field: str,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    return tuple(value)


def load_manifest(path: Path = DEFAULT_MANIFEST) -> Manifest:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    planner = raw.get("planner")
    if not isinstance(planner, dict):
        raise ValueError("manifest is missing [planner]")
    max_reverse_depth = planner.get("max_reverse_depth", 2)
    if not isinstance(max_reverse_depth, int) or max_reverse_depth < 0:
        raise ValueError("planner.max_reverse_depth must be a non-negative integer")

    raw_gates = raw.get("gates", {})
    if not isinstance(raw_gates, dict):
        raise ValueError("manifest gates must be a table")
    gates: dict[str, Gate] = {}
    for name, config in raw_gates.items():
        if not isinstance(config, dict):
            raise ValueError(f"gate {name!r} must be a table")
        command = config.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError(f"gate {name!r} must define a command")
        gates[name] = Gate(
            name=name,
            command=command,
            resources=_string_list(
                config.get("resources", []),
                field=f"gates.{name}.resources",
            ),
        )

    raw_rules = raw.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("manifest must define at least one [[rules]] entry")
    rules: list[Rule] = []
    seen_names: set[str] = set()
    for index, config in enumerate(raw_rules):
        if not isinstance(config, dict):
            raise ValueError(f"rules[{index}] must be a table")
        name = config.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"rules[{index}].name must be a non-empty string")
        if name in seen_names:
            raise ValueError(f"duplicate rule name: {name}")
        seen_names.add(name)
        rule_gates = _string_list(
            config.get("gates", []),
            field=f"rules.{name}.gates",
        )
        unknown_gates = sorted(set(rule_gates) - gates.keys())
        if unknown_gates:
            raise ValueError(
                f"rule {name!r} references unknown gates: {unknown_gates}"
            )
        fallback = config.get("fallback", False)
        if not isinstance(fallback, bool):
            raise ValueError(f"rules.{name}.fallback must be a boolean")
        rules.append(
            Rule(
                name=name,
                paths=_string_list(
                    config.get("paths"),
                    field=f"rules.{name}.paths",
                    allow_empty=False,
                ),
                commands=_string_list(
                    config.get("commands"),
                    field=f"rules.{name}.commands",
                    allow_empty=False,
                ),
                gates=rule_gates,
                risk=_string_list(
                    config.get("risk", []),
                    field=f"rules.{name}.risk",
                ),
                resources=_string_list(
                    config.get("resources", []),
                    field=f"rules.{name}.resources",
                ),
                fallback=fallback,
            )
        )

    return Manifest(
        max_reverse_depth=max_reverse_depth,
        exclusive_resources=_string_list(
            planner.get("exclusive_resources", []),
            field="planner.exclusive_resources",
        ),
        full_mandatory_paths=_string_list(
            planner.get("full_mandatory_paths", []),
            field="planner.full_mandatory_paths",
        ),
        gates=gates,
        rules=tuple(rules),
    )


def _glob_regex(pattern: str) -> re.Pattern[str]:
    normalized = _normalize_repo_path(pattern)
    parts: list[str] = ["^"]
    index = 0
    while index < len(normalized):
        char = normalized[index]
        if char == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                index += 2
                if index < len(normalized) and normalized[index] == "/":
                    parts.append("(?:.*/)?")
                    index += 1
                else:
                    parts.append(".*")
                continue
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    parts.append("$")
    return re.compile("".join(parts))


def path_matches(path: str, pattern: str) -> bool:
    normalized = _normalize_repo_path(path)
    return _glob_regex(pattern).fullmatch(normalized) is not None


def _relative_file(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def collect_reverse_imports(
    changed_files: Sequence[str],
    *,
    repo_root: Path = ROOT,
    specs: Sequence[PackageSpec] = DEFAULT_PACKAGES,
    max_depth: int = 2,
) -> list[dict[str, Any]]:
    if max_depth <= 0:
        return []
    changed = {_normalize_repo_path(item) for item in changed_files}
    impacts: list[dict[str, Any]] = []
    for spec in specs:
        graph = build_package_graph(spec)
        module_by_file = {
            _relative_file(path, repo_root): module
            for module, path in graph.modules.items()
        }
        reverse_edges: dict[str, set[str]] = {
            module: set() for module in graph.modules
        }
        for source, targets in graph.edges.items():
            for target in targets:
                reverse_edges.setdefault(target, set()).add(source)

        for changed_file in sorted(changed & module_by_file.keys()):
            changed_module = module_by_file[changed_file]
            queue: list[tuple[str, int]] = [(changed_module, 0)]
            visited = {changed_module}
            callers: list[dict[str, Any]] = []
            while queue:
                current, depth = queue.pop(0)
                if depth >= max_depth:
                    continue
                for caller in sorted(reverse_edges.get(current, ())):
                    if caller in visited:
                        continue
                    visited.add(caller)
                    caller_depth = depth + 1
                    callers.append(
                        {
                            "file": _relative_file(
                                graph.modules[caller],
                                repo_root,
                            ),
                            "module": caller,
                            "depth": caller_depth,
                        }
                    )
                    queue.append((caller, caller_depth))
            if callers:
                ordered_callers = sorted(
                    callers,
                    key=lambda item: (
                        item["depth"],
                        item["module"],
                    ),
                )
                impacts.append(
                    {
                        "changed_file": changed_file,
                        "changed_module": changed_module,
                        "callers": ordered_callers,
                    }
                )
    return impacts


def _render_gate_command(
    gate: Gate,
    *,
    changed_files: Sequence[str],
    repo_root: Path,
) -> tuple[str | None, str | None]:
    substitutions = {
        "changed_python": [
            path
            for path in changed_files
            if path.endswith(".py") and (repo_root / path).is_file()
        ],
        "changed_migrations": [
            path
            for path in changed_files
            if path_matches(path, "apps/api/alembic/versions/*.py")
            and (repo_root / path).is_file()
        ],
    }
    command = gate.command
    for placeholder, files in substitutions.items():
        token = "{" + placeholder + "}"
        if token not in command:
            continue
        if not files:
            return None, f"{placeholder} is empty"
        command = command.replace(token, shlex.join(sorted(files)))
    return command, None


def _command_id(kind: str, command: str, name: str | None = None) -> str:
    if name is not None:
        return f"{kind}:{name}"
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    return f"{kind}:{digest}"


def _matching_paths(paths: Iterable[str], patterns: Sequence[str]) -> list[str]:
    return sorted(
        path
        for path in set(paths)
        if any(path_matches(path, pattern) for pattern in patterns)
    )


def build_plan(
    manifest: Manifest,
    *,
    changed_files: Sequence[str],
    base: str,
    head: str,
    reverse_imports: Sequence[dict[str, Any]],
    repo_root: Path = ROOT,
) -> dict[str, Any]:
    normalized_changed = sorted(
        {_normalize_repo_path(path) for path in changed_files if path}
    )
    reverse_files = sorted(
        {
            caller["file"]
            for impact in reverse_imports
            for caller in impact.get("callers", [])
        }
    )
    raw_matches = [
        (
            rule,
            _matching_paths(normalized_changed, rule.paths),
            _matching_paths(reverse_files, rule.paths),
        )
        for rule in manifest.rules
    ]
    covered_direct = {
        path
        for rule, direct_paths, _reverse_paths in raw_matches
        if not rule.fallback
        for path in direct_paths
    }
    covered_reverse = {
        path
        for rule, _direct_paths, reverse_paths in raw_matches
        if not rule.fallback
        for path in reverse_paths
    }
    matched_rules: list[dict[str, Any]] = []
    matched_rule_objects: list[Rule] = []
    for rule, direct_paths, reverse_paths in raw_matches:
        if rule.fallback:
            direct_paths = sorted(set(direct_paths) - covered_direct)
            reverse_paths = sorted(set(reverse_paths) - covered_reverse)
        if not direct_paths and not reverse_paths:
            continue
        matched_rule_objects.append(rule)
        matched_rules.append(
            {
                "name": rule.name,
                "matched_paths": direct_paths,
                "reverse_import_paths": reverse_paths,
                "commands": list(rule.commands),
                "gates": list(rule.gates),
                "risk": list(rule.risk),
            }
        )
    matched_rules.sort(key=lambda item: item["name"])
    matched_rule_objects.sort(key=lambda item: item.name)

    command_entries: list[dict[str, Any]] = []
    skipped_gates: list[dict[str, str]] = []
    selected_gate_names = sorted(
        {gate_name for rule in matched_rule_objects for gate_name in rule.gates}
    )
    for gate_name in selected_gate_names:
        gate = manifest.gates[gate_name]
        command, reason = _render_gate_command(
            gate,
            changed_files=normalized_changed,
            repo_root=repo_root,
        )
        if command is None:
            skipped_gates.append({"name": gate_name, "reason": reason or "skipped"})
            continue
        command_entries.append(
            {
                "id": _command_id("gate", command, gate_name),
                "kind": "gate",
                "command": command,
                "sources": [
                    rule.name
                    for rule in matched_rule_objects
                    if gate_name in rule.gates
                ],
                "resource_tags": sorted(set(gate.resources)),
            }
        )

    tests_by_command: dict[str, dict[str, set[str]]] = {}
    exclusive = set(manifest.exclusive_resources)
    for rule in matched_rule_objects:
        resource_tags = set(rule.resources) | (set(rule.risk) & exclusive)
        for command in rule.commands:
            entry = tests_by_command.setdefault(
                command,
                {"sources": set(), "resource_tags": set()},
            )
            entry["sources"].add(rule.name)
            entry["resource_tags"].update(resource_tags)
    for command in sorted(tests_by_command):
        details = tests_by_command[command]
        command_entries.append(
            {
                "id": _command_id("test", command),
                "kind": "test",
                "command": command,
                "sources": sorted(details["sources"]),
                "resource_tags": sorted(details["resource_tags"]),
            }
        )

    full_reasons = sorted(
        (
            {"file": path, "pattern": pattern}
            for path in normalized_changed
            for pattern in manifest.full_mandatory_paths
            if path_matches(path, pattern)
        ),
        key=lambda item: (item["file"], item["pattern"]),
    )
    matched_names = {rule.name for rule in matched_rule_objects}
    matched_direct_or_reverse = {
        path
        for rule in manifest.rules
        for path in (
            _matching_paths(normalized_changed, rule.paths)
            + _matching_paths(reverse_files, rule.paths)
        )
    }
    return {
        "schema_version": 1,
        "base": base,
        "head": head,
        "changed_files": normalized_changed,
        "matched_rules": matched_rules,
        "reverse_imports": list(reverse_imports),
        "commands": command_entries,
        "gates": selected_gate_names,
        "skipped_gates": skipped_gates,
        "risk": sorted(
            {risk for rule in matched_rule_objects for risk in rule.risk}
        ),
        "not_run_suites": [
            {
                "name": rule.name,
                "reason": "no changed or reverse-dependent path matched",
            }
            for rule in sorted(manifest.rules, key=lambda item: item.name)
            if rule.name not in matched_names
        ],
        "unmatched_changed_files": sorted(
            set(normalized_changed) - matched_direct_or_reverse
        ),
        "full_mandatory": bool(full_reasons),
        "full_mandatory_reasons": full_reasons,
        "exclusive_resources": list(manifest.exclusive_resources),
    }


def changed_files_between(
    base: str,
    head: str,
    *,
    repo_root: Path = ROOT,
) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRD",
            "--find-renames",
            f"{base}...{head}",
        ],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff failed")
    return sorted(
        {
            line.strip().replace("\\", "/")
            for line in result.stdout.splitlines()
            if line.strip()
        }
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = load_manifest(args.manifest)
    changed_files = changed_files_between(args.base, args.head)
    reverse_imports = collect_reverse_imports(
        changed_files,
        max_depth=manifest.max_reverse_depth,
    )
    plan = build_plan(
        manifest,
        changed_files=changed_files,
        base=args.base,
        head=args.head,
        reverse_imports=reverse_imports,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {args.output}: {len(changed_files)} changed files, "
        f"{len(plan['matched_rules'])} rules, {len(plan['commands'])} commands, "
        f"full_mandatory={str(plan['full_mandatory']).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
