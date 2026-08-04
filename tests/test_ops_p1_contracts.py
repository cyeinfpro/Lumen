from __future__ import annotations

import os
from pathlib import Path
import pwd
import shlex
import shutil
import signal
import stat
import subprocess
import time

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
INSTALL = ROOT / "scripts" / "install.sh"
BACKUP = ROOT / "scripts" / "backup.sh"
RESTORE = ROOT / "scripts" / "restore.sh"
UPDATE_RUNNER = ROOT / "scripts" / "update" / "runner.sh"
UPDATE_COMPOSE = ROOT / "scripts" / "update" / "services" / "compose.sh"
UPDATE_ACTIVATION = ROOT / "scripts" / "update" / "services" / "release_activation.sh"
BACKUP_RESTORE_SERVICES = ROOT / "scripts" / "lib" / "backup_restore_services.sh"
STORAGE_IDENTITY = ROOT / "scripts" / "update" / "backup" / "storage_identity.sh"
MIGRATE = ROOT / "scripts" / "migrate_to_releases.sh"
COMPOSE = ROOT / "docker-compose.yml"


def _run_bash(
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
        check=False,
    )


def _without_flock_source(script: Path, *args: str) -> list[str]:
    body = """
command() {
    if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
        return 1
    fi
    builtin command "$@"
}
source_file="$1"
shift
. "$source_file" "$@"
"""
    return ["/bin/bash", "-c", body, "ops-p1-test", str(script), *args]


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _environment_with_test_flock(tmp_path: Path) -> dict[str, str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    flock = fakebin / "flock"
    flock.write_text(
        """#!/usr/bin/env python3
import fcntl
import sys

operation = sys.argv[1]
descriptor = int(sys.argv[2])
try:
    if operation == "-n":
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif operation == "-u":
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    else:
        raise SystemExit(2)
except BlockingIOError:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    flock.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fakebin}:{environment['PATH']}"
    return environment


def test_deploy_root_resolver_accepts_release_current_symlink(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    release = deploy_root / "releases" / "release-1"
    scripts_dir = release / "scripts"
    scripts_dir.mkdir(parents=True)
    (deploy_root / "current").symlink_to(Path("releases") / release.name)

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_resolve_deploy_root \
            {shlex.quote(str(deploy_root / "current" / "scripts"))} "" ""
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert Path(result.stdout.strip()) == deploy_root


def test_deploy_root_resolver_rejects_traversal_and_escaping_release_link(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "lumen"
    outside_release = tmp_path / "outside-release"
    deploy_root.mkdir()
    (deploy_root / "releases").mkdir()
    outside_release.mkdir()
    (deploy_root / "current").symlink_to(outside_release)

    traversal = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_resolve_deploy_root \
            {shlex.quote(str(ROOT / "scripts"))} \
            {shlex.quote(str(deploy_root / ".." / deploy_root.name))} ""
        """
    )
    escaping = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_resolve_deploy_root \
            {shlex.quote(str(ROOT / "scripts"))} \
            {shlex.quote(str(deploy_root))} ""
        """
    )

    assert traversal.returncode != 0
    assert "traversal" in traversal.stderr
    assert escaping.returncode != 0
    assert "current escapes" in escaping.stderr


def test_shared_env_lookup_stays_inside_custom_deploy_root(tmp_path: Path) -> None:
    deploy_root = tmp_path / "custom-deploy"
    shared_env = deploy_root / "shared" / ".env"
    shared_env.parent.mkdir(parents=True)
    shared_env.write_text("DB_NAME=lumen\n", encoding="utf-8")

    result = _run_bash(
        f"""
        set -euo pipefail
        LUMEN_DEPLOY_ROOT={shlex.quote(str(deploy_root))}
        . {shlex.quote(str(LIB))}
        lumen_find_shared_env ""
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert Path(result.stdout.strip()) == shared_env


def test_install_root_resolver_creates_only_canonical_non_symlink_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fresh" / "lumen"
    resolved = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_resolve_install_deploy_root \
            {shlex.quote(str(ROOT / "scripts"))} \
            {shlex.quote(str(target))}
        """
    )

    assert resolved.returncode == 0, resolved.stderr + resolved.stdout
    assert Path(resolved.stdout.strip()) == target
    assert target.is_dir()

    traversal = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_resolve_install_deploy_root \
            {shlex.quote(str(ROOT / "scripts"))} \
            {shlex.quote(str(target / ".." / target.name))}
        """
    )
    assert traversal.returncode != 0
    assert "traversal" in traversal.stderr

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    ambiguous = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_resolve_install_deploy_root \
            {shlex.quote(str(ROOT / "scripts"))} \
            {shlex.quote(str(symlink_parent / "lumen"))}
        """
    )
    assert ambiguous.returncode != 0
    assert "symlink" in ambiguous.stderr


def test_target_lock_blocks_install_from_a_different_checkout(tmp_path: Path) -> None:
    checkout_a = tmp_path / "checkout-a"
    checkout_b = tmp_path / "checkout-b"
    shutil.copytree(ROOT / "scripts", checkout_a / "scripts", symlinks=True)
    shutil.copytree(ROOT / "scripts", checkout_b / "scripts", symlinks=True)
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir()
    ready = tmp_path / "install-lock-ready"

    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            """
            set -euo pipefail
            . "$1"
            lumen_acquire_lock "$2" checkout-a-install
            : > "$3"
            while :; do sleep 0.1; done
            """,
            "install-lock-holder",
            str(checkout_a / "scripts" / "lib.sh"),
            str(deploy_root),
            str(ready),
        ],
        cwd=checkout_a,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        env = os.environ.copy()
        env.update(
            {
                "LUMEN_DATA_ROOT": str(tmp_path / "data"),
                "LUMEN_DB_ROOT": str(tmp_path / "data"),
                "LUMEN_DEPLOY_ROOT": str(deploy_root),
                "LUMEN_NONINTERACTIVE": "1",
                "LUMEN_SELF_UPDATE": "0",
            }
        )
        attempted = subprocess.run(
            [
                "/bin/bash",
                str(checkout_b / "scripts" / "install.sh"),
                "--install",
                "--image-tag=main",
            ],
            cwd=checkout_b,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
            check=False,
        )
    finally:
        holder.terminate()
        holder_stdout, holder_stderr = holder.communicate(timeout=5)

    assert holder.returncode in {-15, 143}, holder_stderr + holder_stdout
    assert attempted.returncode != 0
    assert "已有 Lumen 维护脚本" in attempted.stderr
    assert not (checkout_b / ".lumen-maintenance.lock").exists()
    assert not (checkout_b / ".lumen-maintenance.lock.d").exists()
    assert not (deploy_root / "releases").exists()
    assert not (deploy_root / "shared").exists()


def test_borrowed_mkdir_lock_requires_live_owner_and_secret(tmp_path: Path) -> None:
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir()
    result = _run_bash(
        f"""
        set -euo pipefail
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        . {shlex.quote(str(LIB))}
        lumen_acquire_lock {shlex.quote(str(deploy_root))} updater
        lumen_export_borrowed_maintenance_lock {shlex.quote(str(deploy_root))}
        /bin/bash -c '
            command() {{
                if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                    return 1
                fi
                builtin command "$@"
            }}
            . "$1"
            lumen_verify_borrowed_maintenance_lock "$2"
        ' borrowed-child {shlex.quote(str(LIB))} {shlex.quote(str(deploy_root))}
        if LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY=forged \
                /bin/bash -c '
                    command() {{
                        if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                            return 1
                        fi
                        builtin command "$@"
                    }}
                    . "$1"
                    lumen_verify_borrowed_maintenance_lock "$2"
                ' forged-child {shlex.quote(str(LIB))} \
                    {shlex.quote(str(deploy_root))}; then
            exit 91
        fi
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_reexec_adopts_the_same_maintenance_lock_without_a_gap(
    tmp_path: Path,
) -> None:
    environment = _environment_with_test_flock(tmp_path)
    for force_no_flock, expected_kind in (("0", "flock"), ("1", "mkdir")):
        deploy_root = tmp_path / f"deploy-{expected_kind}"
        deploy_root.mkdir()
        result = _run_bash(
            f"""
            set -euo pipefail
            if [ {force_no_flock} = 1 ]; then
                command() {{
                    if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                        return 1
                    fi
                    builtin command "$@"
                }}
            fi
            . {shlex.quote(str(LIB))}
            lumen_acquire_lock {shlex.quote(str(deploy_root))} old-updater
            lumen_export_borrowed_maintenance_lock \
                {shlex.quote(str(deploy_root))}
            export LUMEN_UPDATE_SELF_UPDATED=1
            exec /bin/bash -c '
                set -euo pipefail
                if [ "$3" = 1 ]; then
                    command() {{
                        if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                            return 1
                        fi
                        builtin command "$@"
                    }}
                fi
                . "$1"
                lumen_acquire_lock "$2" new-updater
                test "$LUMEN_LOCK_KIND" = "$4"
                test -z "${{LUMEN_BORROWED_MAINTENANCE_LOCK_KIND:-}}"
                if (
                    unset LUMEN_LOCK_KIND LUMEN_LOCK_PATH \
                        LUMEN_LOCK_OWNER_TOKEN LUMEN_LOCK_OWNER_CAPABILITY
                    lumen_clear_borrowed_maintenance_lock
                    exec 9>&-
                    lumen_try_acquire_lock "$2" contender
                ); then
                    exit 91
                fi
                lumen_release_lock
            ' reexec {shlex.quote(str(LIB))} \
                {shlex.quote(str(deploy_root))} {force_no_flock} \
                {expected_kind}
            """,
            env=environment,
        )

        assert result.returncode == 0, result.stderr + result.stdout
        assert not (deploy_root / ".lumen-maintenance.lock.d").exists()


