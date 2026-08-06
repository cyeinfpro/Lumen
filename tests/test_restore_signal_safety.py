from __future__ import annotations

import gzip
import hashlib
import json
import os
import shlex
import signal
import subprocess
import tarfile
import tempfile
import textwrap
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESTORE = ROOT / "scripts" / "restore.sh"
TS = "20260802-010203"
SYSTEMD_WRITER_UNITS = (
    "lumen-api.service",
    "lumen-worker.service",
    "lumen-tgbot.service",
)
APPLICATION_SERVICES = ("api", "worker", "tgbot", "web")


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
            import re
            import sys
            import time
            from pathlib import Path

            args = sys.argv[1:]
            state = Path(os.environ["TEST_STATE_DIR"])
            db_dir = state / "dbs"
            log_path = Path(os.environ["TEST_DOCKER_LOG"])
            marker = Path(os.environ["TEST_PHASE_MARKER"])

            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(" ".join(args) + "\\n")

            def db_path(name: str) -> Path:
                return db_dir / name

            def block_here() -> None:
                marker.write_text("ready\\n", encoding="utf-8")
                time.sleep(60)

            def service_state_path(service: str) -> Path:
                return state / f"service-{service}"

            def bootstrap_state_path() -> Path:
                return state / "redis-bootstrap-running"

            def set_service_state(service: str, running: bool) -> None:
                service_state_path(service).write_text(
                    "true" if running else "false",
                    encoding="utf-8",
                )

            def active_database_is_restored() -> bool:
                active = db_path("lumen")
                return (
                    active.exists()
                    and active.read_text(encoding="utf-8") == "restored\\n"
                )

            if args[:2] == ["compose", "version"]:
                raise SystemExit(0)
            if args and args[0] == "compose":
                if "exec" in args:
                    if (
                        os.environ.get("TEST_WORKER_READY", "1") != "1"
                        and (
                            os.environ.get(
                                "TEST_READY_FAILURE_RESTORED_ONLY",
                                "0",
                            )
                            != "1"
                            or active_database_is_restored()
                        )
                    ):
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
                if "-f" in args and "{{.State.Running}}" in args:
                    if args[-1].startswith("lumen-redis-restore-bootstrap-"):
                        print(
                            bootstrap_state_path().read_text(encoding="utf-8")
                            if bootstrap_state_path().exists()
                            else "false"
                        )
                        raise SystemExit(0)
                    service = args[-1].removeprefix("lumen-")
                    if os.environ.get("TEST_FAIL_INSPECT_SERVICE") == service:
                        print("docker daemon unavailable", file=sys.stderr)
                        raise SystemExit(55)
                    service_path = service_state_path(service)
                    print(
                        service_path.read_text(encoding="utf-8")
                        if service_path.exists()
                        else "true"
                    )
                    raise SystemExit(0)
                if "-f" in args and "{{.Config.Image}}" in args:
                    print("redis:test")
                    raise SystemExit(0)
                if "-f" in args and "{{.Config.User}}" in args:
                    print("999:999")
                    raise SystemExit(0)
                print(os.environ["TEST_REDIS_HOST_DIR"])
                raise SystemExit(0)
            if args and args[0] == "run":
                bootstrap_state_path().write_text("true", encoding="utf-8")
                print("bootstrap-id")
                raise SystemExit(0)
            if args and args[0] == "rm":
                bootstrap_state_path().write_text("false", encoding="utf-8")
                raise SystemExit(0)
            if args and args[0] == "ps":
                if (
                    bootstrap_state_path().exists()
                    and bootstrap_state_path().read_text(encoding="utf-8")
                    == "true"
                ):
                    print("lumen-redis-restore-bootstrap-stale")
                raise SystemExit(0)
            if args and args[0] == "stop":
                if len(args) > 1 and args[1] == "lumen-redis":
                    counter_path = state / "redis-stop-count"
                    count = (
                        int(counter_path.read_text() or "0")
                        if counter_path.exists()
                        else 0
                    )
                    count += 1
                    counter_path.write_text(str(count), encoding="utf-8")
                    fail_number = int(
                        os.environ.get("TEST_FAIL_REDIS_STOP_NUMBER", "0")
                    )
                    if fail_number == count:
                        raise SystemExit(1)
                    set_service_state("redis", False)
                raise SystemExit(0)
            if args and args[0] == "start":
                if len(args) > 1 and args[1] == "lumen-redis":
                    set_service_state("redis", True)
                raise SystemExit(0)
            if not args or args[0] != "exec":
                raise SystemExit(0)

            index = 1
            while index < len(args) and args[index] in {"-i", "-e"}:
                if args[index] == "-e":
                    index += 2
                else:
                    index += 1
            container = args[index] if index < len(args) else ""
            index += 1
            command = args[index] if index < len(args) else ""
            rest = args[index + 1 :]

            if container.startswith("lumen-redis-restore-bootstrap-"):
                if command == "redis-cli":
                    redis_command = next(
                        (
                            item
                            for item in rest
                            if item
                            in {
                                "PING",
                                "DBSIZE",
                                "CONFIG",
                                "INFO",
                                "SHUTDOWN",
                            }
                        ),
                        "",
                    )
                    if redis_command == "PING":
                        print("PONG")
                    elif redis_command == "DBSIZE":
                        print("1")
                    elif redis_command == "CONFIG":
                        redis_host = Path(os.environ["TEST_REDIS_HOST_DIR"])
                        appendonly = redis_host / "appendonlydir"
                        appendonly.mkdir(exist_ok=True)
                        (appendonly / "appendonly.aof.1.base.rdb").write_bytes(
                            b"generated-base"
                        )
                        manifest = (
                            "file appendonly.aof.1.base.rdb seq 1 type b\\n"
                            "file appendonly.aof.1.incr.aof seq 1 type i\\n"
                        )
                        (appendonly / "appendonly.aof.manifest").write_text(
                            manifest,
                            encoding="utf-8",
                        )
                        if os.environ.get("TEST_AOF_VALID", "1") == "1":
                            (
                                appendonly / "appendonly.aof.1.incr.aof"
                            ).write_bytes(b"")
                        print("OK")
                    elif redis_command == "INFO":
                        print("aof_enabled:1")
                        print("aof_rewrite_in_progress:0")
                        print("aof_last_bgrewrite_status:ok")
                    elif redis_command == "SHUTDOWN":
                        bootstrap_state_path().write_text(
                            "false",
                            encoding="utf-8",
                        )
                    raise SystemExit(0)
                if command == "sh":
                    raise SystemExit(
                        0 if os.environ.get("TEST_AOF_VALID", "1") == "1" else 1
                    )
                raise SystemExit(0)

            if command == "pg_restore":
                sys.stdin.buffer.read()
                if "--list" not in rest and "-d" in rest:
                    target = rest[rest.index("-d") + 1]
                    db_path(target).write_text("restored\\n", encoding="utf-8")
                    if os.environ.get("TEST_MUTATE_PG_BACKUP_AFTER_RESTORE") == "1":
                        Path(os.environ["TEST_PG_BACKUP_PATH"]).write_bytes(
                            b"mutated-after-pg-restore"
                        )
                raise SystemExit(0)

            if command == "redis-cli":
                if "DBSIZE" in rest:
                    print("1")
                    raise SystemExit(0)
                counter_path = state / "redis-ping-count"
                count = int(counter_path.read_text() or "0") if counter_path.exists() else 0
                count += 1
                counter_path.write_text(str(count), encoding="utf-8")
                block_number = int(os.environ.get("TEST_BLOCK_REDIS_PING_NUMBER", "0"))
                if block_number == count:
                    block_here()
                print("PONG")
                raise SystemExit(0)
            if command == "redis-check-rdb":
                raise SystemExit(
                    0 if os.environ.get("TEST_RDB_VALID", "1") == "1" else 1
                )

            if command != "psql":
                raise SystemExit(0)

            sql = ""
            for option in ("-tAc", "-c"):
                if option in rest:
                    sql = rest[rest.index(option) + 1]
                    break

            match = re.search(r"datname = '([^']*)'", sql)
            if match:
                if db_path(match.group(1)).exists():
                    print("1")
                raise SystemExit(0)

            match = re.search(r'CREATE DATABASE "([^"]+)"', sql)
            if match:
                db_path(match.group(1)).write_text("empty\\n", encoding="utf-8")
                raise SystemExit(0)

            match = re.search(r'DROP DATABASE IF EXISTS "([^"]+)"', sql)
            if match:
                db_path(match.group(1)).unlink(missing_ok=True)
                raise SystemExit(0)

            match = re.search(
                r'ALTER DATABASE "([^"]+)" RENAME TO "([^"]+)"',
                sql,
            )
            if match:
                source, target = match.groups()
                db_path(source).rename(db_path(target))
                block_pattern = os.environ.get("TEST_BLOCK_AFTER_SQL_CONTAINS", "")
                block_fired = state / "sql-block-fired"
                if (
                    block_pattern
                    and block_pattern in sql
                    and not block_fired.exists()
                ):
                    block_fired.write_text("1", encoding="utf-8")
                    block_here()
                raise SystemExit(0)

            raise SystemExit(0)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_fake_systemctl(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -u
            printf '%s\\n' "$*" >> "${TEST_SYSTEMCTL_LOG:?}"
            if [ "${1:-}" != "is-active" ] || [ "$#" -ne 2 ]; then
                printf 'unexpected systemctl invocation: %s\\n' "$*" >&2
                exit 64
            fi
            case " ${TEST_ACTIVE_SYSTEMD_UNITS:-} " in
                *" $2 "*)
                    printf 'active\\n'
                    exit 0
                    ;;
            esac
            printf 'inactive\\n'
            exit 3
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
            printf '%s\\n' "$url" >> "${TEST_CURL_LOG:?}"
            if [[ "$url" == */readyz ]] \
                    && [[ "${TEST_API_READY:-1}" != "1" ]]; then
                if [[ "${TEST_READY_FAILURE_RESTORED_ONLY:-0}" != "1" ]] \
                        || grep -Fxq 'restored' \
                            "${TEST_STATE_DIR:?}/dbs/lumen" 2>/dev/null; then
                    exit 22
                fi
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_command_wrappers(fakebin: Path) -> None:
    (fakebin / "mv").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import sys
            import time

            source = Path(sys.argv[-2])
            destination = Path(sys.argv[-1])
            item = source.name
            if (
                os.environ.get("TEST_FAIL_ROLLBACK_ITEM", "") == item
                and ".lumen-restore-old." in str(source)
            ):
                raise SystemExit(91)
            target = destination / item if destination.is_dir() else destination
            os.replace(source, target)
            if (
                os.environ.get("TEST_BLOCK_AFTER_STASH_ITEM", "") == item
                and ".lumen-restore-old." in str(destination)
            ):
                Path(os.environ["TEST_PHASE_MARKER"]).write_text(
                    "ready\\n",
                    encoding="utf-8",
                )
                time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    (fakebin / "mv").chmod(0o755)

    (fakebin / "cp").write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import os
            from pathlib import Path
            import shutil
            import sys
            import time

            source = Path(sys.argv[-2])
            destination = Path(sys.argv[-1])
            item = source.name
            target = destination / item if destination.is_dir() else destination
            shutil.copy2(source, target)
            if (
                os.environ.get("TEST_BLOCK_AFTER_COPY_ITEM", "") == item
                and str(destination).startswith(os.environ["TEST_REDIS_HOST_DIR"])
            ):
                Path(os.environ["TEST_PHASE_MARKER"]).write_text(
                    "ready\\n",
                    encoding="utf-8",
                )
                time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    (fakebin / "cp").chmod(0o755)


