#!/usr/bin/env python3
"""Generate Lumen's governance score from repository and test evidence."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / ".audit_state" / "governance-evidence.json"
DEFAULT_JSON_OUTPUT = ROOT / "docs" / "refactors" / "governance-score.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "docs" / "refactors" / "governance-score.md"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

WEIGHTS = {
    "funding_async_correctness": 0.15,
    "runtime_ownership": 0.12,
    "module_boundaries": 0.12,
    "ci_and_gates": 0.12,
    "release_update_rollback": 0.10,
    "web_state_isolation": 0.10,
    "data_migration_storage": 0.08,
    "observability_recovery": 0.08,
    "security_supply_chain": 0.06,
    "debt_documentation": 0.07,
}

DIMENSION_CHECKS = {
    "funding_async_correctness": (
        "known_p0_zero",
        "known_p1_zero",
        "sidecar_recovery_faults",
        "sidecar_delivery_faults",
    ),
    "runtime_ownership": (
        "runtime_gate",
        "ownership_registry_complete",
        "runtime_scanner_full_roots",
    ),
    "module_boundaries": (
        "architecture_gate",
        "architecture_layers_valid",
        "architecture_layers_consumed",
        "web_domain_boundaries",
        "billing_dynamic_facade_zero",
    ),
    "ci_and_gates": (
        "manifest_gate",
        "rerun_plan_identity",
        "baseline_monotonic",
        "full_tests",
    ),
    "release_update_rollback": (
        "migration_gate",
        "release_tag_main_guard",
        "stable_alias_before_release",
        "updater_health_commit",
        "release_faults",
        "release_proof",
    ),
    "web_state_isolation": (
        "web_p1_zero",
        "web_isolation",
        "web_domain_boundaries",
    ),
    "data_migration_storage": (
        "migration_gate",
        "migration_faults",
        "storage_consistency",
    ),
    "observability_recovery": (
        "fault_matrix",
        "observability_metrics",
        "recovery_proof",
    ),
    "security_supply_chain": (
        "release_tag_main_guard",
        "signed_images",
        "supply_chain",
    ),
    "debt_documentation": (
        "dead_code_zero",
        "facade_gate",
        "facade_inventory",
        "documentation_freshness",
    ),
}

HARD_GATES = (
    "known_p0_zero",
    "known_p1_zero",
    "manifest_gate",
    "runtime_gate",
    "ownership_registry_complete",
    "migration_gate",
    "release_tag_main_guard",
    "updater_health_commit",
    "web_isolation",
    "full_tests",
    "release_proof",
)

STATIC_COMMANDS = {
    "architecture_gate": ("uv", "run", "python", "scripts/check_architecture.py"),
    "baseline_monotonic": (
        "uv",
        "run",
        "python",
        "scripts/baseline_monotonic.py",
    ),
    "complexity_gate": ("uv", "run", "python", "scripts/check_complexity.py"),
    "dead_code_zero": ("uv", "run", "python", "scripts/dead_code_audit.py"),
    "facade_gate": ("uv", "run", "python", "scripts/architecture_audit.py"),
    "facade_inventory": (
        "uv",
        "run",
        "python",
        "scripts/facade_inventory.py",
    ),
    "manifest_gate": ("uv", "run", "python", "scripts/test_manifest_lint.py"),
    "migration_gate": (
        "uv",
        "run",
        "python",
        "scripts/lint_alembic_breaking.py",
    ),
    "runtime_gate": (
        "uv",
        "run",
        "python",
        "scripts/module_runtime_state_audit.py",
    ),
}


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    source: str
    detail: str


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _head_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("cannot resolve the current git commit")
    return commit


def _command_checks(root: Path, runner: Runner) -> dict[str, CheckResult]:
    checks: dict[str, CheckResult] = {}
    for name, command in STATIC_COMMANDS.items():
        result = runner(command, root)
        output = (result.stdout or result.stderr).strip().splitlines()
        checks[name] = CheckResult(
            passed=result.returncode == 0,
            source="command",
            detail=output[-1] if output else f"exit={result.returncode}",
        )
    return checks


def _known_defect_checks(root: Path) -> dict[str, CheckResult]:
    payload = _load_json(root / "docs/refactors/known-defects.json")
    defects = payload.get("defects")
    if payload.get("version") != 1 or not isinstance(defects, list):
        raise ValueError("unsupported known-defects registry")
    open_by_severity: dict[str, list[str]] = {"P0": [], "P1": []}
    web_open: list[str] = []
    for entry in defects:
        if not isinstance(entry, dict):
            raise ValueError("invalid known-defects entry")
        defect_id = str(entry.get("id", ""))
        severity = str(entry.get("severity", ""))
        status = str(entry.get("status", ""))
        if status == "closed":
            tests = entry.get("regression_tests")
            fixed_commit = entry.get("fixed_commit")
            if (
                not isinstance(tests, list)
                or not tests
                or not isinstance(fixed_commit, str)
                or COMMIT_RE.fullmatch(fixed_commit) is None
            ):
                raise ValueError(
                    f"closed defect {defect_id} lacks tests or a fixed commit"
                )
        elif severity in open_by_severity:
            open_by_severity[severity].append(defect_id)
        if defect_id in {"P1-05", "P1-06", "P1-07"} and status != "closed":
            web_open.append(defect_id)
    return {
        "known_p0_zero": CheckResult(
            passed=not open_by_severity["P0"],
            source="known-defects",
            detail=",".join(open_by_severity["P0"]) or "0 open P0",
        ),
        "known_p1_zero": CheckResult(
            passed=not open_by_severity["P1"],
            source="known-defects",
            detail=",".join(open_by_severity["P1"]) or "0 open P1",
        ),
        "web_p1_zero": CheckResult(
            passed=not web_open,
            source="known-defects",
            detail=",".join(web_open) or "web P1 defects closed",
        ),
    }


def _ownership_check(root: Path) -> CheckResult:
    ownership = _load_json(root / "docs/refactors/module-ownership.json")
    runtime = _load_json(root / "docs/refactors/module-runtime-state-ledger.json")
    modules = ownership.get("modules")
    runtime_modules = runtime.get("modules")
    if (
        ownership.get("version") != 1
        or not isinstance(modules, list)
        or not isinstance(runtime_modules, list)
    ):
        raise ValueError("unsupported module ownership registry")
    required = {
        "path",
        "owner",
        "composition_root",
        "shutdown",
        "test_reset",
        "symbols",
    }
    registered: dict[str, dict[str, Any]] = {}
    for entry in modules:
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise ValueError("invalid module ownership entry")
        registered[str(entry["path"])] = entry
    missing: list[str] = []
    for runtime_entry in runtime_modules:
        if not isinstance(runtime_entry, dict):
            raise ValueError("invalid runtime ledger entry")
        path = str(runtime_entry.get("path", ""))
        owner_entry = registered.get(path)
        if owner_entry is None:
            missing.append(path)
            continue
        expected = set(runtime_entry.get("symbols") or [])
        actual = set(owner_entry.get("symbols") or [])
        if expected != actual:
            missing.append(f"{path}:symbol-mismatch")
    return CheckResult(
        passed=not missing,
        source="module-ownership",
        detail=",".join(missing) or f"{len(registered)} modules registered",
    )


def _source_checks(root: Path) -> dict[str, CheckResult]:
    architecture = (root / "scripts/check_architecture.py").read_text(
        encoding="utf-8"
    )
    runtime = (root / "scripts/module_runtime_state_audit.py").read_text(
        encoding="utf-8"
    )
    test_runner = (root / "scripts/run_test_plan.py").read_text(encoding="utf-8")
    release = (root / ".github/workflows/docker-release.yml").read_text(
        encoding="utf-8"
    )
    updater_health = (root / "scripts/update/services/health.sh").read_text(
        encoding="utf-8"
    )
    billing_surface = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "apps/api/app/routes/billing_parts").glob("*.py"))
    )
    layers_path = root / "scripts/architecture-layers.toml"
    layers_valid = False
    if layers_path.is_file():
        layers = tomllib.loads(layers_path.read_text(encoding="utf-8"))
        layers_valid = (
            layers.get("version") == 1
            and isinstance(layers.get("packages"), list)
            and isinstance(layers.get("rules"), list)
        )
    stable_index = release.find("\n  promote-shared:")
    release_index = release.find("\n  release:")
    checks = {
        "architecture_layers_valid": CheckResult(
            layers_valid,
            "source",
            "scripts/architecture-layers.toml",
        ),
        "architecture_layers_consumed": CheckResult(
            "architecture-layers.toml" in architecture,
            "source",
            "check_architecture.py consumes declarative layers",
        ),
        "runtime_scanner_full_roots": CheckResult(
            '"packages" / "core" / "lumen_core"' in runtime
            and '"apps" / "tgbot" / "app"' in runtime,
            "source",
            "runtime scanner covers Core and TgBot",
        ),
        "rerun_plan_identity": CheckResult(
            "plan_identity" in test_runner
            and "commands that were never executed" in test_runner,
            "source",
            "rerun results bind current plan and command set",
        ),
        "release_tag_main_guard": CheckResult(
            "git merge-base --is-ancestor" in release
            and "origin/main" in release,
            "source",
            "release tag ancestry guard",
        ),
        "stable_alias_before_release": CheckResult(
            0 <= stable_index < release_index,
            "source",
            "stable alias job precedes GitHub Release job",
        ),
        "updater_health_commit": CheckResult(
            updater_health.find("mark_update_committed")
            < updater_health.find("emit_done health_check 0")
            and updater_health.find("mark_update_committed") >= 0,
            "source",
            "commit marker follows health proof",
        ),
        "billing_dynamic_facade_zero": CheckResult(
            all(
                marker not in billing_surface
                for marker in ("globals()", "ContextVar[Any]", "current_runtime")
            ),
            "source",
            "billing dynamic facade markers absent",
        ),
    }
    return checks


def _evidence_checks(
    path: Path,
    *,
    commit: str,
) -> dict[str, CheckResult]:
    if not path.is_file():
        return {}
    payload = _load_json(path)
    if payload.get("version") != 1 or payload.get("commit") != commit:
        raise ValueError("governance evidence is stale or unsupported")
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, dict):
        raise ValueError("governance evidence checks must be an object")
    checks: dict[str, CheckResult] = {}
    for name, entry in raw_checks.items():
        if not isinstance(entry, dict):
            raise ValueError(f"invalid governance evidence check: {name}")
        passed = (
            entry.get("status") == "passed"
            and entry.get("exit_code") == 0
            and isinstance(entry.get("command"), str)
            and bool(entry["command"])
        )
        checks[str(name)] = CheckResult(
            passed=passed,
            source="evidence",
            detail=str(entry.get("command") or entry.get("status") or "missing"),
        )
    return checks


def build_report(
    *,
    root: Path = ROOT,
    evidence_path: Path = DEFAULT_EVIDENCE,
    runner: Runner = _run,
    generated_at: str | None = None,
) -> dict[str, Any]:
    commit = _head_commit(root)
    checks: dict[str, CheckResult] = {}
    checks.update(_command_checks(root, runner))
    checks.update(_known_defect_checks(root))
    checks["ownership_registry_complete"] = _ownership_check(root)
    checks.update(_source_checks(root))
    checks.update(_evidence_checks(evidence_path, commit=commit))

    all_check_names = {
        name for names in DIMENSION_CHECKS.values() for name in names
    } | set(HARD_GATES)
    for name in sorted(all_check_names):
        checks.setdefault(
            name,
            CheckResult(False, "missing", "required evidence is missing"),
        )

    dimensions: dict[str, dict[str, Any]] = {}
    weighted_score = 0.0
    for dimension, weight in WEIGHTS.items():
        names = DIMENSION_CHECKS[dimension]
        passed = sum(1 for name in names if checks[name].passed)
        score = 10.0 * passed / len(names)
        contribution = score * weight
        weighted_score += contribution
        dimensions[dimension] = {
            "checks": list(names),
            "passed": passed,
            "score": round(score, 3),
            "total": len(names),
            "weight": weight,
            "weighted_contribution": round(contribution, 3),
        }

    hard_gate_results = {
        name: checks[name].passed for name in HARD_GATES
    }
    hard_gates_passed = all(hard_gate_results.values())
    return {
        "checks": {name: asdict(result) for name, result in sorted(checks.items())},
        "commit": commit,
        "dimensions": dimensions,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "hard_gate_results": hard_gate_results,
        "hard_gates_passed": hard_gates_passed,
        "schema_version": 1,
        "status": (
            "passed"
            if hard_gates_passed and weighted_score >= 9.0
            else "not_achieved"
        ),
        "weighted_score": round(weighted_score, 3),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Lumen Governance Score",
        "",
        f"- Commit: `{report['commit']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Weighted score: **{report['weighted_score']:.3f}/10**",
        f"- Hard gates: **{'passed' if report['hard_gates_passed'] else 'failed'}**",
        f"- Status: **{report['status']}**",
        "",
        "| Dimension | Weight | Passed | Score | Contribution |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, dimension in report["dimensions"].items():
        lines.append(
            f"| `{name}` | {dimension['weight']:.0%} | "
            f"{dimension['passed']}/{dimension['total']} | "
            f"{dimension['score']:.3f} | "
            f"{dimension['weighted_contribution']:.3f} |"
        )
    lines.extend(["", "## Failed Hard Gates", ""])
    failed = [
        name
        for name, passed in report["hard_gate_results"].items()
        if not passed
    ]
    lines.extend(f"- `{name}`" for name in failed)
    if not failed:
        lines.append("- None")
    lines.extend(["", "## Missing Or Failed Evidence", ""])
    missing = [
        (name, value)
        for name, value in report["checks"].items()
        if not value["passed"]
    ]
    lines.extend(
        f"- `{name}`: {value['detail']} ({value['source']})"
        for name, value in missing
    )
    if not missing:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=DEFAULT_MARKDOWN_OUTPUT,
    )
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="return non-zero unless hard gates pass and score is at least 9.0",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(evidence_path=args.evidence)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    print(
        f"governance score={report['weighted_score']:.3f} "
        f"hard_gates={'passed' if report['hard_gates_passed'] else 'failed'} "
        f"status={report['status']}"
    )
    if args.require_passed and report["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
