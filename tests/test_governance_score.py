from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_governance_score",
    ROOT / "scripts" / "governance_score.py",
)
assert SPEC is not None and SPEC.loader is not None
governance_score = module_from_spec(SPEC)
sys.modules[SPEC.name] = governance_score
SPEC.loader.exec_module(governance_score)


def _runner(
    command: list[str] | tuple[str, ...],
    _cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "passed\n", "")


def test_current_report_fails_closed_without_dynamic_evidence() -> None:
    report = governance_score.build_report(
        root=ROOT,
        evidence_path=ROOT / ".audit_state/missing-governance-evidence.json",
        runner=_runner,
        generated_at="2026-07-29T00:00:00+00:00",
    )

    assert report["status"] == "not_achieved"
    assert report["hard_gates_passed"] is False
    assert report["checks"]["full_tests"]["passed"] is False
    assert report["checks"]["known_p0_zero"]["passed"] is True
    assert report["checks"]["known_p1_zero"]["passed"] is True
    assert report["weighted_score"] < 9.0


def test_stale_evidence_is_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": "f" * 40,
                "checks": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale or unsupported"):
        governance_score.build_report(
            root=ROOT,
            evidence_path=evidence,
            runner=_runner,
        )


def test_evidence_requires_command_and_zero_exit(tmp_path: Path) -> None:
    commit = governance_score._head_commit(ROOT)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": commit,
                "checks": {
                    "full_tests": {
                        "command": "uv run pytest -q",
                        "exit_code": 0,
                        "status": "passed",
                    },
                    "fault_matrix": {
                        "command": "",
                        "exit_code": 0,
                        "status": "passed",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    checks = governance_score._evidence_checks(evidence, commit=commit)

    assert checks["full_tests"].passed is True
    assert checks["fault_matrix"].passed is False


def test_markdown_lists_failed_hard_gates() -> None:
    report = governance_score.build_report(
        root=ROOT,
        evidence_path=ROOT / ".audit_state/missing-governance-evidence.json",
        runner=_runner,
        generated_at="2026-07-29T00:00:00+00:00",
    )

    markdown = governance_score.render_markdown(report)

    assert "# Lumen Governance Score" in markdown
    assert "Failed Hard Gates" in markdown
    assert "`full_tests`" in markdown
    assert "`release_proof`" in markdown