def _prepare_restore(
    tmp_path: Path,
    *,
    initial_active: bool = True,
    block_after_sql: str = "",
    block_ping_number: int = 0,
    block_stash_item: str = "",
    block_copy_item: str = "",
    fail_rollback_item: str = "",
    fail_redis_stop_number: int = 0,
    inspect_failure_service: str = "",
    pair_marker_mode: str = "valid",
    mutate_pg_backup_after_restore: bool = False,
    api_ready: bool = True,
    worker_ready: bool = True,
    rdb_valid: bool = True,
    aof_valid: bool = True,
    failpoint: str = "",
    env_out: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Path, Path, Path, Path]:
    backup_root = tmp_path / "backup"
    pg_backup = backup_root / "pg"
    redis_backup = backup_root / "redis"
    pg_backup.mkdir(parents=True)
    redis_backup.mkdir()
    with gzip.open(pg_backup / f"{TS}.pg.dump.gz", "wb") as fh:
        fh.write(b"fake postgres archive")

    redis_source = tmp_path / "redis-source"
    redis_source.mkdir()
    (redis_source / "dump.rdb").write_bytes(b"new-dump")
    (redis_source / "appendonly.aof").write_bytes(b"new-aof")
    (redis_source / "appendonlydir").mkdir()
    (redis_source / "appendonlydir" / "part.aof").write_bytes(b"new-part")
    with tarfile.open(redis_backup / f"{TS}.redis.tgz", "w:gz") as archive:
        archive.add(redis_source / "dump.rdb", arcname="dump.rdb")
        archive.add(redis_source / "appendonly.aof", arcname="appendonly.aof")
        archive.add(redis_source / "appendonlydir", arcname="appendonlydir")
    pg_path = pg_backup / f"{TS}.pg.dump.gz"
    redis_path = redis_backup / f"{TS}.redis.tgz"
    pair_marker = backup_root / f".backup-pair.{TS}.json"
    pair_document = {
        "schema": 1,
        "operation_id": f"backup-{TS}-test",
        "timestamp": TS,
        "pg": {
            "name": pg_path.name,
            "size": pg_path.stat().st_size,
            "sha256": hashlib.sha256(pg_path.read_bytes()).hexdigest(),
        },
        "redis": {
            "name": redis_path.name,
            "size": redis_path.stat().st_size,
            "sha256": hashlib.sha256(redis_path.read_bytes()).hexdigest(),
        },
    }
    if pair_marker_mode == "hash_mismatch":
        pair_document["redis"]["sha256"] = "0" * 64
    elif pair_marker_mode not in {"valid", "missing"}:
        raise ValueError(f"unsupported pair marker mode: {pair_marker_mode}")
    if pair_marker_mode != "missing":
        pair_marker.write_text(
            json.dumps(pair_document) + "\n",
            encoding="utf-8",
        )

    redis_host = tmp_path / "redis-host"
    redis_host.mkdir()
    (redis_host / "dump.rdb").write_bytes(b"old-dump")
    (redis_host / "appendonly.aof").write_bytes(b"old-aof")
    (redis_host / "appendonlydir").mkdir()
    (redis_host / "appendonlydir" / "part.aof").write_bytes(b"old-part")

    state = tmp_path / "state"
    db_dir = state / "dbs"
    db_dir.mkdir(parents=True)
    if initial_active:
        (db_dir / "lumen").write_text("old-active\n", encoding="utf-8")
    for service in APPLICATION_SERVICES:
        (state / f"service-{service}").write_text("true", encoding="utf-8")

    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_fake_docker(fakebin / "docker")
    _write_fake_systemctl(fakebin / "systemctl")
    _write_curl_wrapper(fakebin / "curl")
    _write_command_wrappers(fakebin)

    marker = tmp_path / "phase.ready"
    docker_log = tmp_path / "docker.log"
    systemctl_log = tmp_path / "systemctl.log"
    temp_dir = tmp_path / "tmp"
    maint_root = tmp_path / "maint"
    deploy_root = maint_root
    temp_dir.mkdir()
    maint_root.mkdir()

    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "BACKUP_ROOT": str(backup_root),
            "TMPDIR": str(temp_dir),
            "LUMEN_MAINT_ROOT": str(maint_root),
            "LUMEN_DEPLOY_ROOT": str(deploy_root),
            "LUMEN_BACKUP_RESTORE_LOCKFILE": str(tmp_path / "backup.lock"),
            "LUMEN_RESTORE_STATE_DIR": str(tmp_path / "restore-state"),
            "DB_USER": "lumen",
            "DB_NAME": "lumen",
            "TEST_STATE_DIR": str(state),
            "TEST_DOCKER_LOG": str(docker_log),
            "TEST_SYSTEMCTL_LOG": str(systemctl_log),
            "TEST_ACTIVE_SYSTEMD_UNITS": "",
            "TEST_CURL_LOG": str(tmp_path / "curl.log"),
            "TEST_REDIS_HOST_DIR": str(redis_host),
            "TEST_PHASE_MARKER": str(marker),
            "TEST_BLOCK_AFTER_SQL_CONTAINS": block_after_sql,
            "TEST_BLOCK_REDIS_PING_NUMBER": str(block_ping_number),
            "TEST_BLOCK_AFTER_STASH_ITEM": block_stash_item,
            "TEST_BLOCK_AFTER_COPY_ITEM": block_copy_item,
            "TEST_FAIL_ROLLBACK_ITEM": fail_rollback_item,
            "TEST_FAIL_REDIS_STOP_NUMBER": str(fail_redis_stop_number),
            "TEST_FAIL_INSPECT_SERVICE": inspect_failure_service,
            "TEST_MUTATE_PG_BACKUP_AFTER_RESTORE": (
                "1" if mutate_pg_backup_after_restore else "0"
            ),
            "TEST_API_READY": "1" if api_ready else "0",
            "TEST_WORKER_READY": "1" if worker_ready else "0",
            "TEST_READY_FAILURE_RESTORED_ONLY": "1",
            "TEST_RDB_VALID": "1" if rdb_valid else "0",
            "TEST_AOF_VALID": "1" if aof_valid else "0",
            "TEST_PG_BACKUP_PATH": str(pg_path),
            "LUMEN_RESTORE_FAILPOINT": failpoint,
            "LUMEN_SYSTEMD_RUNTIME_AVAILABLE": "1",
            "LUMEN_CORE_READINESS_ATTEMPTS": "1",
            "LUMEN_CORE_READINESS_INTERVAL_SECONDS": "0",
            "LUMEN_SERVICE_STATE_INTERVAL_SECONDS": "0",
            "LUMEN_SERVICE_QUIESCE_ATTEMPTS": "4",
            "LUMEN_SERVICE_START_ATTEMPTS": "4",
            "LUMEN_API_IMAGE_REF": (
                f"example.invalid/lumen-api@sha256:{'1' * 64}"
            ),
            "LUMEN_WORKER_IMAGE_REF": (
                f"example.invalid/lumen-worker@sha256:{'2' * 64}"
            ),
            "LUMEN_WEB_IMAGE_REF": (
                f"example.invalid/lumen-web@sha256:{'3' * 64}"
            ),
            "LUMEN_TGBOT_IMAGE_REF": (
                f"example.invalid/lumen-tgbot@sha256:{'4' * 64}"
            ),
        }
    )
    if env_out is not None:
        env_out.update(env)
    shell = """
command() {
    if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
        return 1
    fi
    builtin command "$@"
}
. "$1" "$2"
"""
    process = subprocess.Popen(
        ["/bin/bash", "-c", shell, "restore-signal-test", str(RESTORE), TS],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    return process, marker, redis_host, db_dir, docker_log


def _assert_systemd_writer_units_checked(tmp_path: Path) -> None:
    calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()
    for unit in SYSTEMD_WRITER_UNITS:
        assert f"is-active {unit}" in calls


def _recover_before_missing_restore(
    tmp_path: Path,
    env: dict[str, str],
    marker: Path,
    *,
    failpoint: str = "",
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    marker.unlink(missing_ok=True)
    recovery_maint = Path(tempfile.mkdtemp(prefix="maint-recovery-", dir=tmp_path))
    recovery_env = env.copy()
    recovery_env.update(
        {
            "LUMEN_MAINT_ROOT": str(recovery_maint),
            "LUMEN_DEPLOY_ROOT": str(recovery_maint),
            "LUMEN_BACKUP_RESTORE_LOCKFILE": str(
                recovery_maint / "backup-recovery.lock"
            ),
            "TEST_BLOCK_AFTER_SQL_CONTAINS": "",
            "TEST_BLOCK_REDIS_PING_NUMBER": "0",
            "TEST_BLOCK_AFTER_STASH_ITEM": "",
            "TEST_BLOCK_AFTER_COPY_ITEM": "",
            "LUMEN_RESTORE_FAILPOINT": failpoint,
        }
    )
    if overrides:
        recovery_env.update(overrides)
    shell = """
command() {
    if [ "$1" = "-v" ] && [ "${2:-}" = "flock" ]; then
        return 1
    fi
    builtin command "$@"
}
. "$1" "$2"
"""
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            shell,
            "restore-crash-recovery-test",
            str(RESTORE),
            "--recover-only",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=recovery_env,
        timeout=20,
        check=False,
    )


def _interrupt(
    process: subprocess.Popen[str],
    marker: Path,
    sig: signal.Signals,
) -> tuple[int, str]:
    _wait_for_file(marker)
    os.killpg(process.pid, sig)
    stdout, stderr = process.communicate(timeout=15)
    return process.returncode, stdout + stderr


def _assert_old_redis(redis_host: Path) -> None:
    assert (redis_host / "dump.rdb").read_bytes() == b"old-dump"
    assert (redis_host / "appendonly.aof").read_bytes() == b"old-aof"
    assert (redis_host / "appendonlydir" / "part.aof").read_bytes() == b"old-part"
    assert not list(redis_host.glob(".lumen-restore-old.*"))


def _assert_new_redis(redis_host: Path) -> None:
    assert (redis_host / "dump.rdb").read_bytes() == b"new-dump"
    assert not (redis_host / "appendonly.aof").exists()
    appendonly = redis_host / "appendonlydir"
    assert (
        (appendonly / "appendonly.aof.manifest")
        .read_text(encoding="utf-8")
        .startswith("file appendonly.aof.1.base.rdb")
    )
    assert (appendonly / "appendonly.aof.1.base.rdb").read_bytes() == (
        b"generated-base"
    )
    assert (appendonly / "appendonly.aof.1.incr.aof").is_file()


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path).encode())
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            snapshot[relative] = ("dir", b"")
    return snapshot