@pytest.mark.parametrize("force_no_flock", (True, False))
def test_maintenance_root_swap_cannot_bypass_parent_anchor(
    tmp_path: Path,
    force_no_flock: bool,
) -> None:
    deploy_root = tmp_path / "deploy"
    displaced = tmp_path / "displaced-deploy"
    ready = tmp_path / "ready"
    deploy_root.mkdir()
    environment = _environment_with_test_flock(tmp_path)
    force = "1" if force_no_flock else "0"
    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            if [ {force} = 1 ]; then
                command() {{
                    if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                        return 1
                    fi
                    builtin command "$@"
                }}
            fi
            . "$1"
            lumen_acquire_lock "$2" holder
            : > "$3"
            while :; do sleep 0.1; done
            """,
            "root-swap-holder",
            str(LIB),
            str(deploy_root),
            str(ready),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        deploy_root.rename(displaced)
        deploy_root.mkdir(mode=0o777)
        deploy_root.chmod(0o777)
        replacement = deploy_root / "replacement-unsafe"
        replacement.write_text("keep\n", encoding="utf-8")
        replacement.chmod(0o777)
        contender = _run_bash(
            f"""
            set -euo pipefail
            if [ {force} = 1 ]; then
                command() {{
                    if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                        return 1
                    fi
                    builtin command "$@"
                }}
            fi
            . {shlex.quote(str(LIB))}
            lumen_try_acquire_lock {shlex.quote(str(deploy_root))} contender
            """,
            env=environment,
        )
    finally:
        if holder.poll() is None:
            os.killpg(holder.pid, signal.SIGTERM)
        holder_stdout, holder_stderr = holder.communicate(timeout=5)

    assert holder.returncode in {-signal.SIGTERM, 143}, holder_stderr + holder_stdout
    assert contender.returncode != 0
    assert "replacement-unsafe" in replacement.name
    assert replacement.read_text(encoding="utf-8") == "keep\n"
    assert replacement.stat().st_mode & 0o777 == 0o777
    assert displaced.is_dir()
    assert not (deploy_root / ".lumen-maintenance.lock.d").exists()


def test_failed_flock_contender_does_not_truncate_owner_proof(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    ready = tmp_path / "ready"
    deploy_root.mkdir()
    environment = _environment_with_test_flock(tmp_path)
    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            """
            set -euo pipefail
            . "$1"
            lumen_acquire_lock "$2" holder
            lumen_export_borrowed_maintenance_lock "$2"
            cp "$2/.lumen-maintenance.lock" "$3"
            : > "$4"
            while :; do sleep 0.1; done
            """,
            "flock-holder",
            str(LIB),
            str(deploy_root),
            str(tmp_path / "owner-before"),
            str(ready),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        contender = _run_bash(
            f"""
            set -euo pipefail
            . {shlex.quote(str(LIB))}
            if lumen_try_acquire_lock \
                    {shlex.quote(str(deploy_root))} contender; then
                exit 91
                fi
                """,
            env=environment,
        )
        current = (deploy_root / ".lumen-maintenance.lock").read_bytes()
        before = (tmp_path / "owner-before").read_bytes()
    finally:
        if holder.poll() is None:
            os.killpg(holder.pid, signal.SIGTERM)
        holder_stdout, holder_stderr = holder.communicate(timeout=5)

    assert holder.returncode in {-signal.SIGTERM, 143}, holder_stderr + holder_stdout
    assert contender.returncode == 0, contender.stderr + contender.stdout
    assert current == before
    assert b"capability_sha256=" in current


def test_maintenance_lock_interrupt_cleans_owned_mkdir_lock(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    ready = tmp_path / "ready"
    deploy_root.mkdir()
    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            """
            set -euo pipefail
            command() {
                if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
                    return 1
                fi
                builtin command "$@"
            }
            . "$1"
            trap 'exit 143' TERM
            lumen_acquire_lock "$2" interrupted-owner
            : > "$3"
            while :; do sleep 0.1; done
            """,
            "mkdir-holder",
            str(LIB),
            str(deploy_root),
            str(ready),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        os.killpg(holder.pid, signal.SIGTERM)
        stdout, stderr = holder.communicate(timeout=5)
    finally:
        if holder.poll() is None:
            os.killpg(holder.pid, signal.SIGKILL)
            holder.wait()

    assert holder.returncode == 143, stderr + stdout
    assert not (deploy_root / ".lumen-maintenance.lock.d").exists()


def test_maintenance_lock_rejects_symlinked_fallback_domain(tmp_path: Path) -> None:
    deploy_root = tmp_path / "deploy"
    outside = tmp_path / "outside-lock"
    deploy_root.mkdir()
    outside.mkdir()
    (deploy_root / ".lumen-maintenance.lock.d").symlink_to(
        outside,
        target_is_directory=True,
    )
    result = _run_bash(
        f"""
        set -euo pipefail
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        . {shlex.quote(str(LIB))}
        lumen_acquire_lock {shlex.quote(str(deploy_root))} install.sh
        """
    )

    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert not (outside / "owner").exists()


def test_custom_deploy_root_maintenance_lock_blocks_all_ops(tmp_path: Path) -> None:
    deploy_root = tmp_path / "custom-deploy"
    backup_root = tmp_path / "backup"
    ready = tmp_path / "ready"
    release = deploy_root / "releases" / "release-1"
    (release / "scripts").mkdir(parents=True)
    (deploy_root / "shared").mkdir()
    backup_root.mkdir()
    (deploy_root / "current").symlink_to(Path("releases") / release.name)

    holder = subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            """
            set -euo pipefail
            command() {
                if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
                    return 1
                fi
                builtin command "$@"
            }
            . "$1"
            lumen_acquire_lock "$2" holder.sh
            : > "$3"
            while :; do sleep 0.1; done
            """,
            "ops-p1-holder",
            str(LIB),
            str(deploy_root),
            str(ready),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_file(ready)
        env = os.environ.copy()
        env.update(
            {
                "BACKUP_ROOT": str(backup_root),
                "LUMEN_BACKUP_FORCE": "1",
                "LUMEN_BACKUP_ROOT": str(backup_root),
                "LUMEN_DEPLOY_ROOT": str(deploy_root),
                "LUMEN_BACKUP_RESTORE_LOCKFILE": str(tmp_path / "backup-restore.lock"),
            }
        )
        backup = subprocess.run(
            _without_flock_source(BACKUP),
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
            check=False,
        )
        restore = subprocess.run(
            _without_flock_source(RESTORE, "20260803-000000"),
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
            check=False,
        )
        update = subprocess.run(
            _without_flock_source(UPDATE_RUNNER),
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
            check=False,
        )
    finally:
        holder.terminate()
        holder_stdout, holder_stderr = holder.communicate(timeout=5)

    assert holder.returncode in {-15, 143}, holder_stderr + holder_stdout
    assert backup.returncode == 0, backup.stderr + backup.stdout
    assert "skipped: maintenance lock held" in backup.stdout
    assert restore.returncode != 0
    assert "已有 Lumen 维护脚本" in restore.stderr
    assert update.returncode != 0
    assert "已有 Lumen 维护脚本" in update.stderr
    assert not (ROOT / ".lumen-maintenance.lock.d").exists()


def test_flock_owner_write_preserves_shared_group_mode(tmp_path: Path) -> None:
    deploy_root = tmp_path / "deploy"
    deploy_root.mkdir()
    lock_file = deploy_root / ".lumen-maintenance.lock"
    lock_file.write_text("", encoding="utf-8")
    lock_file.chmod(0o660)

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_try_acquire_lock {shlex.quote(str(deploy_root))} backup.sh
        """
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o660


