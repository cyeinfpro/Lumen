from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "lumen_storage_mount.sh"
APP_SERVICES = ("api", "worker", "tgbot", "web")
ALL_SERVICES = (*APP_SERVICES, "postgres", "redis")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class StorageHarness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        mode: str = "local",
        initial_mounted: bool = True,
        running_services: tuple[str, ...] = ALL_SERVICES,
        db_moves_with_target: bool = True,
    ) -> None:
        self.tmp_path = tmp_path
        self.mockbin = tmp_path / "bin"
        self.state_dir = tmp_path / "storage-state"
        self.mock_state = tmp_path / "mock-state"
        self.proc_root = tmp_path / "proc"
        self.target = tmp_path / "target"
        self.local_root = tmp_path / "local"
        self.compose_dir = tmp_path / "compose"
        self.db_root = self.target if db_moves_with_target else tmp_path / "database"
        for path in (
            self.mockbin,
            self.state_dir,
            self.mock_state,
            self.proc_root,
            self.target,
            self.local_root,
            self.compose_dir,
        ):
            path.mkdir()
        (self.compose_dir / "docker-compose.yml").write_text(
            "services: {}\n",
            encoding="utf-8",
        )
        (self.state_dir / "apply.trigger").write_text(
            "a" * 32 + "\n",
            encoding="ascii",
        )
        self.write_config(mode)
        self.set_running_services(running_services)
        (self.mock_state / "containers").write_text("", encoding="utf-8")
        if initial_mounted:
            self.set_mount_state(
                mount_id="old-mount",
                source="old-device[/old-storage]",
                fstype="ext4",
                options="rw",
            )
        else:
            self.clear_mount_state()
        self._install_mocks()
        self.env = {
            **os.environ,
            "PATH": f"{self.mockbin}{os.pathsep}{os.environ['PATH']}",
            "LC_ALL": "C",
            "LUMEN_STORAGE_STATE_DIR": str(self.state_dir),
            "LUMEN_STORAGE_TARGET": str(self.target),
            "LUMEN_STORAGE_DEFAULT_LOCAL_ROOT": str(self.local_root),
            "LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS": str(self.tmp_path),
            "LUMEN_DOCKER_COMPOSE_DIR": str(self.compose_dir),
            "LUMEN_DOCKER_SERVICES": " ".join(APP_SERVICES),
            "LUMEN_DB_ROOT": str(self.db_root),
            "LUMEN_STORAGE_PROC_ROOT": str(self.proc_root),
            "MOCK_STATE_DIR": str(self.mock_state),
            "MOCK_PROC_ROOT": str(self.proc_root),
            "MOCK_TARGET": str(self.target),
            "MOCK_LOCAL_ROOT": str(self.local_root),
            "MOCK_LOCAL_SOURCE": "local-device",
            "MOCK_LOCAL_FSTYPE": "ext4",
            "TEST_DOCKER_SYSTEMD_STATE": "active",
            "TEST_DOCKER_PS_RC": "0",
            "TEST_STOP_RC": "0",
            "TEST_STOP_LEAVES_RUNNING": "",
            "TEST_BUSY_AFTER_STOP": "0",
            "TEST_MOUNT_RC": "0",
            "TEST_MOUNT_FINAL_STATE": "valid",
            "TEST_UMOUNT_REGULAR_RC": "0",
            "TEST_UMOUNT_REGULAR_FINAL": "unmounted",
            "TEST_UMOUNT_LAZY_RC": "0",
            "TEST_UMOUNT_LAZY_FINAL": "unmounted",
        }

    def _ensure_target_directory(self) -> None:
        if self.target.is_symlink():
            self.target.unlink()
        elif self.target.exists():
            shutil.rmtree(self.target)
        self.target.mkdir()

    def clear_mount_state(self) -> None:
        for name in ("mounted", "mount_id", "source", "fstype", "options"):
            (self.mock_state / name).unlink(missing_ok=True)
        self._ensure_target_directory()

    def set_mount_state(
        self,
        *,
        mount_id: str,
        source: str,
        fstype: str,
        options: str,
        local_identity: bool = False,
    ) -> None:
        self._ensure_target_directory()
        if local_identity:
            self.target.rmdir()
            self.target.symlink_to(self.local_root, target_is_directory=True)
        (self.mock_state / "mounted").touch()
        for name, value in (
            ("mount_id", mount_id),
            ("source", source),
            ("fstype", fstype),
            ("options", options),
        ):
            (self.mock_state / name).write_text(value + "\n", encoding="utf-8")

    def set_running_services(self, services: tuple[str, ...]) -> None:
        text = "".join(f"{service}\n" for service in services)
        (self.mock_state / "services.running").write_text(text, encoding="utf-8")

    def running_services(self) -> set[str]:
        path = self.mock_state / "services.running"
        return {line for line in path.read_text(encoding="utf-8").splitlines() if line}

    def add_external_container(self, container_id: str, source: Path) -> None:
        with (self.mock_state / "containers").open("a", encoding="utf-8") as file:
            file.write(f"{container_id}|{source}\n")

    def add_process_reference(
        self,
        kind: str,
        *,
        pid: int = 4242,
        under_target: bool = True,
    ) -> None:
        process_dir = self.proc_root / str(pid)
        fd_dir = process_dir / "fd"
        fd_dir.mkdir(parents=True)
        outside = self.tmp_path / "outside"
        outside.mkdir(exist_ok=True)
        target_path = self.target / "process-reference"
        target_path.mkdir(exist_ok=True)
        referenced = target_path if under_target else outside
        (process_dir / "cwd").symlink_to(
            referenced if kind == "cwd" else outside,
            target_is_directory=True,
        )
        (process_dir / "root").symlink_to(
            referenced if kind == "root" else Path("/"),
            target_is_directory=True,
        )
        if kind == "fd":
            (fd_dir / "3").symlink_to(referenced / "open-file")
        maps = ""
        if kind == "mmap":
            maps = (
                "1000-2000 r--p 00000000 00:00 0 "
                f"{referenced / 'mapped-file'}\n"
            )
        (process_dir / "maps").write_text(maps, encoding="utf-8")

    def write_config(self, mode: str) -> None:
        if mode == "local":
            text = f"MODE=local\nLOCAL_ROOT={self.local_root}\n"
        elif mode == "smb":
            text = (
                "MODE=smb\n"
                "SMB_HOST=nas.example\n"
                "SMB_SHARE=media\n"
                "SMB_SUBPATH=/images\n"
                "SMB_USERNAME=lumen\n"
                "SMB_PASSWORD=secret\n"
            )
        else:
            raise ValueError(mode)
        (self.state_dir / "storage.conf").write_text(text, encoding="utf-8")

    def run(
        self,
        command: str,
        **overrides: object,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **self.env,
            **{key: str(value) for key, value in overrides.items()},
        }
        return subprocess.run(
            ["bash", str(SCRIPT), command],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def log_lines(self, name: str) -> list[str]:
        path = self.mock_state / name
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8").splitlines()

    def status(self) -> dict[str, object]:
        return json.loads((self.state_dir / "status.json").read_text(encoding="utf-8"))

    def apply_result(self) -> dict[str, object]:
        return json.loads(
            (self.state_dir / "last-apply.json").read_text(encoding="utf-8")
        )

    def _install_mocks(self) -> None:
        _write_executable(
            self.mockbin / "mountpoint",
            r"""#!/usr/bin/env bash
path="${!#}"
if [[ "$path" == "$MOCK_TARGET" && -f "$MOCK_STATE_DIR/mounted" ]]; then
  exit 0
fi
exit 1
""",
        )
        _write_executable(
            self.mockbin / "findmnt",
            r"""#!/usr/bin/env bash
path=""
field=""
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    -T)
      path="$2"
      shift 2
      ;;
    -no)
      field="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [[ "$path" == "$MOCK_TARGET" && -f "$MOCK_STATE_DIR/mounted" ]]; then
  case "$field" in
    ID) cat "$MOCK_STATE_DIR/mount_id" ;;
    SOURCE) cat "$MOCK_STATE_DIR/source" ;;
    FSTYPE) cat "$MOCK_STATE_DIR/fstype" ;;
    OPTIONS) cat "$MOCK_STATE_DIR/options" ;;
    *) exit 1 ;;
  esac
  exit 0
fi
if [[ "$path" == "$MOCK_LOCAL_ROOT" ]]; then
  case "$field" in
    ID) printf 'local-root-mount\n' ;;
    SOURCE) printf '%s\n' "$MOCK_LOCAL_SOURCE" ;;
    FSTYPE) printf '%s\n' "$MOCK_LOCAL_FSTYPE" ;;
    OPTIONS) printf 'rw\n' ;;
    *) exit 1 ;;
  esac
  exit 0
fi
exit 1
""",
        )
        _write_executable(
            self.mockbin / "flock",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        _write_executable(
            self.mockbin / "timeout",
            '#!/usr/bin/env bash\nshift\nexec "$@"\n',
        )
        _write_executable(
            self.mockbin / "systemctl",
            r"""#!/usr/bin/env bash
if [[ "${1:-}" == "is-active" ]]; then
  printf '%s\n' "${TEST_DOCKER_SYSTEMD_STATE:-unknown}"
  [[ "${TEST_DOCKER_SYSTEMD_STATE:-unknown}" == "active" ]]
  exit
fi
exit 1
""",
        )
        _write_executable(
            self.mockbin / "fuser",
            r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_STATE_DIR/fuser.log"
printf '9999\n'
exit 0
""",
        )
        _write_executable(
            self.mockbin / "lsof",
            r"""#!/usr/bin/env bash
case "${TEST_LSOF_STATE:-idle}" in
  active)
    printf '7777\n'
    exit 1
    ;;
  idle)
    exit 1
    ;;
  error)
    printf 'target scan unavailable\n' >&2
    exit 2
    ;;
  *)
    exit 64
    ;;