def test_writer_quiesce_rechecks_and_stops_transient_restart(
    tmp_path: Path,
) -> None:
    helper = ROOT / "scripts/lib/backup_restore_services.sh"
    stop_log = tmp_path / "stop.log"
    api_calls = tmp_path / "api.calls"
    systemctl_log = tmp_path / "systemctl.log"
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _write_fake_systemctl(fakebin / "systemctl")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}{os.pathsep}{env['PATH']}",
            "TEST_SYSTEMCTL_LOG": str(systemctl_log),
            "TEST_ACTIVE_SYSTEMD_UNITS": "",
            "LUMEN_SYSTEMD_RUNTIME_AVAILABLE": "1",
        }
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"""
            set -euo pipefail
            . {shlex.quote(str(helper))}
            lumen_stop_services() {{
                printf '%s\\n' "$*" >> {shlex.quote(str(stop_log))}
            }}
            lumen_service_running_state() {{
                if [ "$1" = api ]; then
                    count=0
                    [ ! -f {shlex.quote(str(api_calls))} ] \
                        || count="$(cat {shlex.quote(str(api_calls))})"
                    count=$((count + 1))
                    printf '%s\\n' "$count" > {shlex.quote(str(api_calls))}
                    if [ "$count" -eq 2 ]; then
                        printf 'running\\n'
                        return 0
                    fi
                fi
                printf 'stopped\\n'
            }}
            LUMEN_SERVICE_STATE_INTERVAL_SECONDS=0
            LUMEN_SERVICE_QUIESCE_ATTEMPTS=5
            LUMEN_SERVICE_QUIESCE_STABLE_POLLS=2
            lumen_quiesce_all_writer_services
            """,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert stop_log.read_text(encoding="utf-8").splitlines() == [
        "api worker tgbot",
        "api worker tgbot",
    ]
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        *(f"is-active {unit}" for unit in SYSTEMD_WRITER_UNITS),
        *(f"is-active {unit}" for unit in SYSTEMD_WRITER_UNITS),
    ]