def test_worker_health_is_the_compose_wait_contract(tmp_path: Path) -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]

    assert worker["command"] == ["python", "-m", "app.worker_health", "run"]
    assert worker["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "app.worker_health",
        "check",
    ]
    assert (
        worker["environment"]["LUMEN_WORKER_HEALTH_KEY_PREFIX"]
        == "${LUMEN_WORKER_HEALTH_KEY_PREFIX:-arq:queue:health-check}"
    )
    assert "LUMEN_WORKER_HEALTH_KEY" not in worker["environment"]

    calls = tmp_path / "compose.log"
    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(UPDATE_COMPOSE))}
        lumen_compose_in() {{
            printf '%s\\n' "$*" >> {shlex.quote(str(calls))}
        }}
        LUMEN_UPDATE_MODE=fast
        compose_up_service /release worker
        LUMEN_UPDATE_MODE=standard
        compose_up_service /release worker
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    fast, standard = calls.read_text(encoding="utf-8").splitlines()
    assert "up --pull missing --no-deps" in fast
    assert "-d --wait --force-recreate worker" in fast
    assert "up --pull missing --timeout" in standard
    assert "-d --wait --force-recreate worker" in standard


def test_systemd_worker_readiness_passes_expected_service_owner_uid(
    tmp_path: Path,
) -> None:
    deploy_root = tmp_path / "deploy"
    worker_dir = deploy_root / "current" / "apps" / "worker"
    python_bin = deploy_root / "current" / ".venv" / "bin" / "python"
    shared = deploy_root / "shared"
    worker_dir.mkdir(parents=True)
    python_bin.parent.mkdir(parents=True)
    shared.mkdir()
    (shared / ".env").write_text(
        "REDIS_URL=redis://127.0.0.1:6379/0\n",
        encoding="utf-8",
    )
    state_file = shared / "worker-var" / "worker-health.json"
    state_file.parent.mkdir()
    calls = tmp_path / "worker-health.log"
    python_bin.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'args=%s\\n\' "$*" > "${TEST_WORKER_HEALTH_LOG:?}"\n'
        "printf 'redis=%s\\n' \"${REDIS_URL:?}\" >> "
        '"${TEST_WORKER_HEALTH_LOG:?}"\n'
        "printf 'state=%s\\n' \"${LUMEN_WORKER_HEALTH_STATE_FILE:?}\" >> "
        '"${TEST_WORKER_HEALTH_LOG:?}"\n',
        encoding="utf-8",
    )
    python_bin.chmod(0o755)

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_run_as_root() {{ "$@"; }}
        lumen_systemd_unit_property() {{
            test "$1" = lumen-worker.service
            test "$2" = User
            printf '%s\\n' {shlex.quote(pwd.getpwuid(os.getuid()).pw_name)}
        }}
        export TEST_WORKER_HEALTH_LOG={shlex.quote(str(calls))}
        LUMEN_SYSTEMD_WORKER_PYTHON={shlex.quote(str(python_bin))}
        LUMEN_SYSTEMD_WORKER_HEALTH_STATE_FILE={shlex.quote(str(state_file))}
        lumen_systemd_worker_readiness_once \
            {shlex.quote(str(deploy_root))} \
            http://127.0.0.1:8000/readyz 0 1
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert calls.read_text(encoding="utf-8").splitlines() == [
        (f"args=-m app.worker_health check --expected-owner-uid {os.getuid()}"),
        "redis=redis://127.0.0.1:6379/0",
        f"state={state_file}",
    ]