esac
""",
        )
        _write_executable(
            self.mockbin / "mktemp",
            r"""#!/usr/bin/env bash
path="$MOCK_STATE_DIR/smb-credential"
: > "$path"
printf '%s\n' "$path"
""",
        )
        _write_executable(
            self.mockbin / "docker",
            r"""#!/usr/bin/env bash
running="$MOCK_STATE_DIR/services.running"
containers="$MOCK_STATE_DIR/containers"
printf '%s\n' "$*" >> "$MOCK_STATE_DIR/docker.log"

service_running() {
  grep -Fqx -- "$1" "$running"
}

remove_service() {
  local wanted="$1" line=""
  : > "${running}.tmp"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "$wanted" ]] || printf '%s\n' "$line" >> "${running}.tmp"
  done < "$running"
  mv "${running}.tmp" "$running"
}

add_service() {
  service_running "$1" || printf '%s\n' "$1" >> "$running"
}

if [[ "${1:-}" == "compose" ]]; then
  shift
  subcommand="${1:-}"
  shift || true
  case "$subcommand" in
    stop)
      if [[ "${1:-}" == "-t" ]]; then
        shift 2
      fi
      for service in "$@"; do
        case " ${TEST_STOP_LEAVES_RUNNING:-} " in
          *" $service "*) ;;
          *) remove_service "$service" ;;
        esac
      done
      if [[ "${TEST_BUSY_AFTER_STOP:-0}" == "1" ]]; then
        mkdir -p "$MOCK_PROC_ROOT/4242/fd" "$MOCK_TARGET/process-reference"
        ln -sfn "$MOCK_TARGET/process-reference" "$MOCK_PROC_ROOT/4242/cwd"
        ln -sfn / "$MOCK_PROC_ROOT/4242/root"
        : > "$MOCK_PROC_ROOT/4242/maps"
      fi
      exit "${TEST_STOP_RC:-0}"
      ;;
    ps)
      while [[ "$#" -gt 0 && "$1" != "--quiet" ]]; do
        shift
      done
      [[ "$#" -gt 0 ]] && shift
      if [[ "$#" -eq 0 ]]; then
        while IFS= read -r service || [[ -n "$service" ]]; do
          [[ -n "$service" ]] && printf 'id-%s\n' "$service"
        done < "$running"
      else
        for service in "$@"; do
          service_running "$service" && printf 'id-%s\n' "$service"
        done
      fi
      exit 0
      ;;
    start)
      for service in "$@"; do
        add_service "$service"
      done
      exit 0
      ;;
  esac
  exit 1