def test_restore_aborts_before_stopping_services_when_state_snapshot_fails(
    tmp_path: Path,
) -> None:
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        inspect_failure_service="worker",
    )
    before = _tree_snapshot(redis_host)

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 70, output
    assert "failed to capture the pre-restore writer state" in output
    assert _tree_snapshot(redis_host) == before
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    calls = docker_log.read_text(encoding="utf-8")
    assert " compose " not in f" {calls} "
    assert "stop lumen-redis" not in calls


@pytest.mark.parametrize("pair_marker_mode", ["missing", "hash_mismatch"])
def test_restore_rejects_unbound_backup_pair_before_archive_or_service_use(
    tmp_path: Path,
    pair_marker_mode: str,
) -> None:
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        pair_marker_mode=pair_marker_mode,
    )
    before = _tree_snapshot(redis_host)

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 3, output
    assert "not a committed, verifiable backup pair" in output
    assert _tree_snapshot(redis_host) == before
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert not docker_log.exists() or not docker_log.read_text(encoding="utf-8")


def test_restore_revalidates_bound_pair_after_staging_before_stopping_services(
    tmp_path: Path,
) -> None:
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        mutate_pg_backup_after_restore=True,
    )
    before = _tree_snapshot(redis_host)

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 3, output
    assert "changed while restore archives were being validated" in output
    assert _tree_snapshot(redis_host) == before
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert not list(db_dir.glob("lumen_restore_*"))
    calls = docker_log.read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot stop" not in calls
    assert "stop lumen-redis" not in calls


