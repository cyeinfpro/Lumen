from __future__ import annotations

import os
import shlex
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


def _write_systemctl_mock(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -u
            printf '%s\\n' "$*" >> "${SYSTEMCTL_LOG:?}"
            case "${1:-}" in
                list-unit-files)
                    printf '%s enabled\\n' "${2:?}"
                    ;;
                is-active)
                    exit 0
                    ;;
                stop)
                    if [ "${2:-}" = "${SYSTEMCTL_FAIL_STOP_UNIT:-}" ]; then
                        exit 1
                    fi
                    ;;
                start)
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


def _run_migration(
    tmp_path: Path,
    *,
    fail_stop: str = "",
    fail_start: str = "",
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    root = tmp_path / "lumen"
    data_root = tmp_path / "lumendata"
    fakebin = tmp_path / "bin"
    systemctl_log = tmp_path / "systemctl.log"
    root.mkdir()
    data_root.mkdir()
    fakebin.mkdir()
    (root / "payload.txt").write_text("keep-me\n", encoding="utf-8")
    _write_systemctl_mock(fakebin / "systemctl")

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
        }
    )
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
        "stop lumen-tgbot.service",
        "list-unit-files lumen-web.service --no-legend",
        "is-active --quiet lumen-web.service",
        "stop lumen-web.service",
        "list-unit-files lumen-worker.service --no-legend",
        "is-active --quiet lumen-worker.service",
        "stop lumen-worker.service",
        "is-active --quiet lumen-worker.service",
        "start lumen-web.service",
        "start lumen-tgbot.service",
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
    assert (root / "current").is_symlink()
    assert (root / "releases" / "initial" / "payload.txt").is_file()
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