fi

case "${1:-}" in
  ps)
    if [[ "${TEST_DOCKER_PS_RC:-0}" -ne 0 ]]; then
      exit "$TEST_DOCKER_PS_RC"
    fi
    while IFS='|' read -r container_id source; do
      [[ -n "$container_id" && -n "$source" ]] && printf '%s\n' "$container_id"
    done < "$containers"
    ;;
  inspect)
    container_id="${!#}"
    while IFS='|' read -r current_id source; do
      if [[ "$current_id" == "$container_id" ]]; then
        printf '%s\n' "$source"
        exit 0
      fi
    done < "$containers"
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
""",
        )
        _write_executable(
            self.mockbin / "mount",
            r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_STATE_DIR/mount.log"

clear_mount() {
  rm -f "$MOCK_STATE_DIR/mounted" "$MOCK_STATE_DIR/mount_id" \
    "$MOCK_STATE_DIR/source" "$MOCK_STATE_DIR/fstype" \
    "$MOCK_STATE_DIR/options"
  rm -rf "$MOCK_TARGET"
  mkdir -p "$MOCK_TARGET"
}

write_mount() {
  local kind="$1" source="$2" fstype="$3" options="$4"
  clear_mount
  if [[ "$kind" == "local" ]]; then
    rm -rf "$MOCK_TARGET"
    ln -s "$MOCK_LOCAL_ROOT" "$MOCK_TARGET"
  fi
  touch "$MOCK_STATE_DIR/mounted"
  printf 'new-mount\n' > "$MOCK_STATE_DIR/mount_id"
  printf '%s\n' "$source" > "$MOCK_STATE_DIR/source"
  printf '%s\n' "$fstype" > "$MOCK_STATE_DIR/fstype"
  printf '%s\n' "$options" > "$MOCK_STATE_DIR/options"
}

kind=""
source=""
if [[ "${1:-}" == "--bind" ]]; then
  kind="local"
  source="$2"
elif [[ "${1:-}" == "-t" && "${2:-}" == "cifs" ]]; then
  kind="smb"
  source="$3"
else
  exit 64
fi

case "${TEST_MOUNT_FINAL_STATE:-valid}" in
  valid)
    if [[ "$kind" == "local" ]]; then
      write_mount local "${MOCK_LOCAL_SOURCE}[${MOCK_LOCAL_ROOT}]" \
        "$MOCK_LOCAL_FSTYPE" "rw,bind"
    else
      write_mount smb "$source" cifs "rw,vers=3.0"
    fi
    ;;
  unmounted)
    clear_mount
    ;;
  wrong-source)
    if [[ "$kind" == "local" ]]; then
      write_mount local "wrong-device[/wrong]" "$MOCK_LOCAL_FSTYPE" "rw,bind"
    else
      write_mount smb "//wrong.example/share" cifs "rw,vers=3.0"
    fi
    ;;
  wrong-fstype)
    if [[ "$kind" == "local" ]]; then
      write_mount local "${MOCK_LOCAL_SOURCE}[${MOCK_LOCAL_ROOT}]" xfs "rw,bind"
    else
      write_mount smb "$source" ext4 "rw"
    fi
    ;;
  unchanged)
    ;;
  *)
    exit 65
    ;;
esac
exit "${TEST_MOUNT_RC:-0}"
""",
        )
        _write_executable(
            self.mockbin / "umount",
            r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_STATE_DIR/umount.log"

clear_mount() {
  rm -f "$MOCK_STATE_DIR/mounted" "$MOCK_STATE_DIR/mount_id" \
    "$MOCK_STATE_DIR/source" "$MOCK_STATE_DIR/fstype" \
    "$MOCK_STATE_DIR/options"
  rm -rf "$MOCK_TARGET"
  mkdir -p "$MOCK_TARGET"
}

if [[ "${1:-}" == "-l" ]]; then
  final="${TEST_UMOUNT_LAZY_FINAL:-unmounted}"
  rc="${TEST_UMOUNT_LAZY_RC:-0}"
else
  final="${TEST_UMOUNT_REGULAR_FINAL:-unmounted}"
  rc="${TEST_UMOUNT_REGULAR_RC:-0}"
fi
case "$final" in
  unmounted)
    clear_mount
    ;;
  mounted)
    ;;
  changed)
    touch "$MOCK_STATE_DIR/mounted"
    printf 'replacement-mount\n' > "$MOCK_STATE_DIR/mount_id"
    printf 'replacement-source\n' > "$MOCK_STATE_DIR/source"
    printf 'ext4\n' > "$MOCK_STATE_DIR/fstype"
    printf 'rw\n' > "$MOCK_STATE_DIR/options"
    ;;
  *)
    exit 65
    ;;