def test_old_legal_archive_restores_only_verified_rdb_and_clears_journal(
    tmp_path: Path,
) -> None:
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(tmp_path)

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 0, output
    _assert_new_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    assert not list(redis_host.glob(".lumen-restore-old.*"))
    assert not list(db_dir.glob("lumen_rollback_*"))
    assert not (tmp_path / "restore-state" / "active.json").exists()
    calls = docker_log.read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot start api worker tgbot web" in calls
    assert "exec -T worker python -m app.worker_health check" in calls
    for service in APPLICATION_SERVICES:
        assert (
            tmp_path / "state" / f"service-{service}"
        ).read_text(encoding="utf-8") == "true"
    _assert_systemd_writer_units_checked(tmp_path)


def test_generated_aof_missing_segment_is_rejected_before_redis_restart(
    tmp_path: Path,
) -> None:
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        aof_valid=False,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 5, output
    assert "generated Redis AOF manifest or segment validation failed" in output
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    calls = docker_log.read_text(encoding="utf-8")
    assert "start lumen-redis" in calls
    assert "compose --ansi=never --profile tgbot start api worker tgbot web" in calls


@pytest.mark.parametrize(
    ("api_ready", "worker_ready", "expected_error"),
    [
        (False, True, "API /readyz 未通过"),
        (True, False, "Worker python -m app.worker_health check 未通过"),
    ],
)
def test_restore_readiness_failure_keeps_committed_pair_and_stops_services(
    tmp_path: Path,
    api_ready: bool,
    worker_ready: bool,
    expected_error: str,
) -> None:
    process, _marker, redis_host, db_dir, _docker_log = _prepare_restore(
        tmp_path,
        api_ready=api_ready,
        worker_ready=worker_ready,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 70, output
    assert expected_error in output
    _assert_new_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    assert list(db_dir.glob("lumen_rollback_*"))
    journal = tmp_path / "restore-state" / "active.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "committed"
    for service in ("api", "worker", "tgbot", "web"):
        assert (tmp_path / "state" / f"service-{service}").read_text() == "false"
    probed = (tmp_path / "curl.log").read_text(encoding="utf-8").splitlines()
    assert probed
    assert set(probed) == {"http://127.0.0.1:8000/readyz"}


def test_readiness_failure_does_not_attempt_post_commit_data_rollback(
    tmp_path: Path,
) -> None:
    process, _marker, redis_host, db_dir, _docker_log = _prepare_restore(
        tmp_path,
        api_ready=False,
        fail_rollback_item="dump.rdb",
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 70, output
    assert "restore recovery incomplete; refusing to restart writers" in output
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    assert list(redis_host.glob(".lumen-restore-old.*"))
    _assert_new_redis(redis_host)
    journal = tmp_path / "restore-state" / "active.json"
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "committed"
    for service in ("api", "worker", "tgbot", "web"):
        assert (tmp_path / "state" / f"service-{service}").read_text() == "false"


def test_sigkill_after_pg_rollback_resumes_with_redis_rollback(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, _marker, redis_host, db_dir, _docker_log = _prepare_restore(
        tmp_path,
        failpoint="before_storage_commit",
        env_out=env,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr
    journal = tmp_path / "restore-state" / "active.json"

    assert process.returncode == -signal.SIGKILL, output
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "pg_promoted"
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    _assert_new_redis(redis_host)

    interrupted_recovery = _recover_before_missing_restore(
        tmp_path,
        env,
        tmp_path / "phase.ready",
        failpoint="after_pg_rollback",
    )

    assert interrupted_recovery.returncode == -signal.SIGKILL
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    _assert_new_redis(redis_host)
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == (
        "pg_rolled_back"
    )

    recovered = _recover_before_missing_restore(
        tmp_path,
        env,
        tmp_path / "phase.ready",
    )

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    _assert_old_redis(redis_host)
    assert not journal.exists()


@pytest.mark.parametrize(
    ("sig", "expected_rc"),
    [
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ],
)
def test_signal_after_redis_pong_rolls_back_pair_and_restarts_services(
    tmp_path: Path,
    sig: signal.Signals,
    expected_rc: int,
) -> None:
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_ping_number=2,
    )

    returncode, output = _interrupt(process, marker, sig)

    assert returncode == expected_rc, output
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert not list(db_dir.glob("lumen_restore_*"))
    calls = docker_log.read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot start api worker tgbot web" in calls


def test_signal_during_partial_redis_stash_restores_every_original_item(
    tmp_path: Path,
) -> None:
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_stash_item="dump.rdb",
    )
    _wait_for_file(marker)
    maintenance_lock = tmp_path / "maint" / ".lumen-maintenance.lock.d"
    pair_lock = tmp_path / "backup.lock.d"
    assert maintenance_lock.is_dir()
    assert pair_lock.is_dir()

    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=15)
    returncode = process.returncode
    output = stdout + stderr

    assert returncode == 143, output
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert (
        "compose --ansi=never --profile tgbot start api worker tgbot web"
        in docker_log.read_text(encoding="utf-8")
    )
    assert not maintenance_lock.exists()
    assert not pair_lock.exists()


def test_signal_during_redis_copy_removes_partial_new_data_before_restart(
    tmp_path: Path,
) -> None:
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_copy_item="dump.rdb",
    )

    returncode, output = _interrupt(process, marker, signal.SIGTERM)

    assert returncode == 143, output
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert (
        "compose --ansi=never --profile tgbot start api worker tgbot web"
        in docker_log.read_text(encoding="utf-8")
    )


