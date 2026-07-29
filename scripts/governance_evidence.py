#!/usr/bin/env python3
"""Run commit-bound governance evidence commands and persist their results."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".audit_state" / "governance-evidence.json"

CHECK_COMMANDS = {
    "dead_code_zero": (
        "uv run python scripts/dead_code_audit.py",
    ),
    "documentation_freshness": (
        "uv run pytest -q tests/test_governance_score.py "
        "tests/test_facade_inventory.py tests/test_baseline_monotonic.py",
    ),
    "fault_matrix": (
        "uv run pytest -q apps/worker/tests/test_image_job_execution_boundary.py "
        "apps/worker/tests/test_outbox_reconciliation_contracts.py "
        "tests/test_promote_release_images.py tests/test_update_state_machine.py",
    ),
    "full_tests": ("bash scripts/test.sh",),
    "migration_faults": (
        "uv run pytest -q tests/test_update_migration_backup.py "
        "tests/test_update_state_machine.py "
        "apps/api/tests/test_alembic_env.py",
    ),
    "observability_metrics": (
        "uv run pytest -q apps/worker/tests/test_context_summary_prometheus.py "
        "apps/worker/tests/test_provider_pool_image_route.py "
        "apps/api/tests/test_core_security_infra.py",
    ),
    "recovery_proof": (
        "uv run pytest -q apps/worker/tests/test_image_job_execution_boundary.py "
        "apps/worker/tests/test_outbox_reconciliation_contracts.py "
        "tests/test_update_state_machine.py",
    ),
    "release_faults": (
        "uv run pytest -q tests/test_promote_release_images.py "
        "tests/test_release_manifest.py tests/test_update_state_machine.py",
    ),
    "sidecar_delivery_faults": (
        "uv run pytest -q apps/worker/tests/test_image_job_execution_boundary.py "
        "-k 'succeeded_retry_is_delivery_only or billing_never_releases'",
    ),
    "sidecar_recovery_faults": (
        "uv run pytest -q apps/worker/tests/test_image_job_execution_boundary.py "
        "-k 'accepted_timeout or progress_persists or typed_recovery'",
    ),
    "signed_images": (
        "uv run pytest -q tests/test_release_manifest.py "
        "-k 'signed or promotes_aliases_only_after_all_signed_builds'",
    ),
    "storage_consistency": (
        "uv run pytest -q apps/api/tests/images/test_artifact_saga.py "
        "apps/api/tests/images/test_artifact_runtime_regressions.py "
        "apps/worker/tests/test_storage.py image-job/tests/test_app_hardening.py "
        "image-job/tests/test_cancellation_fencing.py "
        "image-job/tests/test_retention_budgets.py",
    ),
    "supply_chain": (
        "uv run pytest -q tests/test_release_manifest.py "
        "tests/test_lumenctl_scripts.py -k 'cosign or release_manifest'",
    ),
    "web_domain_boundaries": (
        "cd apps/web && npm run check:architecture && "
        "npm test -- src/features/featureArchitecture.test.ts "
        "src/app/video/page.test.ts",
    ),
    "web_isolation": (
        "cd apps/web && npm test -- "
        "src/features/realtime/model/runtime.test.ts "
        "src/lib/runtimeResilience.test.ts src/app/video/page.test.ts "
        "__tests__/chat-hardening.test.mjs",
    ),
}


def _head_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "cannot resolve HEAD")
    return result.stdout.strip()


def _load_existing(path: Path, *, commit: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or payload.get("commit") != commit:
        return {}
    checks = payload.get("checks")
    return dict(checks) if isinstance(checks, dict) else {}


def _run_command(command: str, *, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def run_checks(
    names: Sequence[str],
    *,
    root: Path = ROOT,
    output: Path = DEFAULT_OUTPUT,
) -> tuple[dict[str, Any], bool]:
    unknown = sorted(set(names) - set(CHECK_COMMANDS))
    if unknown:
        raise ValueError(f"unknown governance evidence checks: {', '.join(unknown)}")
    commit = _head_commit(root)
    checks = _load_existing(output, commit=commit)
    all_passed = True
    for name in names:
        command = " && ".join(CHECK_COMMANDS[name])
        result = _run_command(command, root=root)
        output_lines = ((result.stdout or "") + (result.stderr or "")).splitlines()
        checks[name] = {
            "command": command,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "detail": output_lines[-1] if output_lines else f"exit={result.returncode}",
            "exit_code": result.returncode,
            "status": "passed" if result.returncode == 0 else "failed",
        }
        all_passed = all_passed and result.returncode == 0
    payload = {"version": 1, "commit": commit, "checks": checks}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload, all_passed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="append",
        choices=sorted(CHECK_COMMANDS),
        dest="checks",
    )
    parser.add_argument("--list", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        for name in sorted(CHECK_COMMANDS):
            print(f"{name}: {shlex.join(['bash', '-lc', ' && '.join(CHECK_COMMANDS[name])])}")
        return 0
    names = args.check or sorted(CHECK_COMMANDS)
    payload, passed = run_checks(names, output=args.output)
    for name in names:
        check = payload["checks"][name]
        print(f"{name}: {check['status']} ({check['detail']})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
