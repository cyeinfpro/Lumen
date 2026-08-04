from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tarfile
import textwrap
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "backup.sh"


def _wait_for_file(path: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _write_fake_docker(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            import sys
            import time
            from pathlib import Path

            args = sys.argv[1:]
            marker = Path(os.environ["TEST_PHASE_MARKER"])
            log_path = Path(os.environ["TEST_DOCKER_LOG"])
            state_path = Path(os.environ["TEST_DOCKER_STATE"])
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(" ".join(args) + "\\n")

            def block_here() -> None:
                marker.write_text("ready\\n", encoding="utf-8")
                time.sleep(60)

            def set_service_state(service: str, running: bool) -> None:
                (state_path.parent / f"service-{service}").write_text(
                    "true" if running else "false",
                    encoding="utf-8",
                )

            def load_redis_state() -> dict[str, str]:
                return dict(
                    line.split("=", 1)
                    for line in state_path.read_text().splitlines()
                    if "=" in line
                )

            def save_redis_state(values: dict[str, str]) -> None:
                state_path.write_text(
                    "".join(f"{key}={value}\\n" for key, value in values.items()),
                    encoding="utf-8",
                )

            if args and args[0] == "compose":
                if "exec" in args:
                    if os.environ.get("TEST_WORKER_READY", "1") != "1":
                        raise SystemExit(1)
                    raise SystemExit(0)
                action = next(
                    (item for item in args if item in {"start", "stop"}),
                    "",
                )
                if action:
                    index = args.index(action)
                    for service in args[index + 1 :]:
                        set_service_state(service, action == "start")
                raise SystemExit(0)

            if args and args[0] == "inspect":
                service = args[-1].removeprefix("lumen-")
                if os.environ.get("TEST_FAIL_INSPECT_SERVICE") == service:
                    print("docker daemon unavailable", file=sys.stderr)
                    raise SystemExit(55)
                service_state = state_path.parent / f"service-{service}"
                print(
                    service_state.read_text(encoding="utf-8")
                    if service_state.exists()
                    else "true"
                )
                raise SystemExit(0)

            if args and args[0] == "exec":
                index = 1
                while index < len(args) and args[index] in {"-i", "-e"}:
                    index += 2 if args[index] == "-e" else 1
                index += 1
                command = args[index] if index < len(args) else ""
                rest = args[index + 1 :]
                if command == "pg_dump":
                    sys.stdout.buffer.write(b"postgres-dump-bytes")
                    sys.stdout.buffer.flush()
                    if os.environ.get("TEST_BLOCK_PHASE") == "pg_dump":
                        block_here()
                    raise SystemExit(0)
                if command == "redis-cli":
                    redis_command = next(
                        (
                            item
                            for item in rest
                            if item in {"PING", "LASTSAVE", "BGSAVE", "INFO"}
                        ),
                        "",
                    )
                    if redis_command == "PING":
                        print("PONG")
                    elif redis_command == "LASTSAVE":
                        values = load_redis_state()
                        print(values["lastsave"])
                    elif redis_command == "BGSAVE":
                        mode = os.environ.get("TEST_BGSAVE_MODE", "success")
                        values = load_redis_state()
                        if values.get("in_progress") == "1":
                            print("ERR Background save already in progress")
                            raise SystemExit(0)
                        if mode != "unchanged":
                            values["lastsave"] = str(int(values["lastsave"]) + 1)
                            values["rdb_saves"] = str(int(values["rdb_saves"]) + 1)
                            values["bgsave_started"] = str(
                                int(values.get("bgsave_started", "0")) + 1
                            )
                            values["in_progress"] = "0"
                            save_redis_state(values)
                        print("Background saving started")
                    elif redis_command == "INFO":
                        values = load_redis_state()
                        print(
                            "rdb_bgsave_in_progress:"
                            + os.environ.get(
                                "TEST_RDB_IN_PROGRESS",
                                values.get("in_progress", "0"),
                            )
                        )
                        print(
                            "rdb_last_bgsave_status:"
                            + os.environ.get("TEST_RDB_STATUS", "ok")
                        )
                        if os.environ.get("TEST_OMIT_RDB_SAVES") != "1":
                            print(f"rdb_saves:{values['rdb_saves']}")
                        if (
                            mode := os.environ.get("TEST_BGSAVE_MODE", "success")
                        ) == "preexisting" and values.get("in_progress") == "1":
                            values["lastsave"] = str(int(values["lastsave"]) + 1)
                            values["rdb_saves"] = str(int(values["rdb_saves"]) + 1)
                            values["in_progress"] = "0"
                            save_redis_state(values)
                    raise SystemExit(0)
                if command == "redis-check-rdb":
                    raise SystemExit(
                        0 if os.environ.get("TEST_RDB_VALID", "1") == "1" else 1
                    )
                raise SystemExit(0)

            if args and args[0] == "cp":
                source = args[1]
                destination = Path(args[2])
                if ":" in args[2] and not source.startswith("lumen-redis:"):
                    raise SystemExit(0)
                if source.endswith("/dump.rdb"):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    values = load_redis_state()
                    destination.write_bytes(
                        f"redis-dump-rdb-saves={values['rdb_saves']}".encode()
                    )
                    raise SystemExit(0)
                if source.endswith("/appendonlydir"):
                    mode = os.environ.get("TEST_APPENDONLYDIR_MODE", "success")
                    if mode == "success":
                        destination.mkdir(parents=True, exist_ok=True)
                        (destination / "part.aof").write_bytes(b"redis-aof")
                        raise SystemExit(0)
                    if mode == "partial-fail":
                        destination.mkdir(parents=True, exist_ok=True)
                        (destination / "partial.aof").write_bytes(b"partial")
                        print("input/output error", file=sys.stderr)
                        raise SystemExit(1)
                    print("not found", file=sys.stderr)
                    raise SystemExit(1)
                if source.endswith("/appendonly.aof"):
                    mode = os.environ.get("TEST_APPENDONLY_FILE_MODE", "missing")
                    if mode == "success":
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(b"redis-aof")
                        raise SystemExit(0)
                    if mode == "partial-fail":
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(b"partial")
                        print("input/output error", file=sys.stderr)
                        raise SystemExit(1)
                    print("not found", file=sys.stderr)
                    raise SystemExit(1)
                print("not found", file=sys.stderr)
                raise SystemExit(1)

            if args and args[0] == "ps":
                raise SystemExit(0)
            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_curl_wrapper(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            url="${@: -1}"
            printf '%s\n' "$url" >> "${TEST_CURL_LOG:?}"
            if [[ "$url" == */readyz ]] \
                    && [[ "${TEST_API_READY:-1}" != "1" ]]; then
                exit 22
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_tar_wrapper(path: Path) -> None:
    real_tar = shutil.which("tar")
    assert real_tar is not None
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -u
            {shlex.quote(real_tar)} "$@"
            rc=$?
            if [ "$rc" -eq 0 ] && [ "${{TEST_BLOCK_PHASE:-}}" = "redis_archive" ]; then
                case " $* " in
                    *" -czf "*)
                        printf 'ready\\n' > "${{TEST_PHASE_MARKER:?}}"
                        sleep 60
                        ;;
                esac
            fi
            exit "$rc"
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_sleep_wrapper(path: Path) -> None:
    real_sleep = shutil.which("sleep")
    assert real_sleep is not None
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [ "${{1:-}}" = "1" ]; then
                exit 0
            fi
            exec {shlex.quote(real_sleep)} "$@"
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _start_backup(
    tmp_path: Path,
    *,
    block_phase: str,
    bgsave_mode: str = "success",
    rdb_status: str = "ok",
    omit_rdb_saves: bool = False,
    appendonlydir_mode: str = "success",
    appendonly_file_mode: str = "missing",
    inspect_failure_service: str = "",
    failpoint: str = "",
    api_ready: bool = True,
    worker_ready: bool = True,
    rdb_valid: bool = True,
    env_out: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Path, Path, Path, Path]:
    backup_root = tmp_path / "backup"
    maint_root = tmp_path / "maint"
    fakebin = tmp_path / "bin"
    marker = tmp_path / "phase.ready"
    docker_log = tmp_path / "docker.log"
    docker_state = tmp_path / "docker.state"
    running_file = backup_root / ".backup.running"
    lock_file = tmp_path / "backup-restore.lock"
    backup_root.mkdir()
    maint_root.mkdir()
    fakebin.mkdir()
    _write_fake_docker(fakebin / "docker")
    _write_curl_wrapper(fakebin / "curl")
    _write_tar_wrapper(fakebin / "tar")
    _write_sleep_wrapper(fakebin / "sleep")
    preexisting_bgsave = bgsave_mode in {"preexisting", "preexisting-stuck"}
    docker_state.write_text(
        "\n".join(
            [
                "lastsave=100",
                "rdb_saves=1",
                f"in_progress={1 if preexisting_bgsave else 0}",
                "bgsave_started=0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "BACKUP_ROOT": str(backup_root),
            "TMPDIR": str(tmp_path / "tmp"),
            "LUMEN_MAINT_ROOT": str(maint_root),
            "LUMEN_BACKUP_RESTORE_LOCKFILE": str(lock_file),
            "LUMEN_BACKUP_SERVICE_MODE": "1",
            "LUMEN_BACKUP_RUNNING_FILE": str(running_file),
            "LUMEN_BACKUP_TRIGGER_FILE": str(backup_root / ".backup.trigger"),
            "LUMEN_BACKUP_PENDING_FILE": str(backup_root / ".backup.pending"),
            "DB_USER": "lumen",
            "DB_NAME": "lumen",
            "TEST_PHASE_MARKER": str(marker),
            "TEST_DOCKER_LOG": str(docker_log),
            "TEST_DOCKER_STATE": str(docker_state),
            "TEST_CURL_LOG": str(tmp_path / "curl.log"),
            "TEST_BLOCK_PHASE": block_phase,
            "TEST_BGSAVE_MODE": bgsave_mode,
            "TEST_RDB_STATUS": rdb_status,
            "TEST_OMIT_RDB_SAVES": "1" if omit_rdb_saves else "0",
            "TEST_APPENDONLYDIR_MODE": appendonlydir_mode,
            "TEST_APPENDONLY_FILE_MODE": appendonly_file_mode,
            "TEST_FAIL_INSPECT_SERVICE": inspect_failure_service,
            "TEST_API_READY": "1" if api_ready else "0",
            "TEST_WORKER_READY": "1" if worker_ready else "0",
            "TEST_RDB_VALID": "1" if rdb_valid else "0",
            "LUMEN_BACKUP_FAILPOINT": failpoint,
            "LUMEN_CORE_READINESS_ATTEMPTS": "1",
            "LUMEN_CORE_READINESS_INTERVAL_SECONDS": "0",
            "LUMEN_SERVICE_STATE_INTERVAL_SECONDS": "0",
            "LUMEN_SERVICE_QUIESCE_ATTEMPTS": "4",
            "LUMEN_SERVICE_START_ATTEMPTS": "4",
        }
    )
    if env_out is not None:
        env_out.update(env)
    (tmp_path / "tmp").mkdir()
    shell = """
command() {
    if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
        return 1
    fi
    builtin command "$@"
}
. "$1"
"""
    process = subprocess.Popen(
        ["/bin/bash", "-c", shell, "backup-signal-test", str(BACKUP)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    return process, marker, backup_root, maint_root, lock_file


def _run_backup_recovery(
    tmp_path: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    recovery_env = env.copy()
    recovery_maint = tmp_path / "maint-recovery"
    recovery_maint.mkdir(exist_ok=True)
    recovery_env.update(
        {
            "LUMEN_MAINT_ROOT": str(recovery_maint),
            "LUMEN_BACKUP_RESTORE_LOCKFILE": str(tmp_path / "backup-recovery.lock"),
            "TEST_BLOCK_PHASE": "",
        }
    )
    shell = """
command() {
    if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
        return 1
    fi
    builtin command "$@"
}
. "$1"
"""
    return subprocess.run(
        ["/bin/bash", "-c", shell, "backup-recovery-test", str(BACKUP)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=recovery_env,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("block_phase", ["pg_dump", "redis_archive"])
def test_sighup_removes_partial_pair_markers_and_both_owned_locks(
    tmp_path: Path,
    block_phase: str,
) -> None:
    process, marker, backup_root, maint_root, lock_file = _start_backup(
        tmp_path,
        block_phase=block_phase,
    )
    _wait_for_file(marker)
    maintenance_lock = maint_root / ".lumen-maintenance.lock.d"
    pair_lock = Path(f"{lock_file}.d")
    assert maintenance_lock.is_dir()
    assert pair_lock.is_dir()

    os.killpg(process.pid, signal.SIGHUP)
    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 129, output
    assert "interrupted by SIGHUP" in output
    assert not (backup_root / ".backup.running").exists()
    assert not list((backup_root / "pg").glob("*"))
    assert not list((backup_root / "redis").glob("*"))
    assert not list(backup_root.glob(".pg-dump*"))
    assert not maintenance_lock.exists()
    assert not pair_lock.exists()
    calls = (backup_root.parent / "docker.log").read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot stop api worker tgbot" in calls
    assert "compose --ansi=never --profile tgbot start api worker tgbot" in calls


def test_backup_enables_signal_handlers_only_after_both_locks_are_recorded() -> None:
    text = BACKUP.read_text(encoding="utf-8")
    runtime = text.index("trap cleanup EXIT", text.index("wait_for_redis_bgsave"))
    ignored = text.index("trap '' INT TERM HUP", runtime)
    maintenance = text.index("lumen_try_acquire_lock", ignored)
    cleanup_restored = text.index("trap cleanup EXIT", maintenance)
    pair_lock = text.index("\nacquire_lock\n", cleanup_restored)
    signal_enabled = text.index("trap 'on_signal INT' INT", pair_lock)

    assert runtime < ignored < maintenance < cleanup_restored < pair_lock
    assert pair_lock < signal_enabled


def test_successful_backup_freezes_all_writers_before_both_snapshots(
    tmp_path: Path,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
    )

    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == 0, stdout + stderr
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    stop_index = calls.index(
        "compose --ansi=never --profile tgbot stop api worker tgbot"
    )
    pg_index = next(index for index, call in enumerate(calls) if "pg_dump" in call)
    bgsave_index = next(
        index for index, call in enumerate(calls) if "redis-cli BGSAVE" in call
    )
    start_index = calls.index(
        "compose --ansi=never --profile tgbot start api worker tgbot"
    )
    assert stop_index < pg_index < bgsave_index < start_index
    assert len(list((backup_root / "pg").glob("*.pg.dump.gz"))) == 1
    assert len(list((backup_root / "redis").glob("*.redis.tgz"))) == 1
    markers = list(backup_root.glob(".backup-pair.*.json"))
    assert len(markers) == 1
    marker_payload = json.loads(markers[0].read_text(encoding="utf-8"))
    assert marker_payload["schema"] == 1
    assert marker_payload["pg"]["sha256"] == hashlib.sha256(
        next((backup_root / "pg").glob("*.pg.dump.gz")).read_bytes()
    ).hexdigest()
    assert marker_payload["redis"]["sha256"] == hashlib.sha256(
        next((backup_root / "redis").glob("*.redis.tgz")).read_bytes()
    ).hexdigest()


def test_backup_waits_out_preexisting_bgsave_then_starts_fresh_generation(
    tmp_path: Path,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        bgsave_mode="preexisting",
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 0, output
    state = dict(
        line.split("=", 1)
        for line in (tmp_path / "docker.state").read_text().splitlines()
        if "=" in line
    )
    assert state["rdb_saves"] == "3"
    assert state["bgsave_started"] == "1"
    archive = next((backup_root / "redis").glob("*.redis.tgz"))
    with tarfile.open(archive, "r:gz") as bundle:
        dump = bundle.extractfile("dump.rdb")
        assert dump is not None
        assert dump.read() == b"redis-dump-rdb-saves=3"


def test_backup_aborts_when_preexisting_bgsave_never_becomes_idle(
    tmp_path: Path,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        bgsave_mode="preexisting-stuck",
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 3, output
    assert "did not become idle in 60s" in output
    assert not list(backup_root.glob(".backup-pair.*.json"))
    state = dict(
        line.split("=", 1)
        for line in (tmp_path / "docker.state").read_text().splitlines()
        if "=" in line
    )
    assert state["bgsave_started"] == "0"


def test_backup_publication_fsyncs_payloads_and_marker_before_retention() -> None:
    text = BACKUP.read_text(encoding="utf-8")
    pg_temp_fsync = text.index('backup_fsync_file "$PG_TMP"')
    pg_rename = text.index('mv -f "$PG_TMP" "$PG_OUT"', pg_temp_fsync)
    pg_dir_fsync = text.index('backup_fsync_directory "$PG_DIR"', pg_rename)
    redis_temp_fsync = text.index('backup_fsync_file "$REDIS_TMP"', pg_dir_fsync)
    redis_rename = text.index('mv -f "$REDIS_TMP" "$REDIS_OUT"', redis_temp_fsync)
    redis_dir_fsync = text.index(
        'backup_fsync_directory "$REDIS_DIR"',
        redis_rename,
    )
    marker_commit = text.index("publish_backup_pair_marker", redis_dir_fsync)
    retention = text.index("prune_paired", marker_commit)

    assert (
        pg_temp_fsync
        < pg_rename
        < pg_dir_fsync
        < redis_temp_fsync
        < redis_rename
        < redis_dir_fsync
        < marker_commit
        < retention
    )


@pytest.mark.parametrize(
    ("failpoint", "pg_exists", "redis_exists", "marker_exists"),
    [
        ("after_pg_rename", True, False, False),
        ("after_redis_rename", True, True, False),
        ("after_pair_marker", True, True, True),
    ],
)
def test_backup_crash_failpoints_expose_only_marker_committed_pairs(
    tmp_path: Path,
    failpoint: str,
    pg_exists: bool,
    redis_exists: bool,
    marker_exists: bool,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        failpoint=failpoint,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == -signal.SIGKILL, output
    pg_files = list((backup_root / "pg").glob("*.pg.dump.gz"))
    redis_files = list((backup_root / "redis").glob("*.redis.tgz"))
    pair_markers = list(backup_root.glob(".backup-pair.*.json"))
    assert bool(pg_files) is pg_exists
    assert bool(redis_files) is redis_exists
    assert bool(pair_markers) is marker_exists
    if pair_markers:
        payload = json.loads(pair_markers[0].read_text(encoding="utf-8"))
        assert payload["pg"]["sha256"] == hashlib.sha256(
            pg_files[0].read_bytes()
        ).hexdigest()
        assert payload["redis"]["sha256"] == hashlib.sha256(
            redis_files[0].read_bytes()
        ).hexdigest()


def test_backup_aborts_before_stopping_services_when_state_snapshot_fails(
    tmp_path: Path,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        inspect_failure_service="worker",
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 70, output
    assert "failed to capture the pre-backup writer state" in output
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert " compose " not in f" {calls} "
    assert "pg_dump" not in calls
    assert not list((backup_root / "pg").glob("*"))
    assert not list((backup_root / "redis").glob("*"))


def test_sigkill_leaves_durable_consumer_state_and_next_run_restores_writers(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="pg_dump",
        env_out=env,
    )
    _wait_for_file(marker)

    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=15)

    journal = backup_root / ".recovery" / "backup.json"
    assert process.returncode == -signal.SIGKILL
    assert journal.is_file()
    for service in ("api", "worker", "tgbot"):
        assert (tmp_path / f"service-{service}").read_text(encoding="utf-8") == (
            "false"
        )

    recovered = _run_backup_recovery(tmp_path, env)

    assert recovered.returncode == 0, recovered.stderr + recovered.stdout
    assert not journal.exists()
    for service in ("api", "worker", "tgbot"):
        assert (tmp_path / f"service-{service}").read_text(encoding="utf-8") == (
            "true"
        )
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines()
    assert calls.count(
        "compose --ansi=never --profile tgbot start api worker tgbot"
    ) == 1


def test_unknown_backup_journal_state_is_preserved_and_never_restarts_writers(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="pg_dump",
        env_out=env,
    )
    _wait_for_file(marker)
    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=15)
    journal = backup_root / ".recovery" / "backup.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["phase"] = "unknown"
    journal.write_text(json.dumps(payload), encoding="utf-8")
    journal.chmod(0o600)
    before = (tmp_path / "docker.log").read_text(encoding="utf-8")

    recovered = _run_backup_recovery(tmp_path, env)

    assert recovered.returncode == 70
    assert journal.exists()
    after = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert after == before


def test_backup_systemd_consumer_watches_journal_and_restarts_failed_unit() -> None:
    path_unit = (
        ROOT / "deploy/systemd/lumen-backup.path"
    ).read_text(encoding="utf-8")
    service = (
        ROOT / "deploy/systemd/lumen-backup.service"
    ).read_text(encoding="utf-8")

    assert "PathExists=/opt/lumendata/backup/.recovery/backup.json" in path_unit
    assert "Restart=on-failure" in service


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        ({"bgsave_mode": "unchanged"}, "did not complete"),
        ({"rdb_status": "err"}, "last BGSAVE status is err"),
        ({"omit_rdb_saves": True}, "rdb_saves is unavailable before BGSAVE"),
    ],
)
def test_backup_rejects_unproven_redis_bgsave_completion(
    tmp_path: Path,
    kwargs: dict[str, object],
    expected_error: str,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        **kwargs,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 3, output
    assert expected_error in output
    assert not list((backup_root / "pg").glob("*"))
    assert not list((backup_root / "redis").glob("*"))
    calls = (tmp_path / "docker.log").read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot start api worker tgbot" in calls


def test_partial_live_aof_is_ignored_and_archive_remains_rdb_only(
    tmp_path: Path,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        appendonlydir_mode="partial-fail",
    )

    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 0, stdout + stderr
    archives = list((backup_root / "redis").glob("*.redis.tgz"))
    assert len(archives) == 1
    listing = subprocess.run(
        ["tar", "-tzf", str(archives[0])],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert any(entry.endswith("dump.rdb") for entry in listing)
    assert not any("appendonly" in entry for entry in listing)
    assert not list((tmp_path / "tmp").glob("lumen-backup.*"))


def test_missing_optional_aof_still_produces_dump_only_archive(
    tmp_path: Path,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        appendonlydir_mode="missing",
        appendonly_file_mode="missing",
    )

    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == 0, stdout + stderr
    archives = list((backup_root / "redis").glob("*.redis.tgz"))
    assert len(archives) == 1
    listing = subprocess.run(
        ["tar", "-tzf", str(archives[0])],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert any(entry.endswith("dump.rdb") for entry in listing)
    assert not any("appendonly" in entry for entry in listing)


@pytest.mark.parametrize(
    ("api_ready", "worker_ready", "expected_error"),
    [
        (False, True, "API /readyz 未通过"),
        (True, False, "Worker python -m app.worker_health check 未通过"),
    ],
)
def test_backup_restart_readiness_failure_retains_journal_and_pair(
    tmp_path: Path,
    api_ready: bool,
    worker_ready: bool,
    expected_error: str,
) -> None:
    process, _marker, backup_root, _maint_root, _lock_file = _start_backup(
        tmp_path,
        block_phase="",
        api_ready=api_ready,
        worker_ready=worker_ready,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 70, output
    assert expected_error in output
    assert (backup_root / ".recovery" / "backup.json").is_file()
    assert len(list(backup_root.glob(".backup-pair.*.json"))) == 1
    assert len(list((backup_root / "redis").glob("*.redis.tgz"))) == 1
    probed = (tmp_path / "curl.log").read_text(encoding="utf-8").splitlines()
    assert probed
    assert set(probed) == {"http://127.0.0.1:8000/readyz"}