def test_sigkill_during_redis_copy_is_recovered_before_next_restore(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_copy_item="dump.rdb",
        env_out=env,
    )
    _wait_for_file(marker)

    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=15)

    journal = tmp_path / "restore-state" / "active.json"
    assert process.returncode == -signal.SIGKILL
    assert journal.is_file()

    recovered = _recover_before_missing_restore(tmp_path, env, marker)

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert not list(db_dir.glob("lumen_restore_*"))
    assert not list(db_dir.glob("lumen_rollback_*"))
    assert not journal.exists()
    calls = docker_log.read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot start api worker tgbot" in calls


def test_signal_after_first_pg_rename_rolls_back_postgres_and_redis(
    tmp_path: Path,
) -> None:
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_after_sql='ALTER DATABASE "lumen" RENAME TO "lumen_rollback_',
    )

    returncode, output = _interrupt(process, marker, signal.SIGTERM)

    assert returncode == 143, output
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert not list(db_dir.glob("lumen_restore_*"))
    assert not list(db_dir.glob("lumen_rollback_*"))
    assert (
        "compose --ansi=never --profile tgbot start api worker tgbot web"
        in docker_log.read_text(encoding="utf-8")
    )


def test_sigkill_after_pg_promotion_before_readiness_recovers_old_pair(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_after_sql='RENAME TO "lumen";',
        env_out=env,
    )
    _wait_for_file(marker)

    os.killpg(process.pid, signal.SIGKILL)
    process.communicate(timeout=15)

    journal = tmp_path / "restore-state" / "active.json"
    assert process.returncode == -signal.SIGKILL
    assert journal.is_file()

    recovered = _recover_before_missing_restore(tmp_path, env, marker)

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    _assert_old_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert not list(db_dir.glob("lumen_restore_*"))
    assert not list(db_dir.glob("lumen_rollback_*"))
    assert not journal.exists()
    calls = docker_log.read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot start api worker tgbot" in calls