esac
exit "$rc"
""",
        )


def test_storage_mount_rejects_unsafe_local_root_before_mount(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=(),
    )
    (harness.state_dir / "storage.conf").write_text(
        "MODE=local\nLOCAL_ROOT=/etc\n",
        encoding="utf-8",
    )

    result = harness.run("up")

    assert result.returncode == 2
    assert "refusing unsafe local root: /etc" in result.stderr
    assert harness.log_lines("mount.log") == []


def test_storage_up_propagates_mount_command_failure(tmp_path: Path) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=(),
    )

    result = harness.run(
        "up",
        TEST_MOUNT_RC=32,
        TEST_MOUNT_FINAL_STATE="unmounted",
    )

    assert result.returncode == 32
    assert harness.status()["mounted"] is False
    assert len(harness.log_lines("mount.log")) == 1


@pytest.mark.parametrize("mode", ("local", "smb"))
@pytest.mark.parametrize(
    ("mount_rc", "final_state", "expected_success"),
    (
        (0, "valid", True),
        (0, "unmounted", False),
        (0, "wrong-source", False),
        (0, "wrong-fstype", False),
        (32, "valid", False),
    ),
)
def test_storage_apply_requires_command_success_and_valid_mount_postcondition(
    tmp_path: Path,
    mode: str,
    mount_rc: int,
    final_state: str,
    expected_success: bool,
) -> None:
    harness = StorageHarness(tmp_path, mode=mode)

    result = harness.run(
        "apply",
        TEST_MOUNT_RC=mount_rc,
        TEST_MOUNT_FINAL_STATE=final_state,
    )

    docker_log = harness.log_lines("docker.log")
    assert "compose stop -t 30 api worker tgbot web postgres redis" in docker_log
    assert (
        "compose ps --status running --quiet api worker tgbot web postgres redis"
        in docker_log
    )
    if expected_success:
        assert result.returncode == 0, result.stderr
        assert harness.apply_result()["status"] == "ok"
        assert "compose start postgres redis" in docker_log
        assert "compose start api worker tgbot web" in docker_log
        assert harness.running_services() == set(ALL_SERVICES)
    else:
        assert result.returncode == 1
        assert harness.apply_result()["status"] == "fail"
        assert "compose start postgres redis" not in docker_log
        assert harness.running_services() == set()
    assert not (harness.mock_state / "smb-credential").exists()


def test_storage_apply_rejects_service_still_running_after_stop(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run("apply", TEST_STOP_LEAVES_RUNNING="worker")

    assert result.returncode == 1
    assert "declared Docker services are still running" in result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []
    assert harness.running_services() == set(ALL_SERVICES)
    assert harness.apply_result()["status"] == "fail"


def test_storage_apply_rejects_busy_target_after_successful_stop(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run("apply", TEST_BUSY_AFTER_STOP=1)

    assert result.returncode == 1
    assert "still busy after Docker services stopped" in result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []
    assert harness.running_services() == set(ALL_SERVICES)


@pytest.mark.parametrize("reference_kind", ("cwd", "root", "fd", "mmap"))
def test_storage_down_rejects_exact_process_reference_under_target(
    tmp_path: Path,
    reference_kind: str,
) -> None:
    harness = StorageHarness(tmp_path, running_services=())
    harness.add_process_reference(reference_kind)

    result = harness.run("down")

    assert result.returncode == 1
    assert "still busy after Docker services stopped" in result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("fuser.log") == []


def test_storage_down_ignores_unrelated_process_on_same_filesystem(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, running_services=())
    harness.add_process_reference("cwd", under_target=False)

    result = harness.run("down")

    assert result.returncode == 0, result.stderr
    assert harness.log_lines("fuser.log") == []
    assert len(harness.log_lines("umount.log")) == 1


def test_storage_down_fails_closed_when_target_process_scan_is_unavailable(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, running_services=())

    result = harness.run(
        "down",
        LUMEN_STORAGE_PROC_ROOT=tmp_path / "missing-proc",
        TEST_LSOF_STATE="error",
    )

    assert result.returncode == 1
    assert "cannot verify that target" in result.stderr
    assert "lsof could not verify" in result.stderr
    assert harness.log_lines("umount.log") == []


def test_storage_apply_rejects_unlisted_running_container_using_target(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    harness.add_external_container("external-1", harness.target / "storage")

    result = harness.run("apply")

    assert result.returncode == 1
    assert "running Docker containers still use" in result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []
    assert "inspect --format" in "\n".join(harness.log_lines("docker.log"))


def test_storage_apply_requires_compose_stop_workflow(tmp_path: Path) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run(
        "apply",
        LUMEN_DOCKER_COMPOSE_DIR=tmp_path / "missing-compose",
    )

    assert result.returncode == 1
    assert "docker compose is unavailable" in result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []


def test_storage_down_rejects_running_declared_service(tmp_path: Path) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run("down")

    assert result.returncode == 1
    assert "declared Docker services are still running" in result.stderr
    assert harness.log_lines("umount.log") == []


def test_storage_down_rejects_external_target_container(tmp_path: Path) -> None:
    harness = StorageHarness(tmp_path, running_services=())
    harness.add_external_container("external-2", harness.target)

    result = harness.run("down")

    assert result.returncode == 1
    assert "running Docker containers still use" in result.stderr
    assert harness.log_lines("umount.log") == []


def test_storage_smb_up_never_replaces_active_nonmatching_mount(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, mode="smb")

    result = harness.run("up")

    assert result.returncode == 1
    assert "use apply" in result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []
    assert harness.running_services() == set(ALL_SERVICES)


def test_storage_up_rejects_unmounted_target_while_services_run(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, initial_mounted=False)

    result = harness.run("up")

    assert result.returncode == 1
    assert "storage users are not proven stopped" in result.stderr
    assert harness.log_lines("mount.log") == []


@pytest.mark.parametrize(
    (
        "regular_rc",
        "regular_final",
        "lazy_rc",
        "lazy_final",
        "expected_rc",
        "expected_calls",
    ),
    (
        (0, "unmounted", 0, "mounted", 0, 1),
        (32, "unmounted", 0, "mounted", 0, 1),
        (0, "mounted", 0, "unmounted", 0, 2),
        (32, "mounted", 32, "unmounted", 0, 2),
        (0, "mounted", 0, "mounted", 1, 2),
        (32, "mounted", 32, "mounted", 1, 2),
    ),
)
def test_storage_down_uses_post_unmount_state_as_truth(
    tmp_path: Path,
    regular_rc: int,
    regular_final: str,
    lazy_rc: int,
    lazy_final: str,
    expected_rc: int,
    expected_calls: int,
) -> None:
    harness = StorageHarness(tmp_path, running_services=())

    result = harness.run(
        "down",
        TEST_UMOUNT_REGULAR_RC=regular_rc,
        TEST_UMOUNT_REGULAR_FINAL=regular_final,
        TEST_UMOUNT_LAZY_RC=lazy_rc,
        TEST_UMOUNT_LAZY_FINAL=lazy_final,
    )

    assert result.returncode == expected_rc
    assert len(harness.log_lines("umount.log")) == expected_calls
    assert harness.status()["mounted"] is (expected_rc != 0)


@pytest.mark.parametrize(
    ("lazy_final", "expect_restart"),
    (("mounted", True), ("changed", False)),
)
def test_storage_apply_restarts_db_only_when_old_mount_is_still_valid(
    tmp_path: Path,
    lazy_final: str,
    expect_restart: bool,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run(
        "apply",
        TEST_UMOUNT_REGULAR_RC=32,
        TEST_UMOUNT_REGULAR_FINAL="mounted",
        TEST_UMOUNT_LAZY_RC=32,
        TEST_UMOUNT_LAZY_FINAL=lazy_final,
    )

    assert result.returncode == 1
    docker_log = harness.log_lines("docker.log")
    assert ("compose start postgres redis" in docker_log) is expect_restart
    if expect_restart:
        assert harness.running_services() == set(ALL_SERVICES)
    else:
        assert harness.running_services() == set()
        assert "previous mount is no longer valid" in result.stderr
    assert harness.log_lines("mount.log") == []


def test_storage_apply_without_old_mount_keeps_db_stopped_on_failure(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, initial_mounted=False)

    result = harness.run("apply", TEST_BUSY_AFTER_STOP=1)

    assert result.returncode == 1
    assert "compose start postgres redis" not in harness.log_lines("docker.log")
    assert harness.running_services() == set()
    assert "no valid previous mount exists" in result.stderr


def test_storage_down_allows_verified_docker_inactive_shutdown(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run(
        "down",
        TEST_DOCKER_SYSTEMD_STATE="inactive",
        TEST_DOCKER_PS_RC=1,
        TEST_UMOUNT_REGULAR_RC=32,
        TEST_UMOUNT_REGULAR_FINAL="unmounted",
    )

    assert result.returncode == 0, result.stderr
    assert harness.status()["mounted"] is False
    assert harness.log_lines("docker.log") == ["ps --quiet"]


def test_storage_down_does_not_trust_inactive_unit_when_daemon_responds(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run("down", TEST_DOCKER_SYSTEMD_STATE="inactive")

    assert result.returncode == 1
    assert "declared Docker services are still running" in result.stderr
    assert harness.log_lines("umount.log") == []


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux mount namespaces")
def test_storage_down_real_bind_ignores_unrelated_root_filesystem_process(
    tmp_path: Path,
) -> None:
    unshare = shutil.which("unshare")
    real_mount = shutil.which("mount")
    if unshare is None or real_mount is None:
        pytest.skip("unshare or mount is unavailable")

    harness = StorageHarness(tmp_path, running_services=())
    unrelated = tmp_path / "unrelated"
    bind_ready = tmp_path / "bind-ready"
    unrelated.mkdir()
    env = {
        **harness.env,
        "LUMEN_STORAGE_PROC_ROOT": "/proc",
    }
    script = f"""
    set -eu
    {shlex.quote(real_mount)} --make-rprivate /
    {shlex.quote(real_mount)} --bind \
      {shlex.quote(str(harness.local_root))} {shlex.quote(str(harness.target))}
    : > {shlex.quote(str(bind_ready))}
    (
      cd {shlex.quote(str(unrelated))}
      exec sleep 20
    ) &
    unrelated_pid=$!
    trap 'kill "$unrelated_pid" 2>/dev/null || true' EXIT
    bash {shlex.quote(str(SCRIPT))} down
    """
    result = subprocess.run(
        [
            unshare,
            "--user",
            "--map-root-user",
            "--mount",
            "--pid",
            "--fork",
            "--mount-proc",
            "bash",
            "-c",
            script,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if not bind_ready.exists():
        pytest.skip(f"mount namespace unavailable: {result.stderr.strip()}")

    assert result.returncode == 0, result.stderr + result.stdout
    assert harness.log_lines("fuser.log") == []
