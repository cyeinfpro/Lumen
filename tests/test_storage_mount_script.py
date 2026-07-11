from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_storage_apply(
    tmp_path: Path,
    *,
    stop_fails: bool = False,
    mount_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    mockbin = tmp_path / "bin"
    state_dir = tmp_path / "state"
    target = tmp_path / "target"
    local_root = tmp_path / "local"
    compose_dir = tmp_path / "compose"
    docker_log = tmp_path / "docker.log"
    mount_log = tmp_path / "mount.log"
    deploy_env = tmp_path / "deploy.env"
    mockbin.mkdir()
    state_dir.mkdir()
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (state_dir / "storage.conf").write_text(
        f"MODE=local\nLOCAL_ROOT={local_root}\n",
        encoding="utf-8",
    )
    (state_dir / "apply.trigger").write_text("a" * 32 + "\n", encoding="ascii")
    deploy_env.write_text(f"LUMEN_DATA_ROOT={target}\n", encoding="utf-8")

    _write_executable(mockbin / "mountpoint", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(mockbin / "findmnt", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(mockbin / "flock", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        mockbin / "timeout",
        "#!/usr/bin/env bash\nshift\nexec \"$@\"\n",
    )
    _write_executable(
        mockbin / "mount",
        (
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(mount_log))}\n"
            f"exit {32 if mount_fails else 0}\n"
        ),
    )
    _write_executable(
        mockbin / "docker",
        f"""#!/usr/bin/env bash
printf '%s\n' "$*" >> {shlex.quote(str(docker_log))}
case " $* " in
  *" compose stop "*)
    [ "${{TEST_STOP_FAIL:-0}}" = "1" ] && exit 42
    ;;
  *" compose ps --status running --quiet postgres redis "*)
    exit 0
    ;;
esac
exit 0
""",
    )

    script = Path("deploy/scripts/lumen_storage_mount.sh").resolve()
    env = {
        **os.environ,
        "PATH": f"{mockbin}{os.pathsep}{os.environ['PATH']}",
        "LUMEN_STORAGE_STATE_DIR": str(state_dir),
        "LUMEN_STORAGE_TARGET": str(target),
        "LUMEN_STORAGE_DEFAULT_LOCAL_ROOT": str(local_root),
        "LUMEN_DOCKER_COMPOSE_DIR": str(compose_dir),
        "LUMEN_DEPLOY_ENV_FILE": str(deploy_env),
        "TEST_STOP_FAIL": "1" if stop_fails else "0",
    }
    env.pop("LUMEN_DB_ROOT", None)
    result = subprocess.run(
        ["bash", str(script), "apply"],
        cwd=script.parent.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, state_dir, docker_log, mount_log


def test_storage_mount_up_returns_mount_failure(tmp_path: Path) -> None:
    mockbin = tmp_path / "bin"
    state_dir = tmp_path / "state"
    local_root = tmp_path / "local"
    target = tmp_path / "target"
    mockbin.mkdir()
    state_dir.mkdir()

    _write_executable(mockbin / "mountpoint", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(mockbin / "findmnt", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        mockbin / "mount",
        "#!/usr/bin/env bash\nprintf 'mock mount failed\\n' >&2\nexit 32\n",
    )

    (state_dir / "storage.conf").write_text(
        f"MODE=local\nLOCAL_ROOT={local_root}\n",
        encoding="utf-8",
    )
    script = Path("deploy/scripts/lumen_storage_mount.sh").resolve()
    env = {
        **os.environ,
        "PATH": f"{mockbin}{os.pathsep}{os.environ['PATH']}",
        "LUMEN_STORAGE_STATE_DIR": str(state_dir),
        "LUMEN_STORAGE_TARGET": str(target),
        "LUMEN_STORAGE_DEFAULT_LOCAL_ROOT": str(local_root),
    }

    result = subprocess.run(
        ["bash", str(script), "up"],
        cwd=script.parent.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 32
    assert "mock mount failed" in result.stderr
    assert (state_dir / "status.json").is_file()


def test_storage_mount_rejects_unsafe_local_root_before_mount(tmp_path: Path) -> None:
    mockbin = tmp_path / "bin"
    state_dir = tmp_path / "state"
    target = tmp_path / "target"
    called = tmp_path / "mount-called"
    mockbin.mkdir()
    state_dir.mkdir()

    _write_executable(mockbin / "mountpoint", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(mockbin / "findmnt", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(
        mockbin / "mount",
        f"#!/usr/bin/env bash\ntouch {called}\nexit 0\n",
    )
    (state_dir / "storage.conf").write_text(
        "MODE=local\nLOCAL_ROOT=/etc\n",
        encoding="utf-8",
    )
    script = Path("deploy/scripts/lumen_storage_mount.sh").resolve()
    env = {
        **os.environ,
        "PATH": f"{mockbin}{os.pathsep}{os.environ['PATH']}",
        "LUMEN_STORAGE_STATE_DIR": str(state_dir),
        "LUMEN_STORAGE_TARGET": str(target),
        "LUMEN_STORAGE_DEFAULT_LOCAL_ROOT": str(tmp_path / "fallback"),
    }

    result = subprocess.run(
        ["bash", str(script), "up"],
        cwd=script.parent.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "refusing unsafe local root: /etc" in result.stderr
    assert not called.exists()


def test_storage_apply_default_db_root_stops_and_restarts_postgres_redis(
    tmp_path: Path,
) -> None:
    result, state_dir, docker_log, mount_log = _run_storage_apply(tmp_path)

    assert result.returncode == 0, result.stderr
    log = docker_log.read_text(encoding="utf-8")
    assert "compose stop -t 30 api worker tgbot web postgres redis" in log
    assert "compose ps --status running --quiet postgres redis" in log
    assert "compose start postgres redis" in log
    assert "compose start api worker tgbot web" in log
    assert mount_log.is_file()
    apply_result = json.loads(
        (state_dir / "last-apply.json").read_text(encoding="utf-8")
    )
    assert apply_result["status"] == "ok"


def test_storage_apply_refuses_remount_when_database_stop_fails(
    tmp_path: Path,
) -> None:
    result, state_dir, docker_log, mount_log = _run_storage_apply(
        tmp_path,
        stop_fails=True,
    )

    assert result.returncode == 1
    assert "refusing remount" in result.stderr
    assert "postgres redis" in docker_log.read_text(encoding="utf-8")
    assert not mount_log.exists()
    apply_result = json.loads(
        (state_dir / "last-apply.json").read_text(encoding="utf-8")
    )
    assert apply_result["status"] == "fail"
    assert "did not stop cleanly" in apply_result["message"]


def test_storage_apply_does_not_fallback_to_empty_root_after_db_mount_failure(
    tmp_path: Path,
) -> None:
    result, state_dir, docker_log, mount_log = _run_storage_apply(
        tmp_path,
        mount_fails=True,
    )

    assert result.returncode == 1
    assert mount_log.is_file()
    assert "keeping postgres/redis stopped" in result.stderr
    log = docker_log.read_text(encoding="utf-8")
    assert "compose start postgres redis" not in log
    apply_result = json.loads(
        (state_dir / "last-apply.json").read_text(encoding="utf-8")
    )
    assert apply_result["status"] == "fail"
    assert "avoid data split" in apply_result["message"]