@pytest.mark.parametrize("initial_active", [True, False])
def test_signal_after_pg_promotion_before_readiness_rolls_back_pair(
    tmp_path: Path,
    initial_active: bool,
) -> None:
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        initial_active=initial_active,
        block_after_sql='RENAME TO "lumen";',
    )

    returncode, output = _interrupt(process, marker, signal.SIGTERM)

    assert returncode == 143, output
    _assert_old_redis(redis_host)
    assert not list(db_dir.glob("lumen_restore_*"))
    assert not list(db_dir.glob("lumen_rollback_*"))
    if initial_active:
        assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    else:
        assert not (db_dir / "lumen").exists()
    assert (
        "compose --ansi=never --profile tgbot start api worker tgbot web"
        in docker_log.read_text(encoding="utf-8")
    )


def test_sigkill_after_readiness_commit_keeps_new_pair_and_cleans_rollbacks(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        failpoint="after_readiness_commit",
        env_out=env,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr
    journal = tmp_path / "restore-state" / "active.json"

    assert process.returncode == -signal.SIGKILL, output
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "committed"
    assert list(db_dir.glob("lumen_rollback_*"))
    assert list(redis_host.glob(".lumen-restore-old.*"))

    recovered = _recover_before_missing_restore(
        tmp_path,
        env,
        tmp_path / "phase.ready",
    )

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    _assert_new_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    assert not list(db_dir.glob("lumen_rollback_*"))
    assert not list(redis_host.glob(".lumen-restore-old.*"))
    assert not journal.exists()
    calls = docker_log.read_text(encoding="utf-8")
    assert (
        calls.count("compose --ansi=never --profile tgbot start api worker tgbot web")
        >= 2
    )


def test_sigkill_after_storage_commit_happens_before_any_writer_restart(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, _marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        failpoint="after_storage_commit",
        env_out=env,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr
    journal = tmp_path / "restore-state" / "active.json"

    assert process.returncode == -signal.SIGKILL, output
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "committed"
    _assert_new_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    calls = docker_log.read_text(encoding="utf-8")
    assert (
        "compose --ansi=never --profile tgbot start api worker tgbot web" not in calls
    )

    recovered = _recover_before_missing_restore(
        tmp_path,
        env,
        tmp_path / "phase.ready",
    )

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    _assert_new_redis(redis_host)
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "restored\n"
    assert not journal.exists()


def test_storage_commit_recovery_retains_rollbacks_until_readiness_passes(
    tmp_path: Path,
) -> None:
    env: dict[str, str] = {}
    process, _marker, redis_host, db_dir, _docker_log = _prepare_restore(
        tmp_path,
        failpoint="after_storage_commit",
        env_out=env,
    )

    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr
    journal = tmp_path / "restore-state" / "active.json"

    assert process.returncode == -signal.SIGKILL, output
    assert list(db_dir.glob("lumen_rollback_*"))
    assert list(redis_host.glob(".lumen-restore-old.*"))

    failed_recovery = _recover_before_missing_restore(
        tmp_path,
        env,
        tmp_path / "phase.ready",
        overrides={"TEST_API_READY": "0"},
    )

    assert failed_recovery.returncode != 0
    assert list(db_dir.glob("lumen_rollback_*"))
    assert list(redis_host.glob(".lumen-restore-old.*"))
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "committed"

    recovered = _recover_before_missing_restore(
        tmp_path,
        env,
        tmp_path / "phase.ready",
        overrides={"TEST_API_READY": "1"},
    )

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert not list(db_dir.glob("lumen_rollback_*"))
    assert not list(redis_host.glob(".lumen-restore-old.*"))
    assert not journal.exists()


def test_failed_redis_rollback_refuses_application_restart(
    tmp_path: Path,
) -> None:
    process, marker, redis_host, db_dir, docker_log = _prepare_restore(
        tmp_path,
        block_ping_number=2,
        fail_rollback_item="dump.rdb",
    )

    returncode, output = _interrupt(process, marker, signal.SIGTERM)

    assert returncode == 70, output
    assert "restore recovery incomplete; refusing to restart writers" in output
    assert (db_dir / "lumen").read_text(encoding="utf-8") == "old-active\n"
    assert list(redis_host.glob(".lumen-restore-old.*"))
    calls = docker_log.read_text(encoding="utf-8")
    assert "compose --ansi=never --profile tgbot start api worker tgbot" not in calls


@pytest.mark.parametrize(
    "block_kwargs",
    [
        {"block_stash_item": "dump.rdb"},
        {"block_copy_item": "dump.rdb"},
        {"block_ping_number": 2},
    ],
)
def test_redis_stop_failure_does_not_mutate_data_or_restart_applications(
    tmp_path: Path,
    block_kwargs: dict[str, object],
) -> None:
    process, marker, redis_host, _db_dir, docker_log = _prepare_restore(
        tmp_path,
        fail_redis_stop_number=2,
        **block_kwargs,
    )
    _wait_for_file(marker)
    before = _tree_snapshot(redis_host)

    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=15)
    output = stdout + stderr

    assert process.returncode == 70, output
    assert _tree_snapshot(redis_host) == before
    assert "refusing to restart writers" in output
    assert (
        "compose --ansi=never --profile tgbot start api worker tgbot"
        not in docker_log.read_text(encoding="utf-8")
    )


def test_postgres_commit_flag_precedes_recovery_guard_clear() -> None:
    text = RESTORE.read_text(encoding="utf-8")
    no_active = text.index('log "WARN: active postgres database $PG_DB does not exist;')
    no_active_promoted = text.index("PG_PROMOTED=1", no_active)
    no_active_guard_clear = text.index("PG_SWAP_IN_PROGRESS=0", no_active_promoted)
    active_swap = text.index('if ! pg_rename_database "$PG_TEMP_DB" "$PG_DB"; then')
    active_promoted = text.index("PG_PROMOTED=1", active_swap)
    active_guard_clear = text.index("PG_SWAP_IN_PROGRESS=0", active_promoted)

    assert no_active_promoted < no_active_guard_clear
    assert active_promoted < active_guard_clear


def test_restore_enables_signal_handlers_only_after_both_locks_are_recorded() -> None:
    text = RESTORE.read_text(encoding="utf-8")
    runtime = text.index(
        "trap cleanup EXIT", text.index("redis_rollback_after_pg_failure")
    )
    ignored = text.index("trap '' INT TERM HUP", runtime)
    maintenance = text.index('lumen_acquire_lock "${LUMEN_DEPLOY_ROOT}"', ignored)
    cleanup_restored = text.index("trap cleanup EXIT", maintenance)
    pair_lock = text.index("\nacquire_lock\n", cleanup_restored)
    signal_enabled = text.index("trap 'on_signal INT' INT", pair_lock)

    assert runtime < ignored < maintenance < cleanup_restored < pair_lock
    assert pair_lock < signal_enabled