def test_core_readiness_typed_shell_contract() -> None:
    result = _run_bash(
        f"""
        set -u
        . {shlex.quote(str(LIB))}
        api_probe() {{ return "${{API_RC:?}}"; }}
        worker_probe() {{ return "${{WORKER_RC:?}}"; }}
        run_case() {{
            API_RC="$1"
            WORKER_RC="$2"
            attempts="$3"
            set +e
            state="$(lumen_core_readiness_state \
                api_probe worker_probe "$attempts" 0)"
            rc=$?
            set -e
            printf '%s:%s\\n' "$state" "$rc"
        }}
        set -e
        run_case 0 0 1
        run_case 1 0 1
        run_case 0 1 1
        run_case 0 0 0
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.splitlines() == [
        "ready:0",
        "api_not_ready:1",
        "worker_not_ready:1",
        "invalid:2",
    ]


def test_systemd_fallback_writer_blocks_backup_restore_snapshot(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    docker_log = tmp_path / "docker.log"
    systemctl = fakebin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
if [ "$1" = is-active ] && [ "$2" = lumen-worker.service ]; then
    printf 'active\\n'
    exit 0
fi
printf 'inactive\\n'
exit 3
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)
    docker = fakebin / "docker"
    docker.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(docker_log))}\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    result = _run_bash(
        f"""
        set -u
        export PATH={shlex.quote(str(fakebin))}:$PATH
        export LUMEN_SYSTEMD_RUNTIME_AVAILABLE=1
        . {shlex.quote(str(BACKUP_RESTORE_SERVICES))}
        lumen_running_writer_services
        """
    )

    assert result.returncode != 0
    assert "systemd fallback writers are active" in result.stderr
    assert not docker_log.exists()


def test_systemd_fallback_writer_blocks_update_even_when_storage_skip_is_set(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    systemctl = fakebin / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
if [ "$1" = is-active ] && [ "$2" = lumen-api.service ]; then
    printf 'active\\n'
    exit 0
fi
printf 'inactive\\n'
exit 3
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    result = _run_bash(
        f"""
        set -u
        export PATH={shlex.quote(str(fakebin))}:$PATH
        export LUMEN_SYSTEMD_RUNTIME_AVAILABLE=1
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_warn() {{ :; }}
        . {shlex.quote(str(STORAGE_IDENTITY))}
        SKIP_STORAGE_CHECK=1
        lumen_update_require_storage_identity update_test
        """
    )

    assert result.returncode != 0
    assert "systemd 兜底 writer 仍在运行" in result.stderr
