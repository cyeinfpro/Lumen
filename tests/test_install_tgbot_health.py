from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "scripts" / "install" / "services.sh"
OPERATIONS = ROOT / "scripts" / "install" / "operations.sh"
RUNTIME = ROOT / "scripts" / "install" / "runtime.sh"
STATE = ROOT / "scripts" / "install" / "state.sh"
COMPOSE = ROOT / "docker-compose.yml"


def _run_bash(
    script: str, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged_env,
        check=False,
    )


def _service_harness(compose_body: str) -> str:
    return f"""
set -euo pipefail
. {shlex.quote(str(SERVICES))}
emit_step_start() {{ :; }}
emit_step_done() {{ :; }}
log_info() {{ :; }}
log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
env_file_get() {{
    [ "$1" = "TELEGRAM_BOT_TOKEN" ] && printf 'configured-token'
}}
_install_compose() {{
    printf '%s\\n' "$*" >> "${{COMPOSE_LOG:?}}"
    {compose_body}
}}
SHARED_DIR="${{TEST_ROOT:?}}"
INSTALL_STARTED_SERVICES=()
INSTALL_TGBOT_STATUS=""
start_application_services
printf 'status=%s\\n' "${{INSTALL_TGBOT_STATUS}}"
printf 'started=%s\\n' "${{INSTALL_STARTED_SERVICES[*]}}"
"""


def test_compose_tgbot_health_uses_runtime_state_not_process_liveness() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    assert 'test: ["CMD", "python", "-m", "app.tgbot_health", "check"]' in compose
    assert "TGBOT_HEALTH_STATE_FILE:" in compose
    assert "TGBOT_HEALTH_READY_STABILITY_SECONDS:" in compose
    assert 'needle = b"\\x00-m\\x00app.main\\x00"' not in compose


