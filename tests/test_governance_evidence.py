from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_governance_evidence",
    ROOT / "scripts" / "governance_evidence.py",
)
assert SPEC is not None and SPEC.loader is not None
governance_evidence = module_from_spec(SPEC)
sys.modules[SPEC.name] = governance_evidence
SPEC.loader.exec_module(governance_evidence)


def test_unknown_check_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown governance evidence"):
        governance_evidence.run_checks(
            ["not_registered"],
            root=ROOT,
            output=tmp_path / "evidence.json",
        )


def test_stale_evidence_is_not_inherited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "evidence.json"
    output.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": "f" * 40,
                "checks": {"release_proof": {"status": "passed"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        governance_evidence,
        "_run_command",
        lambda _command, root: subprocess.CompletedProcess([], 0, "ok\n", ""),
    )

    payload, passed = governance_evidence.run_checks(
        ["dead_code_zero"],
        root=ROOT,
        output=output,
    )

    assert passed is True
    assert "release_proof" not in payload["checks"]
    assert payload["checks"]["dead_code_zero"]["status"] == "passed"


def test_failed_command_is_persisted_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        governance_evidence,
        "_run_command",
        lambda _command, root: subprocess.CompletedProcess([], 17, "", "failed\n"),
    )

    payload, passed = governance_evidence.run_checks(
        ["documentation_freshness"],
        root=ROOT,
        output=tmp_path / "evidence.json",
    )

    check = payload["checks"]["documentation_freshness"]
    assert passed is False
    assert check["exit_code"] == 17
    assert check["status"] == "failed"


def test_release_proof_is_explicitly_post_release_only() -> None:
    assert "release_proof" in governance_evidence.CHECK_COMMANDS
    assert "release_proof" not in governance_evidence.DEFAULT_CHECKS
