from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
JOURNAL = ROOT / "scripts/update/journal.sh"


def run_shell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("reexec", [False, True])
def test_manual_update_keeps_durable_journal_without_waking_api_runner(tmp_path: Path, reexec: bool) -> None:
    result = run_shell(f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        OPERATION_ID=manual-update
        unset LUMEN_UPDATE_REQUEST_SHA256 LUMEN_UPDATE_RECOVERY_MARKER LUMEN_UPDATE_JOURNAL
        LUMEN_UPDATE_API_OPERATION_ID={'manual-update' if reexec else ''}
        lumen_update_journal_init
        test ! -e "$SHARED_DIR/.update-resume"
        LUMEN_UPDATE_RESUME=1
        lumen_update_journal_init
        test "$LUMEN_UPDATE_JOURNAL_RESUMED" = 1
        test ! -e "$SHARED_DIR/.update-resume"
    """)
    assert result.returncode == 0, result.stderr + result.stdout
    journal = json.loads((tmp_path / ".update-journal.json").read_text())
    assert journal["operation_id"] == "manual-update"
    assert journal["status"] == "running"


def test_rejected_journal_init_never_publishes_a_new_wake_up(tmp_path: Path) -> None:
    result = run_shell(f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        unset LUMEN_UPDATE_REQUEST_SHA256 LUMEN_UPDATE_RECOVERY_MARKER LUMEN_UPDATE_JOURNAL
        OPERATION_ID=original-update
        lumen_update_journal_init
        OPERATION_ID=new-update
        LUMEN_UPDATE_API_OPERATION_ID=new-update
        LUMEN_UPDATE_REQUEST_SHA256={'a' * 64}
        lumen_update_journal_init
    """)
    assert result.returncode != 0
    assert not (tmp_path / ".update-resume").exists()
    assert json.loads((tmp_path / ".update-journal.json").read_text())["operation_id"] == "original-update"


def test_api_wake_up_is_published_after_matching_journal(tmp_path: Path) -> None:
    result = run_shell(f"""
        set -euo pipefail
        . {shlex.quote(str(JOURNAL))}
        SHARED_DIR={shlex.quote(str(tmp_path))}
        OPERATION_ID=api-update
        lumen_update_recovery_marker_write() {{
            python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["operation_id"] == sys.argv[2]' \
                "$SHARED_DIR/.update-journal.json" "$OPERATION_ID"
        }}
        lumen_update_journal_init
    """)
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize("reset_failure", [False, True])
def test_refresh_rearms_only_update_watcher_without_restarting_runner(tmp_path: Path, reset_failure: bool) -> None:
    templates = tmp_path / "current/deploy/systemd"
    templates.mkdir(parents=True)
    for name in ("lumen-update.path", "lumen-update-runner.service"):
        (templates / name).write_text((ROOT / "deploy/systemd" / name).read_text())
    calls = tmp_path / "systemctl.log"
    result = run_shell(f"""
        set -euo pipefail
        ROOT={shlex.quote(str(tmp_path))}
        UPDATE_LOG_DIR="$ROOT"
        LUMEN_DATA_ROOT="$ROOT/data"
        LUMEN_SYSTEMD_UNIT_DIR="$ROOT/units"
        lumen_systemd_runtime_available() {{ return 0; }}
        lumen_ensure_backup_service_user() {{ return 0; }}
        lumen_install_optional_systemd_unit() {{ return 0; }}
        lumen_enable_optional_systemd_unit() {{ return 0; }}
        sed_replacement_escape() {{ printf '%s' "$1"; }}
        log_info() {{ :; }}; log_warn() {{ :; }}; log_error() {{ :; }}
        emit_info() {{ :; }}; emit_warn() {{ :; }}
        lumen_run_as_root() {{
            if [ "$1" = systemctl ]; then
                shift
                printf '%s\n' "$*" >> {shlex.quote(str(calls))}
                if [ "$1" = reset-failed ] && [ {int(reset_failure)} = 1 ]; then return 1; fi
            fi
            return 0
        }}
        . {shlex.quote(str(ROOT / 'scripts/update/release/runner_units.sh'))}
        refresh_update_runner_units
    """)
    actual = calls.read_text().splitlines()
    reset = "reset-failed lumen-update.path lumen-update-runner.service"
    assert reset in actual
    assert not any(line.startswith("restart lumen-update-runner") for line in actual)
    if reset_failure:
        assert result.returncode != 0
        assert "enable --now lumen-update.path" not in actual
    else:
        assert result.returncode == 0, result.stderr + result.stdout
        assert actual.index(reset) < actual.index("enable --now lumen-update.path")
        assert "restart lumen-update.path" in actual


def test_success_proof_is_emitted_only_after_final_readiness_and_durable_commit() -> None:
    runner = (ROOT / "scripts/update/runner.sh").read_text()
    proof = 'lumen_emit_step "phase=complete"'
    assert runner.rindex('lumen_update_wait_for_core_ready') < runner.index(proof)
    assert runner.index('lumen_update_journal_status complete') < runner.index(proof)