def test_configured_tgbot_uses_compose_wait_and_is_tracked(tmp_path: Path) -> None:
    compose_log = tmp_path / "compose.log"
    result = _run_bash(
        _service_harness("return 0"),
        env={"COMPOSE_LOG": str(compose_log), "TEST_ROOT": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr + result.stdout
    calls = compose_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "up --pull missing -d --wait agent-runtime api worker web",
        "--profile tgbot up --pull missing -d --wait tgbot",
    ]
    assert "status=started" in result.stdout
    assert "started=agent-runtime api worker web tgbot" in result.stdout


def test_configured_tgbot_wait_failure_aborts_install(tmp_path: Path) -> None:
    compose_log = tmp_path / "compose.log"
    compose_body = """
case "$*" in
    *"--profile tgbot"*) return 1 ;;
    *) return 0 ;;
esac
"""
    result = _run_bash(
        _service_harness(compose_body),
        env={"COMPOSE_LOG": str(compose_log), "TEST_ROOT": str(tmp_path)},
    )

    assert result.returncode != 0
    assert "tgbot 启动或健康检查失败" in result.stderr
    assert "status=started" not in result.stdout


@pytest.mark.parametrize(
    ("function_name", "expected"),
    (
        ("start_infrastructure", "postgres redis"),
        ("start_application_services", "agent-runtime api worker web"),
    ),
)
def test_partial_compose_start_is_tracked_before_wait_can_fail(
    tmp_path: Path,
    function_name: str,
    expected: str,
) -> None:
    result = _run_bash(
        f"""
set -euo pipefail
. {shlex.quote(str(SERVICES))}
emit_step_start() {{ :; }}
emit_step_done() {{ :; }}
log_info() {{ :; }}
log_error() {{ :; }}
postgres_data_initialized() {{ return 1; }}
env_file_get() {{ printf ''; }}
_install_compose() {{ return 1; }}
INSTALL_STARTED_SERVICES=()
INSTALL_POSTGRES_DATA_PREEXISTING=0
SHARED_DIR={shlex.quote(str(tmp_path))}
trap 'printf "started=%s\\n" "${{INSTALL_STARTED_SERVICES[*]-}}"' EXIT
{function_name}
""",
    )

    assert result.returncode != 0
    assert f"started={expected}" in result.stdout


def test_install_cleanup_masks_second_signal_until_rollback_finishes(
    tmp_path: Path,
) -> None:
    state = ROOT / "scripts" / "install" / "state.sh"
    completed = tmp_path / "completed"
    result = _run_bash(
        f"""
set -u
. {shlex.quote(str(state))}
log_error() {{ :; }}
log_warn() {{ :; }}
lumen_emit_step() {{ :; }}
lumen_release_lock() {{ :; }}
restore_install_state_snapshot() {{
    kill -TERM "$$"
    : > {shlex.quote(str(completed))}
}}
discard_install_state_snapshot() {{ :; }}
trap 'exit 143' TERM
INSTALL_GHCR_PROBE_FILE=""
INSTALL_PHASE=test
INSTALL_STARTED_SERVICES=()
INSTALL_STATE_SNAPSHOT_READY=1
INSTALL_ENV_SNAPSHOT=""
INSTALL_HOST_ARTIFACT_SNAPSHOT=""
RELEASE_DIR=""
SHARED_DIR={shlex.quote(str(tmp_path))}
DEPLOY_ROOT={shlex.quote(str(tmp_path))}
false
cleanup_on_failure
""",
    )

    assert result.returncode != 143, result.stderr + result.stdout
    assert completed.exists()


@pytest.mark.parametrize("tgbot_state", ["unhealthy", "exited"])
def test_final_health_fails_if_tgbot_degrades_after_successful_up(
    tmp_path: Path,
    tgbot_state: str,
) -> None:
    result = _run_bash(
        f"""
set -euo pipefail
. {shlex.quote(str(RUNTIME))}
. {shlex.quote(str(SERVICES))}
. {shlex.quote(str(OPERATIONS))}
emit_step_start() {{ :; }}
emit_step_done() {{ :; }}
log_info() {{ printf 'INFO:%s\\n' "$*"; }}
log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
_install_health_http() {{ return 0; }}
env_file_get() {{
    [ "$1" = "TELEGRAM_BOT_TOKEN" ] && printf 'configured-token'
}}
_install_compose() {{
    if [ "${{1:-}}" != "ps" ]; then
        return 0
    fi
    local service="${{@: -1}}"
    if [ "$service" = "tgbot" ] && [ {shlex.quote(tgbot_state)} = "exited" ]; then
        return 0
    fi
    printf 'cid-%s\\n' "$service"
}}
docker() {{
    local container="${{@: -1}}"
    case "$container" in
        *tgbot*) printf '%s\\n' {shlex.quote(tgbot_state)} ;;
        *) printf 'healthy\\n' ;;
    esac
}}
sleep() {{ :; }}
SHARED_DIR={shlex.quote(str(tmp_path))}
COMPOSE_LABEL="docker compose"
LUMEN_HEALTH_COMPOSE_ATTEMPTS=1
LUMEN_HEALTH_COMPOSE_INTERVAL=1
INSTALL_STARTED_SERVICES=()
INSTALL_TGBOT_STATUS=""
start_application_services
run_health_checks
""",
    )

    assert result.returncode != 0
    assert "compose service 健康状态异常" in result.stderr
    assert "全部健康" not in result.stdout


def test_final_health_uses_readyz_not_healthz(tmp_path: Path) -> None:
    probes = tmp_path / "probes.log"
    result = _run_bash(
        f"""
set -euo pipefail
. {shlex.quote(str(RUNTIME))}
. {shlex.quote(str(OPERATIONS))}
emit_step_start() {{ :; }}
emit_step_done() {{ :; }}
log_info() {{ :; }}
log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
_install_health_http() {{
    printf '%s\\n' "$1" >> {shlex.quote(str(probes))}
    case "$1" in
        */healthz) return 0 ;;
        */readyz) return 1 ;;
        *) return 0 ;;
    esac
}}
_install_compose() {{ return 0; }}
SHARED_DIR={shlex.quote(str(tmp_path))}
RELEASE_DIR={shlex.quote(str(tmp_path))}
COMPOSE_LABEL="docker compose"
LUMEN_INSTALL_CORE_READINESS_ATTEMPTS=1
LUMEN_INSTALL_CORE_READINESS_INTERVAL_SECONDS=0
run_health_checks
""",
    )

    assert result.returncode != 0
    assert probes.read_text(encoding="utf-8").splitlines() == [
        "http://127.0.0.1:8000/readyz"
    ]
    assert "核心 readiness 失败" in result.stderr


def test_complete_install_journal_is_retained_when_readyz_fails(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    journal = deploy_root / ".install-transaction"
    journal.mkdir(parents=True)
    (journal / "phase").write_text("complete\n", encoding="utf-8")
    result = _run_bash(
        f"""
set -euo pipefail
. {shlex.quote(str(RUNTIME))}
. {shlex.quote(str(STATE))}
log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
_install_core_readiness() {{ return 1; }}
lumen_fsync_directory() {{ return 0; }}
DEPLOY_ROOT={shlex.quote(str(deploy_root))}
RELEASE_DIR={shlex.quote(str(tmp_path / "release"))}
INSTALL_JOURNAL_DIR={shlex.quote(str(journal))}
install_transaction_cleanup
""",
    )

    assert result.returncode != 0
    assert journal.is_dir()
    assert "保留 journal" in result.stderr
