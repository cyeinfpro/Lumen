from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "scripts" / "update" / "journal.sh"
CONTRACT = ROOT / "scripts" / "update" / "phase_contract.sh"
RUNNER = ROOT / "scripts" / "update" / "runner.sh"


def _run(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_update_journal_records_phase_failure_and_explicit_resume(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    journal = shared / "journal.json"
    first = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-original
        LUMEN_UPDATE_RESUME=0
        CURRENT_ID=release-old
        TARGET_TAG=v9.9.9
        lumen_update_journal_init
        lumen_update_journal_phase_start check
        lumen_update_journal_phase_done check
        lumen_update_journal_phase_start preflight
        lumen_update_journal_failed preflight 41
        """
    )
    assert first.returncode == 0, first.stderr + first.stdout

    failed = json.loads(journal.read_text(encoding="utf-8"))
    assert failed["operation_id"] == "update-original"
    assert failed["status"] == "failed"
    assert failed["completed_phases"] == ["check"]
    assert failed["last_error"]["phase"] == "preflight"
    assert failed["last_error"]["return_code"] == 41
    assert failed["context"]["TARGET_TAG"] == "v9.9.9"

    resumed = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-new
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        printf 'operation=%s resumed=%s target=%s\\n' \
            "$OPERATION_ID" "$LUMEN_UPDATE_JOURNAL_RESUMED" "$TARGET_TAG"
        lumen_update_journal_status complete
        """
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    assert "operation=update-original resumed=1 target=v9.9.9" in resumed.stdout
    completed = json.loads(journal.read_text(encoding="utf-8"))
    assert completed["status"] == "complete"
    assert completed["resume_count"] == 1


def test_update_journal_records_rollback_terminal_state(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-rollback
        lumen_update_journal_init
        lumen_update_journal_phase_start switch
        lumen_update_journal_failed switch 1
        lumen_update_journal_status rolled_back
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "rolled_back"
    assert payload["current_phase"] is None


def test_update_failpoints_support_before_after_and_alias_forms(
    tmp_path: Path,
) -> None:
    script = f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        LUMEN_UPDATE_FAILPOINT="$1"
        rc=0
        lumen_update_failpoint "$2" "$3" || rc=$?
        printf 'rc=%s\\n' "$rc"
    """
    for configured, timing, phase in (
        ("before:check", "before", "check"),
        ("check:before", "before", "check"),
        ("check", "before", "check"),
        ("after:check", "after", "check"),
    ):
        result = subprocess.run(
            ["/bin/bash", "-c", script, "bash", configured, timing, phase],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env={**os.environ, "SHARED_DIR": str(tmp_path)},
            check=False,
        )
        assert result.returncode == 0
        assert "rc=97" in result.stdout
        assert f"{timing}:{phase}" in result.stderr


def test_failpoint_records_the_exact_nested_phase(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-nested-failpoint
        lumen_update_journal_init
        LUMEN_UPDATE_FAILPOINT=after:start_green
        rc=0
        lumen_update_failpoint after start_green || rc=$?
        test "$rc" -eq 97
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["last_error"]["phase"] == "start_green"


def test_phase_contract_contains_stable_public_update_protocol() -> None:
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(CONTRACT))}
        for phase in lock check fetch_release migrate_db switch \
                restart_services start_green shift_traffic_100 \
                health_check cleanup; do
            lumen_update_phase_is_known "$phase"
        done
        ! lumen_update_phase_is_known arbitrary_new_phase
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_resume_query_reports_completed_phases(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    result = _run(
        f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        LUMEN_UPDATE_JOURNAL={shlex.quote(str(journal))}
        OPERATION_ID=update-resume-query
        lumen_update_journal_init
        lumen_update_journal_phase_start fetch_release
        lumen_update_journal_phase_done fetch_release
        lumen_update_journal_phase_completed fetch_release
        ! lumen_update_journal_phase_completed switch
        """
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_update_modules_are_domain_split_and_below_600_lines() -> None:
    update_dir = ROOT / "scripts" / "update"
    modules = sorted(update_dir.rglob("*.sh"))

    assert {path.parent.name for path in modules} >= {
        "backup",
        "recovery",
        "release",
        "services",
        "update",
    }
    assert all(
        len(path.read_text(encoding="utf-8").splitlines()) < 600 for path in modules
    )

    runner = RUNNER.read_text(encoding="utf-8")
    assert "update_run_phase" in runner
    for implementation_detail in (
        "docker compose",
        "lumen_compose_in",
        "rsync ",
        "alembic ",
        "curl ",
    ):
        assert implementation_detail not in runner


def test_self_update_unit_contains_every_update_module() -> None:
    update_dir = ROOT / "scripts" / "update"
    source = (update_dir / "release" / "self_update.sh").read_text(encoding="utf-8")
    expected = {
        path.relative_to(ROOT / "scripts").as_posix()
        for path in update_dir.rglob("*.sh")
    }

    assert expected
    assert all(relative in source for relative in expected)


def test_journal_contract_covers_every_emitted_phase() -> None:
    update_dir = ROOT / "scripts" / "update"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(update_dir.rglob("*.sh"))
    )
    emitted = set(__import__("re").findall(r"emit_start\s+([a-z][a-z0-9_]*)", source))
    emitted.add("cleanup")
    contract = CONTRACT.read_text(encoding="utf-8")

    assert emitted
    assert all(f"\n{phase}\n" in contract for phase in emitted)
