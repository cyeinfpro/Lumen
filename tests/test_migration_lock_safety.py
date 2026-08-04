from __future__ import annotations

import os
import pwd
import shlex
import shutil
import signal
import subprocess
import textwrap
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
MIGRATE = ROOT / "scripts" / "migrate_to_releases.sh"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _start_bash(script: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return subprocess.Popen(
        ["bash", "-c", script],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _durably_write_bytes(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_systemctl_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -u
            command_line="$*"
            printf '%s\\n' "${command_line}" >> "${SYSTEMCTL_LOG:?}"
            if [ -n "${SYSTEMCTL_SIGNAL_COMMAND:-}" ] \
                    && [ "${command_line}" = "${SYSTEMCTL_SIGNAL_COMMAND}" ] \
                    && [ ! -e "${SYSTEMCTL_SIGNAL_ONCE_FILE:?}" ]; then
                : > "${SYSTEMCTL_SIGNAL_ONCE_FILE}"
                : > "${SYSTEMCTL_SIGNAL_READY:?}"
                while [ ! -e "${SYSTEMCTL_SIGNAL_GO:?}" ]; do
                    sleep 0.02
                done
                kill "-${SYSTEMCTL_SIGNAL_NAME:?}" "${PPID}"
                exit 0
            fi
            case "${1:-}" in
                list-unit-files)
                    printf '%s enabled\\n' "${2:?}"
                    ;;
                show)
                    if [ "${3:-}" = "User" ] \
                            && [ "${5:-}" = "lumen-worker.service" ]; then
                        printf '%s\\n' "${TEST_WORKER_SERVICE_USER:?}"
                    fi
                    ;;
                is-active)
                    if [ "${3:-}" = "${SYSTEMCTL_INACTIVE_AFTER_START_UNIT:-}" ] \
                            && [ -e "${SYSTEMCTL_STARTED_MARKER:?}" ]; then
                        count=0
                        if [ -f "${SYSTEMCTL_ACTIVE_CHECK_COUNT:?}" ]; then
                            count="$(cat "${SYSTEMCTL_ACTIVE_CHECK_COUNT}")"
                        fi
                        count=$((count + 1))
                        printf '%s\\n' "${count}" > "${SYSTEMCTL_ACTIVE_CHECK_COUNT}"
                        if [ "${count}" -gt "${SYSTEMCTL_ACTIVE_AFTER_START_SUCCESSES:-0}" ]; then
                            exit 3
                        fi
                    fi
                    case " ${SYSTEMCTL_ACTIVE_UNITS:-} " in
                        *" ${3:-} "*) exit 0 ;;
                        *) exit 3 ;;
                    esac
                    ;;
                stop)
                    if [ "${2:-}" = "${SYSTEMCTL_FAIL_STOP_UNIT:-}" ]; then
                        exit 1
                    fi
                    ;;
                start)
                    if [ "${2:-}" = "${SYSTEMCTL_STARTED_MARKER_UNIT:-}" ]; then
                        : > "${SYSTEMCTL_STARTED_MARKER:?}"
                    fi
                    if [ "${2:-}" = "${SYSTEMCTL_FAIL_START_UNIT:-}" ]; then
                        exit 1
                    fi
                    ;;
            esac
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_curl_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -u
            url="${@: -1}"
            printf '%s\\n' "${url}" >> "${CURL_LOG:?}"
            case ",${CURL_FAIL_URLS:-}," in
                *,"${url}",*) exit 22 ;;
            esac
            printf '200'
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_noop_sleep_mock(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _write_worker_health_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\n' "$*" >> "${WORKER_HEALTH_LOG:?}"
            [[ "${TEST_WORKER_READY:-1}" == "1" ]]
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_sudo_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            [ "${1:-}" = "-n" ] && shift
            case "${1:-}" in
                chown)
                    exit 0
                    ;;
                chmod)
                    shift
                    command chmod "$@"
                    ;;
                *)
                    command "$@"
                    ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_flock_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import fcntl
            import sys

            operation = sys.argv[1]
            descriptor = int(sys.argv[2])
            try:
                if operation == "-n":
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                elif operation == "-u":
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                else:
                    raise SystemExit(2)
            except BlockingIOError:
                raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_mv_signal_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -u
            "${REAL_MV:?}" "$@"
            rc=$?
            if [ "${rc}" -eq 0 ] \
                    && [ "${1:-}" = "${MV_SIGNAL_SOURCE:-}" ] \
                    && [ ! -e "${MV_SIGNAL_ONCE_FILE:?}" ]; then
                : > "${MV_SIGNAL_ONCE_FILE}"
                : > "${MV_SIGNAL_READY:?}"
                while [ ! -e "${MV_SIGNAL_GO:?}" ]; do
                    sleep 0.02
                done
                kill "-${MV_SIGNAL_NAME:?}" "${PPID}"
            fi
            exit "${rc}"
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _run_migration(
    tmp_path: Path,
    *,
    fail_stop: str = "",
    fail_start: str = "",
    inactive_after_start_unit: str = "",
    active_after_start_successes: int = 0,
    http_fail_urls: tuple[str, ...] = (),
    api_health_url: str = "",
    web_health_url: str = "",
    active_attempts: int = 1,
    active_stable_polls: int = 1,
    api_health_attempts: int = 1,
    web_health_attempts: int = 1,
    worker_ready: bool = True,
    active_units: tuple[str, ...] = (
        "lumen-tgbot.service",
        "lumen-web.service",
        "lumen-worker.service",
        "lumen-api.service",
    ),
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    root = tmp_path / "lumen"
    data_root = tmp_path / "lumendata"
    fakebin = tmp_path / "bin"
    systemctl_log = tmp_path / "systemctl.log"
    root.mkdir()
    data_root.mkdir()
    fakebin.mkdir()
    (root / "apps/worker").mkdir(parents=True)
    (root / "payload.txt").write_text("keep-me\n", encoding="utf-8")
    (root / ".env").write_text(
        "REDIS_URL=redis://localhost:6379/0\n",
        encoding="utf-8",
    )
    _write_systemctl_mock(fakebin / "systemctl")
    _write_curl_mock(fakebin / "curl")
    _write_noop_sleep_mock(fakebin / "sleep")
    _write_worker_health_mock(fakebin / "worker-health")
    _write_sudo_mock(fakebin / "sudo")
    _write_flock_mock(fakebin / "flock")

    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_ROOT": str(root),
            "LUMEN_DATA_ROOT": str(data_root),
            "LUMEN_BACKUP_ROOT": str(data_root / "backup"),
            "SYSTEMCTL_LOG": str(systemctl_log),
            "SYSTEMCTL_FAIL_STOP_UNIT": fail_stop,
            "SYSTEMCTL_FAIL_START_UNIT": fail_start,
            "SYSTEMCTL_INACTIVE_AFTER_START_UNIT": inactive_after_start_unit,
            "SYSTEMCTL_STARTED_MARKER_UNIT": inactive_after_start_unit,
            "SYSTEMCTL_STARTED_MARKER": str(tmp_path / "started.marker"),
            "SYSTEMCTL_ACTIVE_CHECK_COUNT": str(tmp_path / "active-check.count"),
            "SYSTEMCTL_ACTIVE_AFTER_START_SUCCESSES": str(
                active_after_start_successes
            ),
            "SYSTEMCTL_ACTIVE_UNITS": " ".join(active_units),
            "CURL_LOG": str(tmp_path / "curl.log"),
            "CURL_FAIL_URLS": ",".join(http_fail_urls),
            "LUMEN_MIGRATION_ACTIVE_ATTEMPTS": str(active_attempts),
            "LUMEN_MIGRATION_ACTIVE_STABLE_POLLS": str(active_stable_polls),
            "LUMEN_MIGRATION_ACTIVE_INTERVAL_SECONDS": "0",
            "LUMEN_API_HEALTH_ATTEMPTS": str(api_health_attempts),
            "LUMEN_WEB_HEALTH_ATTEMPTS": str(web_health_attempts),
            "LUMEN_MIGRATION_CORE_READINESS_ATTEMPTS": "1",
            "LUMEN_MIGRATION_CORE_READINESS_INTERVAL_SECONDS": "0",
            "LUMEN_SYSTEMD_WORKER_PYTHON": str(fakebin / "worker-health"),
            "TEST_WORKER_READY": "1" if worker_ready else "0",
            "TEST_WORKER_SERVICE_USER": pwd.getpwuid(os.getuid()).pw_name,
            "WORKER_HEALTH_LOG": str(tmp_path / "worker-health.log"),
            "SYSTEMCTL_SIGNAL_COMMAND": "",
            "SYSTEMCTL_SIGNAL_ONCE_FILE": str(tmp_path / "signal.once"),
            "SYSTEMCTL_SIGNAL_READY": str(tmp_path / "signal.ready"),
            "SYSTEMCTL_SIGNAL_GO": str(tmp_path / "signal.go"),
            "SYSTEMCTL_SIGNAL_NAME": "TERM",
        }
    )
    if api_health_url:
        env["LUMEN_API_HEALTH_URL"] = api_health_url
    if web_health_url:
        env["LUMEN_WEB_HEALTH_URL"] = web_health_url
    result = subprocess.run(
        ["bash", str(MIGRATE)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    calls = (
        systemctl_log.read_text(encoding="utf-8").splitlines()
        if systemctl_log.exists()
        else []
    )
    return result, root, calls


def _prepare_signal_migration(
    tmp_path: Path,
    *,
    signal_command: str,
    signal_name: str,
    active_units: tuple[str, ...] = (
        "lumen-web.service",
        "lumen-api.service",
    ),
    mv_signal_source: Path | None = None,
    migration_failpoint: str = "",
    migration_failpoint_kind: str = "",
    migration_failpoint_name: str = "",
    env_out: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Path, Path, Path, Path, Path]:
    root = tmp_path / "lumen"
    data_root = tmp_path / "lumendata"
    fakebin = tmp_path / "bin"
    systemctl_log = tmp_path / "systemctl.log"
    ready = tmp_path / "signal.ready"
    go = tmp_path / "signal.go"
    lock_dir = tmp_path / "lumen.migrate-to-releases.lock.d"
    (root / "apps/web/.next/cache").mkdir(parents=True)
    (root / "apps/worker/var").mkdir(parents=True)
    data_root.mkdir()
    fakebin.mkdir()
    (root / "payload.txt").write_text("keep-me\n", encoding="utf-8")
    (root / ".env").write_text("ROOT_ENV=original\n", encoding="utf-8")
    (root / "apps/web/.env.local").write_text(
        "WEB_ENV=original\n",
        encoding="utf-8",
    )
    (root / "apps/web/.next/cache/cache.bin").write_bytes(b"cache")
    (root / "apps/worker/var/state.bin").write_bytes(b"worker")
    _write_systemctl_mock(fakebin / "systemctl")
    _write_curl_mock(fakebin / "curl")
    _write_sudo_mock(fakebin / "sudo")
    _write_flock_mock(fakebin / "flock")
    if mv_signal_source is not None:
        _write_mv_signal_mock(fakebin / "mv")

    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "LUMEN_ROOT": str(root),
            "LUMEN_DATA_ROOT": str(data_root),
            "LUMEN_BACKUP_ROOT": str(data_root / "backup"),
            "SYSTEMCTL_LOG": str(systemctl_log),
            "SYSTEMCTL_FAIL_STOP_UNIT": "",
            "SYSTEMCTL_FAIL_START_UNIT": "",
            "SYSTEMCTL_INACTIVE_AFTER_START_UNIT": "",
            "SYSTEMCTL_STARTED_MARKER_UNIT": "",
            "SYSTEMCTL_STARTED_MARKER": str(tmp_path / "started.marker"),
            "SYSTEMCTL_ACTIVE_CHECK_COUNT": str(tmp_path / "active-check.count"),
            "SYSTEMCTL_ACTIVE_AFTER_START_SUCCESSES": "0",
            "SYSTEMCTL_ACTIVE_UNITS": " ".join(active_units),
            "CURL_LOG": str(tmp_path / "curl.log"),
            "CURL_FAIL_URLS": "",
            "LUMEN_MIGRATION_ACTIVE_ATTEMPTS": "1",
            "LUMEN_MIGRATION_ACTIVE_STABLE_POLLS": "1",
            "LUMEN_MIGRATION_ACTIVE_INTERVAL_SECONDS": "0",
            "LUMEN_API_HEALTH_ATTEMPTS": "1",
            "LUMEN_WEB_HEALTH_ATTEMPTS": "1",
            "SYSTEMCTL_SIGNAL_COMMAND": signal_command,
            "SYSTEMCTL_SIGNAL_ONCE_FILE": str(tmp_path / "signal.once"),
            "SYSTEMCTL_SIGNAL_READY": str(ready),
            "SYSTEMCTL_SIGNAL_GO": str(go),
            "SYSTEMCTL_SIGNAL_NAME": signal_name,
            "REAL_MV": shutil.which("mv") or "/bin/mv",
            "MV_SIGNAL_SOURCE": str(mv_signal_source or ""),
            "MV_SIGNAL_ONCE_FILE": str(tmp_path / "mv-signal.once"),
            "MV_SIGNAL_READY": str(ready),
            "MV_SIGNAL_GO": str(go),
            "MV_SIGNAL_NAME": signal_name,
            "LUMEN_MIGRATION_FAILPOINT": migration_failpoint,
            "LUMEN_MIGRATION_FAILPOINT_KIND": migration_failpoint_kind,
            "LUMEN_MIGRATION_FAILPOINT_NAME": migration_failpoint_name,
            "LUMEN_MIGRATION_FAILPOINT_ACTION": "pause",
            "LUMEN_MIGRATION_FAILPOINT_READY": str(ready),
            "LUMEN_MIGRATION_FAILPOINT_GO": str(go),
        }
    )
    if env_out is not None:
        env_out.update(env)
    process = subprocess.Popen(
        ["bash", str(MIGRATE)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    return process, root, lock_dir, systemctl_log, ready, go


def _rerun_interrupted_migration(
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    recovery_env = env.copy()
    recovery_env.update(
        {
            "SYSTEMCTL_SIGNAL_COMMAND": "",
            "MV_SIGNAL_SOURCE": "",
            "LUMEN_MIGRATION_FAILPOINT": "",
            "LUMEN_MIGRATION_FAILPOINT_KIND": "",
            "LUMEN_MIGRATION_FAILPOINT_NAME": "",
            "LUMEN_MIGRATION_FAILPOINT_READY": "",
            "LUMEN_MIGRATION_FAILPOINT_GO": "",
        }
    )
    return subprocess.run(
        ["bash", str(MIGRATE)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=recovery_env,
        timeout=20,
        check=False,
    )


def _assert_original_layout_restored(root: Path) -> None:
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert (root / ".env").read_text(encoding="utf-8") == "ROOT_ENV=original\n"
    assert not (root / ".env").is_symlink()
    assert (
        root / "apps/web/.env.local"
    ).read_text(encoding="utf-8") == "WEB_ENV=original\n"
    assert not (root / "apps/web/.env.local").is_symlink()
    assert (root / "apps/web/.next/cache/cache.bin").read_bytes() == b"cache"
    assert not (root / "apps/web/.next/cache").is_symlink()
    assert (root / "apps/worker/var/state.bin").read_bytes() == b"worker"
    assert not (root / "apps/worker/var").is_symlink()
    assert not (root / "current").exists()
    assert not (root / "releases").exists()
    assert not (root / "shared").exists()
    assert not Path(f"{root}.tmp").exists()
    assert not Path(f"{root}.migrate-to-releases.lock.d").exists()


def test_migration_refuses_to_run_while_global_maintenance_lock_is_held(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lumen"
    data_root = tmp_path / "lumendata"
    ready = tmp_path / "ready"
    go = tmp_path / "go"
    root.mkdir()
    data_root.mkdir()
    (root / "payload.txt").write_text("keep-me\n", encoding="utf-8")

    holder = _start_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        lumen_acquire_lock {shlex.quote(str(root))} holder.sh
        : > {shlex.quote(str(ready))}
        while [ ! -e {shlex.quote(str(go))} ]; do sleep 0.02; done
        """
    )
    try:
        _wait_for_file(ready)
        env = os.environ.copy()
        env.update(
            {
                "LC_ALL": "C",
                "LUMEN_ROOT": str(root),
                "LUMEN_DATA_ROOT": str(data_root),
                "LUMEN_BACKUP_ROOT": str(data_root / "backup"),
            }
        )
        result = subprocess.run(
            ["bash", str(MIGRATE)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=10,
        )
    finally:
        go.touch()
        holder_stdout, holder_stderr = holder.communicate(timeout=5)

    assert holder.returncode == 0, holder_stderr + holder_stdout
    assert result.returncode != 0
    assert "已有 Lumen 维护脚本" in result.stderr
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "current").exists()
    assert not Path(f"{root}.migrate-to-releases.lock.d").exists()


def test_stale_lock_is_preserved_without_cross_platform_cas(tmp_path: Path) -> None:
    root = tmp_path / "root"
    lock_dir = root / ".lumen-maintenance.lock.d"
    root.mkdir()

    result = _run_bash(
        f"""
        set -u
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        lumen_pid_start_token() {{ printf 'token-%s\\n' "$1"; }}
        LOCK_DIR={shlex.quote(str(lock_dir))}
        mkdir "$LOCK_DIR"
        printf 'pid=%s\\nstart_token=stale-token\\nscript=old.sh\\n' "$$" \
            > "$LOCK_DIR/owner"
        if lumen_try_acquire_lock {shlex.quote(str(root))} contender.sh; then
            printf 'contender unexpectedly acquired lock\\n' >&2
            exit 1
        fi
        grep -q '^script=old.sh$' "$LOCK_DIR/owner"
        test "${{LUMEN_LAST_LOCK_STALE:-0}}" = 1
        test "${{LUMEN_LAST_LOCK_RECLAIMED:-0}}" = 0
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_release_cannot_delete_replacement_owner_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    lock_dir = root / ".lumen-maintenance.lock.d"
    displaced = root / "displaced-lock"
    root.mkdir()

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        lumen_pid_start_token() {{ printf 'token-%s\\n' "$1"; }}
        lumen_try_acquire_lock {shlex.quote(str(root))} owner.sh
        test -n "${{LUMEN_LOCK_OWNER_TOKEN:-}}"
        mv {shlex.quote(str(lock_dir))} {shlex.quote(str(displaced))}
        mkdir {shlex.quote(str(lock_dir))}
        mkdir {shlex.quote(str(lock_dir / ".owner.later"))}
        printf 'pid=%s\\nstart_token=token-%s\\nowner_id=.owner.later\\nscript=later.sh\\n' \
            "$$" "$$" > {shlex.quote(str(lock_dir / ".owner.later" / "owner"))}
        lumen_release_lock
        test -f {shlex.quote(str(lock_dir / ".owner.later" / "owner"))}
        grep -q '^script=later.sh$' \
            {shlex.quote(str(lock_dir / ".owner.later" / "owner"))}
        test -d {shlex.quote(str(displaced))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "owner 已变化" in result.stderr


def test_legacy_pid_only_stale_lock_reports_without_errexit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    lock_dir = root / ".lumen-maintenance.lock.d"
    root.mkdir()
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("2147483647\n", encoding="utf-8")

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        lumen_acquire_lock {shlex.quote(str(root))} contender.sh
        """
    )

    assert result.returncode == 1
    assert "stale Lumen" in result.stderr
    assert "owner pid=2147483647" in result.stderr
    assert "人工删除" in result.stderr
    assert lock_dir.is_dir()
    assert (lock_dir / "pid").read_text(encoding="utf-8") == "2147483647\n"


def test_owned_lock_uses_unique_child_and_releases_cleanly(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    lock_dir = root / ".lumen-maintenance.lock.d"
    root.mkdir()

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        lumen_pid_start_token() {{ printf 'token-%s\\n' "$1"; }}
        lumen_try_acquire_lock {shlex.quote(str(root))} owner.sh
        case "${{LUMEN_LOCK_OWNER_TOKEN}}" in
            .owner.*) ;;
            *) exit 1 ;;
        esac
        owner_file="{lock_dir}/${{LUMEN_LOCK_OWNER_TOKEN}}/owner"
        grep -q "^owner_id=${{LUMEN_LOCK_OWNER_TOKEN}}$" "$owner_file"
        grep -q '^script=owner.sh$' "$owner_file"
        test ! -e {shlex.quote(str(lock_dir / "owner"))}
        lumen_release_lock
        test ! -e {shlex.quote(str(lock_dir))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    ("sig", "signal_name", "expected_rc"),
    (
        (signal.SIGTERM, "TERM", 143),
        (signal.SIGINT, "INT", 130),
    ),
)
def test_lumen_with_lock_releases_after_child_signal_status(
    tmp_path: Path,
    sig: signal.Signals,
    signal_name: str,
    expected_rc: int,
) -> None:
    lock_root = tmp_path / "backup"
    lock_dir = lock_root / ".lumen-update.lock.d"
    child_pid_file = tmp_path / "child.pid"
    ready = tmp_path / "ready"
    process = _start_bash(
        f"""
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        LUMEN_BACKUP_ROOT={shlex.quote(str(lock_root))}
        lumen_with_lock child-signal 30 bash -c '
            trap "exit {expected_rc}" {signal_name}
            printf "%s\\n" "$$" > "$1"
            : > "$2"
            while :; do sleep 1; done
        ' bash {shlex.quote(str(child_pid_file))} {shlex.quote(str(ready))}
        """
    )
    try:
        _wait_for_file(ready)
        os.kill(int(child_pid_file.read_text(encoding="utf-8")), sig)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == expected_rc, stderr + stdout
    assert not lock_dir.exists()


@pytest.mark.parametrize(
    ("sig", "signal_name"),
    ((signal.SIGTERM, "TERM"), (signal.SIGINT, "INT")),
)
@pytest.mark.parametrize("disposition", ("ignore", "custom"))
def test_lumen_with_lock_preserves_nondefault_signal_disposition(
    tmp_path: Path,
    sig: signal.Signals,
    signal_name: str,
    disposition: str,
) -> None:
    lock_root = tmp_path / "backup"
    lock_dir = lock_root / ".lumen-update.lock.d"
    ready = tmp_path / "ready"
    completed = tmp_path / "completed"
    handler_log = tmp_path / "handler.log"
    if disposition == "ignore":
        trap_command = f"trap '' {signal_name}"
    else:
        trap_command = (
            f"trap 'printf \"%s\\\\n\" {signal_name} >> "
            f"{shlex.quote(str(handler_log))}' {signal_name}"
        )
    process = _start_bash(
        f"""
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        {trap_command}
        before="$(trap -p {signal_name})"
        work() {{
            : > {shlex.quote(str(ready))}
            sleep 0.7
            : > {shlex.quote(str(completed))}
        }}
        LUMEN_BACKUP_ROOT={shlex.quote(str(lock_root))}
        lumen_with_lock preserve-{disposition} 30 work
        rc=$?
        after="$(trap -p {signal_name})"
        [ "$before" = "$after" ] || exit 91
        exit "$rc"
        """
    )
    try:
        _wait_for_file(ready)
        os.kill(process.pid, sig)
        time.sleep(0.1)
        assert process.poll() is None
        assert lock_dir.is_dir()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 0, stderr + stdout
    assert completed.is_file()
    assert not lock_dir.exists()
    if disposition == "custom":
        assert handler_log.read_text(encoding="utf-8").splitlines() == [signal_name]
    else:
        assert not handler_log.exists()


def test_lumen_with_lock_default_term_releases_and_runs_saved_exit_once(
    tmp_path: Path,
) -> None:
    lock_root = tmp_path / "backup"
    lock_dir = lock_root / ".lumen-update.lock.d"
    ready = tmp_path / "ready"
    exit_log = tmp_path / "exit.log"
    process = _start_bash(
        f"""
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        trap 'printf "exit\\n" >> {shlex.quote(str(exit_log))}' EXIT
        work() {{
            : > {shlex.quote(str(ready))}
            while :; do sleep 0.2; done
        }}
        LUMEN_BACKUP_ROOT={shlex.quote(str(lock_root))}
        lumen_with_lock default-term 30 work
        """
    )
    try:
        _wait_for_file(ready)
        os.kill(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode in (-signal.SIGTERM, 143), stderr + stdout
    assert not lock_dir.exists()
    assert exit_log.read_text(encoding="utf-8").splitlines() == ["exit"]


def test_lumen_with_lock_nested_calls_restore_original_traps(
    tmp_path: Path,
) -> None:
    outer_root = tmp_path / "outer"
    inner_root = tmp_path / "inner"
    exit_log = tmp_path / "exit.log"

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        trap 'printf "exit\\n" >> {shlex.quote(str(exit_log))}' EXIT
        trap 'printf "int\\n" >> {shlex.quote(str(exit_log))}' INT
        trap 'printf "term\\n" >> {shlex.quote(str(exit_log))}' TERM
        before_exit="$(trap -p EXIT)"
        before_int="$(trap -p INT)"
        before_term="$(trap -p TERM)"
        inner() {{
            LUMEN_BACKUP_ROOT={shlex.quote(str(inner_root))}
            lumen_with_lock inner 30 true
        }}
        LUMEN_BACKUP_ROOT={shlex.quote(str(outer_root))}
        lumen_with_lock outer 30 inner
        [ "$(trap -p EXIT)" = "$before_exit" ]
        [ "$(trap -p INT)" = "$before_int" ]
        [ "$(trap -p TERM)" = "$before_term" ]
        test ! -d {shlex.quote(str(outer_root / ".lumen-update.lock.d"))}
        test ! -d {shlex.quote(str(inner_root / ".lumen-update.lock.d"))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert exit_log.read_text(encoding="utf-8").splitlines() == ["exit"]


def test_lumen_with_lock_exit_releases_and_preserves_saved_exit_status(
    tmp_path: Path,
) -> None:
    lock_root = tmp_path / "backup"
    lock_dir = lock_root / ".lumen-update.lock.d"
    exit_log = tmp_path / "exit.log"
    result = _run_bash(
        f"""
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        trap 'printf "%s\\n" "$?" >> {shlex.quote(str(exit_log))}' EXIT
        leave() {{ exit 23; }}
        LUMEN_BACKUP_ROOT={shlex.quote(str(lock_root))}
        lumen_with_lock exit-test 30 leave
        """
    )

    assert result.returncode == 23, result.stderr + result.stdout
    assert not lock_dir.exists()
    assert exit_log.read_text(encoding="utf-8").splitlines() == ["23"]


def test_lumen_with_lock_signal_cleanup_cannot_delete_successor_owner(
    tmp_path: Path,
) -> None:
    lock_root = tmp_path / "backup"
    lock_dir = lock_root / ".lumen-update.lock.d"
    displaced = tmp_path / "displaced"
    successor = lock_dir / ".owner.successor" / "owner"
    process = _start_bash(
        f"""
        . {shlex.quote(str(LIB))}
        command() {{
            if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
                return 1
            fi
            builtin command "$@"
        }}
        lumen_pid_start_token() {{ printf 'token-%s\\n' "$1"; }}
        replace_owner() {{
            mv {shlex.quote(str(lock_dir))} {shlex.quote(str(displaced))}
            mkdir -p {shlex.quote(str(successor.parent))}
            printf 'pid=%s\\nstart_token=token-%s\\nowner_id=.owner.successor\\noperation_id=successor\\n' \
                "$$" "$$" > {shlex.quote(str(successor))}
            kill -TERM "$$"
        }}
        LUMEN_BACKUP_ROOT={shlex.quote(str(lock_root))}
        lumen_with_lock successor-test 30 replace_owner
        """
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode in (-signal.SIGTERM, 143), stderr + stdout
    assert successor.is_file()
    assert "operation_id=successor" in successor.read_text(encoding="utf-8")
    assert displaced.is_dir()


def test_stop_failure_restores_units_stopped_earlier(tmp_path: Path) -> None:
    result, root, calls = _run_migration(
        tmp_path,
        fail_stop="lumen-worker.service",
    )

    assert result.returncode != 0
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "current").exists()
    assert calls == [
        "list-unit-files lumen-tgbot.service --no-legend",
        "is-active --quiet lumen-tgbot.service",
        "list-unit-files lumen-web.service --no-legend",
        "is-active --quiet lumen-web.service",
        "list-unit-files lumen-worker.service --no-legend",
        "is-active --quiet lumen-worker.service",
        "list-unit-files lumen-api.service --no-legend",
        "is-active --quiet lumen-api.service",
        "stop lumen-tgbot.service",
        "stop lumen-web.service",
        "stop lumen-worker.service",
        "start lumen-api.service",
        "is-active --quiet lumen-api.service",
        "start lumen-worker.service",
        "is-active --quiet lumen-worker.service",
        "start lumen-web.service",
        "is-active --quiet lumen-web.service",
        "start lumen-tgbot.service",
        "is-active --quiet lumen-tgbot.service",
        "show -p User --value lumen-worker.service",
    ]
    assert "拒绝移动部署目录" in result.stderr
    assert "迁移完成" not in result.stdout


def test_final_start_failure_is_nonzero_and_not_reported_complete(
    tmp_path: Path,
) -> None:
    result, root, calls = _run_migration(
        tmp_path,
        fail_start="lumen-worker.service",
    )

    assert result.returncode != 0
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "current").exists()
    assert not (root / "releases").exists()
    assert not (root / "shared").exists()
    assert not Path(f"{root}.tmp").exists()
    for unit in (
        "lumen-api.service",
        "lumen-worker.service",
        "lumen-web.service",
        "lumen-tgbot.service",
    ):
        assert f"start {unit}" in calls
    assert "启动 lumen-worker.service 失败" in result.stderr
    assert "至少一个 lumen systemd unit 启动失败" in result.stderr
    assert "迁移完成" not in result.stdout


def test_only_services_originally_active_are_stopped_and_started(
    tmp_path: Path,
) -> None:
    result, root, calls = _run_migration(
        tmp_path,
        active_units=("lumen-web.service", "lumen-api.service"),
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (root / "current").is_symlink()
    assert "stop lumen-web.service" in calls
    assert "stop lumen-api.service" in calls
    assert "start lumen-api.service" in calls
    assert "start lumen-web.service" in calls
    assert "stop lumen-worker.service" not in calls
    assert "stop lumen-tgbot.service" not in calls
    assert "start lumen-worker.service" not in calls
    assert "start lumen-tgbot.service" not in calls
    assert not Path(f"{root}.migrate-to-releases.lock.d").exists()


def test_delayed_unit_exit_fails_health_and_rolls_back(
    tmp_path: Path,
) -> None:
    result, root, calls = _run_migration(
        tmp_path,
        inactive_after_start_unit="lumen-worker.service",
        active_after_start_successes=1,
        active_attempts=3,
        active_stable_polls=2,
    )

    assert result.returncode == 70
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "current").exists()
    assert not (root / "releases").exists()
    assert not (root / "shared").exists()
    assert not Path(f"{root}.tmp").exists()
    lock_dir = Path(f"{root}.migrate-to-releases.lock.d")
    assert lock_dir.is_dir()
    assert list(lock_dir.glob(".owner.*/phase"))
    assert "启动后未能持续 active" in result.stderr
    assert "恢复证据与 owner 锁已保留" in result.stderr
    assert "迁移完成" not in result.stdout
    assert "start lumen-worker.service" in calls


def test_active_worker_without_worker_readiness_retains_migration_journal(
    tmp_path: Path,
) -> None:
    result, root, calls = _run_migration(
        tmp_path,
        active_units=("lumen-worker.service",),
        worker_ready=False,
    )

    assert result.returncode == 70
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "current").exists()
    lock_dir = Path(f"{root}.migrate-to-releases.lock.d")
    assert lock_dir.is_dir()
    assert list(lock_dir.glob(".owner.*/phase"))
    assert "Worker python -m app.worker_health check 未通过" in result.stderr
    assert "恢复证据与 owner 锁已保留" in result.stderr
    assert "start lumen-worker.service" in calls
    worker_calls = (
        tmp_path / "worker-health.log"
    ).read_text(encoding="utf-8").splitlines()
    assert worker_calls
    assert set(worker_calls) == {
        f"-m app.worker_health check --expected-owner-uid {os.getuid()}"
    }


def test_worker_and_tgbot_require_sustained_active_polls(tmp_path: Path) -> None:
    result, root, calls = _run_migration(
        tmp_path,
        active_units=("lumen-worker.service", "lumen-tgbot.service"),
        active_attempts=2,
        active_stable_polls=2,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (root / "current").is_symlink()
    assert calls.count("is-active --quiet lumen-worker.service") == 3
    assert calls.count("is-active --quiet lumen-tgbot.service") == 3
    assert not (tmp_path / "curl.log").exists() \
        or (tmp_path / "curl.log").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("failed_service", "expected_error"),
    (
        ("api", "API /readyz 未通过"),
        ("web", "Web 健康检查失败"),
    ),
)
def test_api_web_http_health_is_bounded_configurable_and_rolls_back(
    tmp_path: Path,
    failed_service: str,
    expected_error: str,
) -> None:
    api_url = "http://127.0.0.1:18000/test-api-health"
    web_url = "http://127.0.0.1:13000/test-web-health"
    failed_url = api_url if failed_service == "api" else web_url
    result, root, _ = _run_migration(
        tmp_path,
        active_units=("lumen-api.service", "lumen-web.service"),
        http_fail_urls=(failed_url,),
        api_health_url=api_url,
        web_health_url=web_url,
        api_health_attempts=2,
        web_health_attempts=2,
    )

    assert result.returncode != 0
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "current").exists()
    assert not (root / "releases").exists()
    assert not (root / "shared").exists()
    assert not Path(f"{root}.tmp").exists()
    assert expected_error in result.stderr
    assert "迁移完成" not in result.stdout
    expected_calls = (
        [api_url, web_url, api_url]
        if failed_service == "api"
        else [api_url, web_url, web_url, api_url]
    )
    assert (
        tmp_path / "curl.log"
    ).read_text(encoding="utf-8").splitlines() == expected_calls


@pytest.mark.parametrize(
    ("signal_name", "expected_rc"),
    (("HUP", 129), ("INT", 130), ("TERM", 143)),
)
def test_signal_at_first_stop_recovers_before_any_directory_move(
    tmp_path: Path,
    signal_name: str,
    expected_rc: int,
) -> None:
    process, root, lock_dir, _, ready, go = _prepare_signal_migration(
        tmp_path,
        signal_command="stop lumen-web.service",
        signal_name=signal_name,
        active_units=("lumen-web.service",),
    )
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        assert (owner_dirs[0] / "phase").read_text(encoding="utf-8") == "stopping\n"
        go.touch()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == expected_rc, stderr + stdout
    _assert_original_layout_restored(root)
    assert not lock_dir.exists()
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert calls.count("start lumen-web.service") == 1
    assert not any(
        call.startswith("start ")
        and call != "start lumen-web.service"
        for call in calls
    )


def test_signal_after_shared_extraction_restores_dirs_links_and_env(
    tmp_path: Path,
) -> None:
    process, root, lock_dir, _, ready, go = _prepare_signal_migration(
        tmp_path,
        signal_command="start lumen-api.service",
        signal_name="TERM",
    )
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        state_dir = owner_dirs[0]
        assert (state_dir / "phase").read_text(encoding="utf-8") == "starting\n"
        moved = (state_dir / "moved.manifest").read_bytes()
        assert b"top\tpayload.txt\0" in moved
        assert b"env\t.env\0" in moved
        assert (root / "current").is_symlink()
        assert (root / ".env").is_symlink()
        assert (root / "current/apps/web/.env.local").exists()
        go.touch()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 143, stderr + stdout
    _assert_original_layout_restored(root)
    assert not lock_dir.exists()
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    assert "start lumen-worker.service" not in calls
    assert "start lumen-tgbot.service" not in calls


def test_signal_between_move_and_moved_ack_uses_intent_manifest(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "lumen/payload.txt"
    process, root, lock_dir, _, ready, go = _prepare_signal_migration(
        tmp_path,
        signal_command="",
        signal_name="TERM",
        active_units=("lumen-web.service",),
        mv_signal_source=payload,
    )
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        state_dir = owner_dirs[0]
        assert (state_dir / "phase").read_text(encoding="utf-8") == "moving\n"
        intent = (state_dir / "move-intent.manifest").read_bytes()
        moved = (state_dir / "moved.manifest").read_bytes()
        assert b"top\tpayload.txt\0" in intent
        assert b"top\tpayload.txt\0" not in moved
        assert not payload.exists()
        go.touch()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 143, stderr + stdout
    _assert_original_layout_restored(root)
    assert not lock_dir.exists()


def test_sigkill_after_durable_move_before_ack_recovers_and_reruns(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, root, lock_dir, _, ready, _go = _prepare_signal_migration(
        tmp_path,
        signal_command="",
        signal_name="TERM",
        active_units=("lumen-web.service",),
        migration_failpoint="after_move_before_ack",
        migration_failpoint_kind="top",
        migration_failpoint_name="payload.txt",
        env_out=env,
    )
    tmp_payload = Path(f"{root}.tmp") / "releases/initial/payload.txt"
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        state_dir = owner_dirs[0]
        assert (state_dir / "phase").read_text(encoding="utf-8") == "moving\n"
        assert b"top\tpayload.txt\0" in (
            state_dir / "move-intent.manifest"
        ).read_bytes()
        assert b"top\tpayload.txt\0" not in (
            state_dir / "moved.manifest"
        ).read_bytes()
        assert tmp_payload.read_text(encoding="utf-8") == "keep-me\n"
        assert not (root / "payload.txt").exists()
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    assert process.returncode == -signal.SIGKILL
    rerun = _rerun_interrupted_migration(env)

    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "上次强制中断的迁移已恢复" in rerun.stderr + rerun.stdout
    assert (root / "current").is_symlink()
    assert (
        root / "releases/initial/payload.txt"
    ).read_text(encoding="utf-8") == "keep-me\n"
    assert not Path(f"{root}.tmp").exists()
    assert not lock_dir.exists()


def test_lost_move_intent_preserves_ambiguous_staging_on_sigkill_rerun(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, root, lock_dir, _, ready, _go = _prepare_signal_migration(
        tmp_path,
        signal_command="",
        signal_name="TERM",
        active_units=("lumen-web.service",),
        migration_failpoint="after_move_before_ack",
        migration_failpoint_kind="top",
        migration_failpoint_name="payload.txt",
        env_out=env,
    )
    tmp_payload = Path(f"{root}.tmp") / "releases/initial/payload.txt"
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        intent_path = owner_dirs[0] / "move-intent.manifest"
        intent = intent_path.read_bytes()
        payload_record = b"top\tpayload.txt\0"
        assert payload_record in intent
        _durably_write_bytes(intent_path, intent.replace(payload_record, b""))
        assert tmp_payload.read_text(encoding="utf-8") == "keep-me\n"
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    assert process.returncode == -signal.SIGKILL
    rerun = _rerun_interrupted_migration(env)

    assert rerun.returncode == 70, rerun.stderr + rerun.stdout
    assert "entry without durable intent: payload.txt" in rerun.stderr
    assert "保留现场，拒绝递归删除" in rerun.stderr
    assert tmp_payload.read_text(encoding="utf-8") == "keep-me\n"
    assert not (root / "payload.txt").exists()
    assert Path(f"{root}.tmp").is_dir()
    assert lock_dir.is_dir()


def test_signal_cleanup_cannot_delete_successor_migration_lock(
    tmp_path: Path,
) -> None:
    process, root, lock_dir, _, ready, go = _prepare_signal_migration(
        tmp_path,
        signal_command="stop lumen-web.service",
        signal_name="TERM",
        active_units=("lumen-web.service",),
    )
    displaced = tmp_path / "displaced-migration-lock"
    successor = lock_dir / ".owner.successor" / "owner"
    try:
        _wait_for_file(ready)
        lock_dir.rename(displaced)
        successor.parent.mkdir(parents=True)
        successor.write_text(
            "pid=999999\n"
            "start_token=successor-token\n"
            "owner_id=.owner.successor\n"
            "script=successor.sh\n",
            encoding="utf-8",
        )
        go.touch()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 143, stderr + stdout
    assert (root / "payload.txt").read_text(encoding="utf-8") == "keep-me\n"
    assert successor.is_file()
    assert "script=successor.sh" in successor.read_text(encoding="utf-8")
    assert displaced.is_dir()


def test_sigkill_after_current_creation_is_recovered_and_rerun_to_completion(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, root, lock_dir, _, ready, _go = _prepare_signal_migration(
        tmp_path,
        signal_command="start lumen-api.service",
        signal_name="TERM",
        env_out=env,
    )
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        assert (owner_dirs[0] / "phase").read_text(encoding="utf-8") == (
            "starting\n"
        )
        assert (root / "current").is_symlink()
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    assert process.returncode == -signal.SIGKILL
    assert lock_dir.is_dir()

    rerun = _rerun_interrupted_migration(env)

    assert rerun.returncode == 0, rerun.stderr + rerun.stdout
    assert "上次强制中断的迁移已恢复" in rerun.stderr + rerun.stdout
    assert (root / "current").is_symlink()
    assert os.readlink(root / "current") == "releases/initial"
    assert (root / "releases/initial/.lumen_release.json").is_file()
    assert not lock_dir.exists()
    assert "拒绝误报迁移完成" not in rerun.stderr


def test_unknown_stale_migration_phase_is_not_reported_complete(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, root, lock_dir, _, ready, _go = _prepare_signal_migration(
        tmp_path,
        signal_command="start lumen-api.service",
        signal_name="TERM",
        env_out=env,
    )
    try:
        _wait_for_file(ready)
        owner_dirs = list(lock_dir.glob(".owner.*"))
        assert len(owner_dirs) == 1
        (owner_dirs[0] / "phase").write_text("unknown\n", encoding="utf-8")
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    rerun = _rerun_interrupted_migration(env)

    assert rerun.returncode != 0
    assert "phase 未知" in rerun.stderr
    assert "迁移已完成" not in rerun.stdout
    assert (root / "current").is_symlink()
    assert lock_dir.is_dir()
