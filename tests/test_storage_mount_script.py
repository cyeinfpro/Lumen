from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


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
        self.maintenance_root = tmp_path / "deploy"
        self.locking_lib = tmp_path / "locking.sh"
        self.db_root = self.target if db_moves_with_target else tmp_path / "database"
        for path in (
            self.mockbin,
            self.state_dir,
            self.mock_state,
            self.proc_root,
            self.target,
            self.local_root,
            self.compose_dir,
            self.maintenance_root,
        ):
            path.mkdir()
        (self.state_dir / "requests").mkdir()
        (self.state_dir / "results").mkdir()
        (self.compose_dir / "docker-compose.yml").write_text(
            "services: {}\n",
            encoding="utf-8",
        )
        self.apply_operation_id = "a" * 32
        self.apply_fence = 0
        self.write_config(mode)
        self.set_running_services(running_services)
        (self.mock_state / "containers").write_text("", encoding="utf-8")
        if initial_mounted:
            self.set_mount_state(
                mount_id="old-mount",
                source="//old.example/archive/images",
                fstype="cifs",
                options="rw,vers=3.0",
            )
            self.write_last_good_smb()
        else:
            self.clear_mount_state()
        self._install_mocks()
        self.locking_lib.write_text(
            """lumen_try_acquire_lock() {
  [ "${TEST_MAINTENANCE_LOCK_AVAILABLE:-1}" = "1" ]
}
lumen_release_lock() { :; }
""",
            encoding="utf-8",
        )
        self.env = {
            **os.environ,
            "PATH": f"{self.mockbin}{os.pathsep}{os.environ['PATH']}",
            "LC_ALL": "C",
            "LUMEN_STORAGE_STATE_DIR": str(self.state_dir),
            "LUMEN_STORAGE_TARGET": str(self.target),
            "LUMEN_STORAGE_DEFAULT_LOCAL_ROOT": str(self.local_root),
            "LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS": str(self.tmp_path),
            "LUMEN_DOCKER_COMPOSE_DIR": str(self.compose_dir),
            "LUMEN_STORAGE_LOCKING_LIB": str(self.locking_lib),
            "LUMEN_STORAGE_MAINTENANCE_ROOT": str(self.maintenance_root),
            "LUMEN_DOCKER_SERVICES": " ".join(APP_SERVICES),
            "LUMEN_DB_ROOT": str(self.db_root),
            "LUMEN_STORAGE_PROC_ROOT": str(self.proc_root),
            "MOCK_STATE_DIR": str(self.mock_state),
            "MOCK_PROC_ROOT": str(self.proc_root),
            "MOCK_TARGET": str(self.target),
            "MOCK_LOCAL_ROOT": str(self.local_root),
            "MOCK_LOCAL_SOURCE": "local-device",
            "MOCK_LOCAL_FSTYPE": "ext4",
            "MOCK_LOCAL_MOUNT_TARGET": "/",
            "TEST_DOCKER_SYSTEMD_STATE": "active",
            "TEST_FALLBACK_SYSTEMD_STATE": "inactive",
            "TEST_DOCKER_PS_RC": "0",
            "TEST_STOP_RC": "0",
            "TEST_STOP_LEAVES_RUNNING": "",
            "TEST_BUSY_AFTER_STOP": "0",
            "TEST_MOUNT_RC": "0",
            "TEST_MOUNT_FINAL_STATE": "valid",
            "TEST_ROLLBACK_MOUNT_RC": "0",
            "TEST_ROLLBACK_MOUNT_FINAL_STATE": "valid",
            "TEST_UMOUNT_REGULAR_RC": "0",
            "TEST_UMOUNT_REGULAR_FINAL": "unmounted",
            "TEST_UMOUNT_LAZY_RC": "0",
            "TEST_UMOUNT_LAZY_FINAL": "unmounted",
            "TEST_API_READY": "1",
            "TEST_WORKER_READY": "1",
            "TEST_API_READY_FAILURES": "0",
            "TEST_WORKER_READY_FAILURES": "0",
            "TEST_START_FAIL_CALL": "0",
            "TEST_START_RC": "1",
            "TEST_KILL_RC": "0",
            "TEST_MAINTENANCE_LOCK_AVAILABLE": "1",
            "TEST_PRESERVE_DIRECT_TARGET": "0",
            "LUMEN_STORAGE_CORE_READINESS_ATTEMPTS": "1",
            "LUMEN_STORAGE_CORE_READINESS_INTERVAL_SECONDS": "0",
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
            maps = f"1000-2000 r--p 00000000 00:00 0 {referenced / 'mapped-file'}\n"
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
        self.write_apply_request(text)

    def write_apply_request(
        self,
        conf_text: str,
        *,
        operation_id: str | None = None,
        fence: int | None = None,
    ) -> Path:
        if fence is None:
            self.apply_fence += 1
            fence = self.apply_fence
        else:
            self.apply_fence = max(self.apply_fence, fence)
        operation_id = operation_id or self.apply_operation_id
        payload = {
            "schema": 1,
            "operation_id": operation_id,
            "fence": fence,
            "config_sha256": hashlib.sha256(conf_text.encode("utf-8")).hexdigest(),
            "config": conf_text,
        }
        path = self.state_dir / "requests" / f"{operation_id}.{fence}.json"
        path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def write_last_good_smb(self) -> None:
        dataset_identity = "a" * 64
        (self.target / ".lumen-storage-dataset-id").write_text(
            dataset_identity + "\n",
            encoding="ascii",
        )
        fields = {
            "MODE": "smb",
            "LOCAL_ROOT": str(self.local_root),
            "SMB_HOST": "old.example",
            "SMB_PORT": "",
            "SMB_SHARE": "archive",
            "SMB_SUBPATH": "/images",
            "SMB_USERNAME": "old-user",
            "SMB_PASSWORD": "old-secret",
            "MOUNT_TARGET": str(self.target),
            "MOUNT_SOURCE": "//old.example/archive/images",
            "MOUNT_FSTYPE": "cifs",
            "DATASET_IDENTITY": dataset_identity,
            "LOCAL_BACKING_TARGET": "",
            "LOCAL_BACKING_SOURCE": "",
            "LOCAL_BACKING_FSTYPE": "",
        }
        text = "".join(f"{key}={shlex.quote(value)}\n" for key, value in fields.items())
        path = self.state_dir / "last-good.conf"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o640)

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
    TARGET) printf '%s\n' "$MOCK_TARGET" ;;
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
    TARGET) printf '%s\n' "${MOCK_LOCAL_MOUNT_TARGET:-/}" ;;
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
            self.mockbin / "curl",
            r"""#!/usr/bin/env bash
url="${@: -1}"
printf '%s\n' "$url" >> "$MOCK_STATE_DIR/curl.log"
if [[ "$url" == */readyz ]]; then
  count_file="$MOCK_STATE_DIR/api-readiness.count"
  count=0
  [[ -f "$count_file" ]] && count="$(cat "$count_file")"
  count=$((count + 1))
  printf '%s\n' "$count" > "$count_file"
  mount_source="$(cat "$MOCK_STATE_DIR/source" 2>/dev/null || true)"
  last_good_source="$(
    sed -n 's/^MOUNT_SOURCE=//p' \
      "$LUMEN_STORAGE_STATE_DIR/last-good.conf" 2>/dev/null || true
  )"
  printf 'readiness api mount=%s last-good=%s\n' \
    "$mount_source" "$last_good_source" >> "$MOCK_STATE_DIR/events.log"
  if [[ "${TEST_API_READY:-1}" != "1" \
    || "$count" -le "${TEST_API_READY_FAILURES:-0}" ]]; then
    exit 22
  fi
fi
exit 0
""",
        )
        _write_executable(
            self.mockbin / "systemctl",
            r"""#!/usr/bin/env bash
if [[ "${1:-}" == "is-active" ]]; then
  unit="${@: -1}"
  if [[ "$unit" == lumen-*.service ]]; then
    printf '%s\n' "${TEST_FALLBACK_SYSTEMD_STATE:-inactive}"
    [[ "${TEST_FALLBACK_SYSTEMD_STATE:-inactive}" == "active" ]]
  else
    printf '%s\n' "${TEST_DOCKER_SYSTEMD_STATE:-unknown}"
    [[ "${TEST_DOCKER_SYSTEMD_STATE:-unknown}" == "active" ]]
  fi
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
printf 'docker %s\n' "$*" >> "$MOCK_STATE_DIR/events.log"

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

record_readiness() {
  local probe="$1" mount_source="" last_good_source=""
  mount_source="$(cat "$MOCK_STATE_DIR/source" 2>/dev/null || true)"
  last_good_source="$(
    sed -n 's/^MOUNT_SOURCE=//p' \
      "$LUMEN_STORAGE_STATE_DIR/last-good.conf" 2>/dev/null || true
  )"
  printf 'readiness %s mount=%s last-good=%s\n' \
    "$probe" "$mount_source" "$last_good_source" \
    >> "$MOCK_STATE_DIR/events.log"
}

if [[ "${1:-}" == "compose" ]]; then
  shift
  subcommand="${1:-}"
  shift || true
  case "$subcommand" in
    exec)
      count_file="$MOCK_STATE_DIR/worker-readiness.count"
      count=0
      [[ -f "$count_file" ]] && count="$(cat "$count_file")"
      count=$((count + 1))
      printf '%s\n' "$count" > "$count_file"
      record_readiness worker
      [[ "${TEST_WORKER_READY:-1}" == "1" \
        && "$count" -gt "${TEST_WORKER_READY_FAILURES:-0}" ]]
      exit
      ;;
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
    kill)
      if [[ "${1:-}" == "-s" ]]; then
        shift 2
      fi
      for service in "$@"; do
        remove_service "$service"
      done
      exit "${TEST_KILL_RC:-0}"
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
      count_file="$MOCK_STATE_DIR/start.count"
      count=0
      [[ -f "$count_file" ]] && count="$(cat "$count_file")"
      count=$((count + 1))
      printf '%s\n' "$count" > "$count_file"
      for service in "$@"; do
        add_service "$service"
      done
      if [[ "$count" -eq "${TEST_START_FAIL_CALL:-0}" ]]; then
        exit "${TEST_START_RC:-1}"
      fi
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
printf 'mount %s\n' "$*" >> "$MOCK_STATE_DIR/events.log"

count_file="$MOCK_STATE_DIR/mount.count"
count=0
[[ -f "$count_file" ]] && count="$(cat "$count_file")"
count=$((count + 1))
printf '%s\n' "$count" > "$count_file"

clear_mount() {
  rm -f "$MOCK_STATE_DIR/mounted" "$MOCK_STATE_DIR/mount_id" \
    "$MOCK_STATE_DIR/source" "$MOCK_STATE_DIR/fstype" \
    "$MOCK_STATE_DIR/options"
  rm -rf "$MOCK_TARGET"
  mkdir -p "$MOCK_TARGET"
}

write_mount() {
  local kind="$1" source="$2" fstype="$3" options="$4"
  if [[ "${TEST_PRESERVE_DIRECT_TARGET:-0}" == "1" \
    && ! -f "$MOCK_STATE_DIR/mounted" ]]; then
    rm -rf "$MOCK_STATE_DIR/direct-target"
    mkdir -p "$MOCK_STATE_DIR/direct-target"
    cp -a "$MOCK_TARGET/." "$MOCK_STATE_DIR/direct-target/"
  fi
  clear_mount
  if [[ "$kind" == "local" ]]; then
    rm -rf "$MOCK_TARGET"
    ln -s "$MOCK_LOCAL_ROOT" "$MOCK_TARGET"
  elif [[ "$source" == "//old.example/archive/images" ]]; then
    printf '%064d\n' 0 | tr '0' 'a' \
      > "$MOCK_TARGET/.lumen-storage-dataset-id"
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

final_state="${TEST_MOUNT_FINAL_STATE:-valid}"
mount_rc="${TEST_MOUNT_RC:-0}"
if [[ "$count" -gt 1 ]]; then
  final_state="${TEST_ROLLBACK_MOUNT_FINAL_STATE:-valid}"
  mount_rc="${TEST_ROLLBACK_MOUNT_RC:-0}"
fi

case "$final_state" in
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
exit "$mount_rc"
""",
        )
        _write_executable(
            self.mockbin / "umount",
            r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$MOCK_STATE_DIR/umount.log"
printf 'umount %s\n' "$*" >> "$MOCK_STATE_DIR/events.log"

clear_mount() {
  rm -f "$MOCK_STATE_DIR/mounted" "$MOCK_STATE_DIR/mount_id" \
    "$MOCK_STATE_DIR/source" "$MOCK_STATE_DIR/fstype" \
    "$MOCK_STATE_DIR/options"
  rm -rf "$MOCK_TARGET"
  mkdir -p "$MOCK_TARGET"
  if [[ "${TEST_PRESERVE_DIRECT_TARGET:-0}" == "1" \
    && -d "$MOCK_STATE_DIR/direct-target" ]]; then
    cp -a "$MOCK_STATE_DIR/direct-target/." "$MOCK_TARGET/"
  fi
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


@pytest.mark.parametrize(
    "final_state",
    ("unmounted", "wrong-source", "wrong-fstype"),
)
def test_storage_up_rejects_zero_exit_mount_with_wrong_identity(
    tmp_path: Path,
    final_state: str,
) -> None:
    harness = StorageHarness(
        tmp_path,
        mode="smb",
        initial_mounted=False,
        running_services=(),
    )

    result = harness.run(
        "up",
        TEST_MOUNT_RC=0,
        TEST_MOUNT_FINAL_STATE=final_state,
    )

    assert result.returncode == 1
    assert not (harness.state_dir / "last-good.conf").exists()
    assert len(harness.log_lines("mount.log")) == 1


def test_boot_up_restores_last_good_instead_of_promoting_candidate(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        mode="local",
        initial_mounted=True,
        running_services=(),
    )
    previous_last_good = (harness.state_dir / "last-good.conf").read_bytes()
    harness.clear_mount_state()

    result = harness.run("up")

    assert result.returncode == 0, result.stderr
    assert harness.status()["source"] == "//old.example/archive/images"
    restored_conf = (harness.state_dir / "storage.conf").read_text(encoding="utf-8")
    assert "SMB_HOST=old.example" in restored_conf
    assert "SMB_SHARE=archive" in restored_conf
    assert (harness.state_dir / "last-good.conf").read_bytes() == previous_last_good


def test_fresh_install_unmanaged_marker_keeps_direct_data_unmounted(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=(),
    )
    sentinel = harness.target / "sentinel.txt"
    sentinel.write_text("direct-data\n", encoding="utf-8")
    (harness.state_dir / "unmanaged-direct").write_text(
        "schema=1\nmode=unmanaged-direct\n",
        encoding="utf-8",
    )

    result = harness.run("up")

    assert result.returncode == 0, result.stderr
    assert sentinel.read_text(encoding="utf-8") == "direct-data\n"
    assert harness.log_lines("mount.log") == []
    assert not (harness.state_dir / "last-good.conf").exists()


def test_first_apply_failure_restores_unmanaged_direct_baseline(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=ALL_SERVICES,
    )
    sentinel = harness.target / "sentinel.txt"
    sentinel.write_text("direct-data\n", encoding="utf-8")
    (harness.state_dir / "unmanaged-direct").write_text(
        "schema=1\nmode=unmanaged-direct\n",
        encoding="utf-8",
    )

    result = harness.run(
        "apply",
        TEST_API_READY_FAILURES=1,
        TEST_PRESERVE_DIRECT_TARGET=1,
    )

    assert result.returncode == 1
    assert "restored and verified previous mount" in (harness.apply_result()["message"])
    assert sentinel.read_text(encoding="utf-8") == "direct-data\n"
    assert harness.running_services() == set(ALL_SERVICES)
    assert not (harness.state_dir / "last-good.conf").exists()


def test_successful_managed_apply_removes_unmanaged_direct_marker(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=ALL_SERVICES,
    )
    marker = harness.state_dir / "unmanaged-direct"
    marker.write_text("schema=1\nmode=unmanaged-direct\n", encoding="utf-8")

    result = harness.run("apply")

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (harness.state_dir / "last-good.conf").exists()


def test_stale_unmanaged_marker_cannot_restore_missing_managed_mount(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=ALL_SERVICES,
    )
    harness.write_last_good_smb()
    (harness.state_dir / "unmanaged-direct").write_text(
        "schema=1\nmode=unmanaged-direct\n",
        encoding="utf-8",
    )

    result = harness.run("apply")

    assert result.returncode == 1
    assert "no verified rollback identity" in result.stderr
    assert harness.running_services() == set(ALL_SERVICES)


def test_storage_apply_rejects_active_systemd_fallback_writer(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run(
        "apply",
        LUMEN_SYSTEMD_RUNTIME_AVAILABLE=1,
        TEST_FALLBACK_SYSTEMD_STATE="active",
    )

    assert result.returncode == 1
    assert harness.apply_result()["message"] == (
        "systemd fallback writers are active or unverifiable"
    )
    assert "compose stop" not in "\n".join(harness.log_lines("docker.log"))


def test_storage_apply_requires_global_maintenance_lock(tmp_path: Path) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run("apply", TEST_MAINTENANCE_LOCK_AVAILABLE=0)

    assert result.returncode == 1
    assert harness.apply_result()["message"] == (
        "another maintenance operation is active"
    )
    assert "compose stop" not in "\n".join(harness.log_lines("docker.log"))
    assert harness.log_lines("umount.log") == []


def test_storage_verify_rejects_replaced_dataset_with_same_mount_source(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, running_services=())
    boot = harness.run("up")
    assert boot.returncode == 0, boot.stderr
    (harness.target / ".lumen-storage-dataset-id").write_text(
        "b" * 64 + "\n",
        encoding="ascii",
    )

    verified = harness.run("verify")

    assert verified.returncode == 1
    assert "last verified mount identity" in verified.stderr


def test_bind_identity_upgrades_verified_legacy_last_good(tmp_path: Path) -> None:
    harness = StorageHarness(tmp_path, running_services=())
    last_good = harness.state_dir / "last-good.conf"
    legacy = "\n".join(
        line
        for line in last_good.read_text(encoding="utf-8").splitlines()
        if not line.startswith("DATASET_IDENTITY=")
    )
    last_good.write_text(legacy + "\n", encoding="utf-8")
    (harness.target / ".lumen-storage-dataset-id").unlink()

    result = harness.run("bind-identity")

    assert result.returncode == 0, result.stderr
    dataset_identity = (
        (harness.target / ".lumen-storage-dataset-id")
        .read_text(encoding="ascii")
        .strip()
    )
    assert len(dataset_identity) == 64
    assert f"DATASET_IDENTITY={dataset_identity}\n" in last_good.read_text(
        encoding="utf-8"
    )


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
        assert (
            "restored and verified previous mount" in harness.apply_result()["message"]
        )
        assert "compose start postgres redis" in docker_log
        assert "compose start api worker tgbot web" in docker_log
        assert harness.running_services() == set(ALL_SERVICES)
        assert harness.status()["source"] == "//old.example/archive/images"
        assert len(harness.log_lines("mount.log")) == 2
        assert "falling back to local default" not in result.stderr
    assert not (harness.mock_state / "smb-credential").exists()


@pytest.mark.parametrize(
    ("overrides", "expected_probe"),
    (
        ({"TEST_API_READY_FAILURES": "1"}, "api"),
        ({"TEST_WORKER_READY_FAILURES": "1"}, "worker"),
    ),
)
def test_storage_apply_readiness_failure_stops_writers_before_rollback(
    tmp_path: Path,
    overrides: dict[str, str],
    expected_probe: str,
) -> None:
    harness = StorageHarness(tmp_path)
    previous_last_good = (harness.state_dir / "last-good.conf").read_bytes()

    result = harness.run("apply", **overrides)

    assert result.returncode == 1
    assert harness.apply_result()["status"] == "fail"
    assert harness.apply_result()["message"] == (
        "new mount readiness failed; restored and verified previous mount"
    )
    assert "API/Worker readiness failed; rolling back" in result.stderr
    assert not any((harness.state_dir / "requests").glob("*.json"))
    assert harness.running_services() == set(ALL_SERVICES)
    assert harness.status()["source"] == "//old.example/archive/images"
    assert (harness.state_dir / "last-good.conf").read_bytes() == previous_last_good

    events = harness.log_lines("events.log")
    failed_readiness = next(
        index
        for index, event in enumerate(events)
        if event.startswith(f"readiness {expected_probe} ")
    )
    rollback_stop = next(
        index
        for index, event in enumerate(events)
        if index > failed_readiness
        and event == "docker compose stop -t 30 api worker tgbot web postgres redis"
    )
    rollback_umount = next(
        index
        for index, event in enumerate(events)
        if index > rollback_stop and event.startswith("umount ")
    )
    rollback_mount = next(
        index
        for index, event in enumerate(events)
        if index > rollback_umount and event.startswith("mount ")
    )
    rollback_start = next(
        index
        for index, event in enumerate(events)
        if index > rollback_mount and event == "docker compose start postgres redis"
    )
    assert failed_readiness < rollback_stop < rollback_umount
    assert rollback_umount < rollback_mount < rollback_start
    readiness_events = [event for event in events if event.startswith("readiness ")]
    assert all(
        "last-good=//old.example/archive/images" in event for event in readiness_events
    )
    assert any(
        "mount=//old.example/archive/images" in event for event in readiness_events
    )


@pytest.mark.parametrize(
    ("start_fail_call", "failure_reason"),
    (
        (1, "new mount postgres/redis startup failed"),
        (2, "new mount application startup failed"),
    ),
)
def test_storage_apply_start_failure_stops_partial_writers_and_rolls_back(
    tmp_path: Path,
    start_fail_call: int,
    failure_reason: str,
) -> None:
    harness = StorageHarness(tmp_path)
    previous_last_good = (harness.state_dir / "last-good.conf").read_bytes()

    result = harness.run("apply", TEST_START_FAIL_CALL=start_fail_call)

    assert result.returncode == 1
    assert harness.apply_result()["message"] == (
        f"{failure_reason}; restored and verified previous mount"
    )
    assert harness.running_services() == set(ALL_SERVICES)
    assert harness.status()["source"] == "//old.example/archive/images"
    assert (harness.state_dir / "last-good.conf").read_bytes() == previous_last_good
    events = harness.log_lines("events.log")
    start_events = [
        index
        for index, event in enumerate(events)
        if event.startswith("docker compose start ")
    ]
    failed_start = start_events[start_fail_call - 1]
    rollback_stop = next(
        index
        for index, event in enumerate(events)
        if index > failed_start
        and event == "docker compose stop -t 30 api worker tgbot web postgres redis"
    )
    rollback_umount = next(
        index
        for index, event in enumerate(events)
        if index > rollback_stop and event.startswith("umount ")
    )
    rollback_mount = next(
        index
        for index, event in enumerate(events)
        if index > rollback_umount and event.startswith("mount ")
    )
    assert failed_start < rollback_stop < rollback_umount < rollback_mount
    assert all(
        "mount=//old.example/archive/images" in event
        for event in events
        if event.startswith("readiness ")
    )


def test_storage_apply_keeps_services_stopped_when_old_readiness_fails(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    previous_last_good = (harness.state_dir / "last-good.conf").read_bytes()

    result = harness.run("apply", TEST_API_READY_FAILURES=2)

    assert result.returncode == 1
    assert harness.apply_result()["message"] == (
        "new mount readiness failed; previous mount restored but service recovery failed"
    )
    assert harness.status()["source"] == "//old.example/archive/images"
    assert harness.running_services() == set()
    assert (harness.state_dir / "last-good.conf").read_bytes() == previous_last_good
    events = harness.log_lines("events.log")
    readiness_events = [
        (index, event)
        for index, event in enumerate(events)
        if event.startswith("readiness api ")
    ]
    assert len(readiness_events) == 2
    final_stop = next(
        index
        for index, event in enumerate(events)
        if index > readiness_events[-1][0]
        and event == "docker compose stop -t 30 api worker tgbot web postgres redis"
    )
    assert final_stop > readiness_events[-1][0]


def test_storage_apply_promotes_last_good_only_after_core_readiness(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    previous_last_good = (harness.state_dir / "last-good.conf").read_bytes()

    result = harness.run("apply")

    assert result.returncode == 0, result.stderr
    readiness_events = [
        event
        for event in harness.log_lines("events.log")
        if event.startswith("readiness ")
    ]
    assert [event.split()[1] for event in readiness_events] == ["api", "worker"]
    assert all(
        "last-good=//old.example/archive/images" in event for event in readiness_events
    )
    assert all(
        "mount=//old.example/archive/images" not in event for event in readiness_events
    )
    promoted_last_good = (harness.state_dir / "last-good.conf").read_bytes()
    assert promoted_last_good != previous_last_good
    assert b"MODE=local\n" in promoted_last_good
    assert b"//old.example/archive/images" not in promoted_last_good


def test_storage_apply_unit_watches_immutable_request_directory() -> None:
    path_unit = (ROOT / "deploy/systemd/lumen-storage-apply.path").read_text(
        encoding="utf-8"
    )
    unit = (ROOT / "deploy/systemd/lumen-storage-apply.service").read_text(
        encoding="utf-8"
    )

    assert "DirectoryNotEmpty=/var/lib/lumen-storage/requests" in path_unit
    assert "apply.trigger" not in path_unit
    assert "apply.trigger" not in unit


def test_repeated_storage_operation_id_returns_terminal_without_remount(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    call_id = "a" * 32
    result = {
        "call_id": call_id,
        "operation_id": call_id,
        "fence": 1,
        "status": "ok",
        "message": "already applied",
        "started_at": 1,
        "finished_at": 2,
    }
    (harness.state_dir / "results" / f"{call_id}.1.json").write_text(
        json.dumps(result),
        encoding="utf-8",
    )
    (harness.state_dir / "last-apply.json").write_text(
        json.dumps(result),
        encoding="utf-8",
    )

    completed = harness.run("apply")

    assert completed.returncode == 0
    assert "no pending storage apply request" in completed.stderr
    assert harness.log_lines("events.log") == []
    assert not any((harness.state_dir / "requests").glob("*.json"))


def test_unresolved_storage_claim_resumes_after_config_activation_crash(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    call_id = "a" * 32
    (harness.state_dir / "apply.claim.json").write_text(
        json.dumps(
            {
                "call_id": call_id,
                "operation_id": call_id,
                "fence": 1,
                "claimed_at": 1,
            }
        ),
        encoding="utf-8",
    )

    result = harness.run("apply")

    assert result.returncode == 0, result.stderr
    assert "apply done" in result.stderr
    assert harness.apply_result()["call_id"] == call_id
    assert harness.apply_result()["fence"] == 1
    assert harness.apply_result()["status"] == "ok"
    claim = json.loads(
        (harness.state_dir / "apply.claim.json").read_text(encoding="utf-8")
    )
    assert claim["resume_count"] == 1
    assert harness.log_lines("events.log")
    assert not any((harness.state_dir / "requests").glob("*.json"))


def test_unresolved_storage_claim_resumes_after_services_stopped_crash(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, running_services=())
    call_id = harness.apply_operation_id
    (harness.state_dir / "apply.claim.json").write_text(
        json.dumps(
            {
                "operation_id": call_id,
                "fence": 1,
                "claimed_at": 1,
            }
        ),
        encoding="utf-8",
    )

    result = harness.run("apply")

    assert result.returncode == 0, result.stderr
    assert harness.apply_result()["status"] == "ok"
    assert harness.apply_result()["fence"] == 1
    assert harness.running_services() == set(ALL_SERVICES)
    claim = json.loads(
        (harness.state_dir / "apply.claim.json").read_text(encoding="utf-8")
    )
    assert claim["resume_count"] == 1
    assert not any((harness.state_dir / "requests").glob("*.json"))


def test_stale_storage_request_cannot_overwrite_newer_fence(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    newer_conf = (
        "MODE=smb\n"
        "SMB_HOST=nas.example\n"
        "SMB_SHARE=media\n"
        "SMB_SUBPATH=/images\n"
        "SMB_USERNAME=lumen\n"
        "SMB_PASSWORD=secret\n"
    )
    harness.write_apply_request(newer_conf, fence=2)

    newer = harness.run("apply")

    assert newer.returncode == 0, newer.stderr
    assert harness.apply_result()["fence"] == 2
    assert "MODE=smb" in (harness.state_dir / "storage.conf").read_text(
        encoding="utf-8"
    )

    stale = harness.run("apply")

    assert stale.returncode == 0, stale.stderr
    stale_result = json.loads(
        (harness.state_dir / "results" / f"{harness.apply_operation_id}.1.json")
        .read_text(encoding="utf-8")
    )
    assert stale_result["status"] == "fail"
    assert stale_result["fence"] == 1
    assert "stale storage apply fence" in stale_result["message"]
    active_conf = (harness.state_dir / "storage.conf").read_text(encoding="utf-8")
    assert "MODE=smb" in active_conf
    assert "MODE=local" not in active_conf


def test_storage_startup_units_require_verified_mount_before_docker_and_workers() -> (
    None
):
    mount_unit = (ROOT / "deploy/systemd/lumen-storage-mount.service").read_text(
        encoding="utf-8"
    )
    api_unit = (ROOT / "deploy/systemd/lumen-api.service").read_text(encoding="utf-8")
    worker_unit = (ROOT / "deploy/systemd/lumen-worker.service").read_text(
        encoding="utf-8"
    )

    assert "SuccessExitStatus=" not in mount_unit
    assert "RequiredBy=docker.service" in mount_unit
    assert "Before=docker.service lumen-api.service lumen-worker.service" in mount_unit
    assert "ExecStartPost=/usr/local/sbin/lumen-storage-mount verify" in mount_unit
    for unit in (api_unit, worker_unit):
        assert "Requires=docker.service lumen-storage-mount.service" in unit
        assert "Wants=network-online.target lumen-storage-mount.service" not in unit
        assert "ExecStartPre=/usr/local/sbin/lumen-storage-mount verify" in unit


def test_compose_never_creates_mount_backed_host_paths() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    expected = {
        ("postgres", "/var/lib/postgresql/data"): (
            "${LUMEN_DB_ROOT:-/opt/lumendata}/postgres"
        ),
        ("redis", "/data"): "${LUMEN_DB_ROOT:-/opt/lumendata}/redis",
        ("api", "/opt/lumendata/storage"): (
            "${LUMEN_DATA_ROOT:-/opt/lumendata}/storage"
        ),
        ("api", "/opt/lumendata/backup"): ("${LUMEN_DATA_ROOT:-/opt/lumendata}/backup"),
        ("api", "/var/lib/lumen-storage"): "/var/lib/lumen-storage",
        ("worker", "/opt/lumendata/storage"): (
            "${LUMEN_DATA_ROOT:-/opt/lumendata}/storage"
        ),
    }

    for (service_name, target), source in expected.items():
        volumes = compose["services"][service_name]["volumes"]
        volume = next(
            item
            for item in volumes
            if isinstance(item, dict) and item.get("target") == target
        )
        assert volume["type"] == "bind"
        assert volume["source"] == source
        assert volume["bind"]["create_host_path"] is False


def test_storage_apply_rolls_back_before_reopening_writes(tmp_path: Path) -> None:
    harness = StorageHarness(tmp_path, mode="smb")

    result = harness.run(
        "apply",
        TEST_MOUNT_RC=32,
        TEST_MOUNT_FINAL_STATE="unmounted",
    )

    assert result.returncode == 1
    assert harness.apply_result()["message"] == (
        "new mount failed; restored and verified previous mount"
    )
    assert harness.status()["source"] == "//old.example/archive/images"
    assert harness.running_services() == set(ALL_SERVICES)
    events = harness.log_lines("events.log")
    mount_events = [
        index for index, event in enumerate(events) if event.startswith("mount ")
    ]
    first_start = next(
        index
        for index, event in enumerate(events)
        if event == "docker compose start postgres redis"
    )
    assert len(mount_events) == 2
    assert mount_events[1] < first_start
    restored_conf = (harness.state_dir / "storage.conf").read_text(encoding="utf-8")
    assert "SMB_HOST=old.example" in restored_conf
    assert "SMB_SHARE=archive" in restored_conf


def test_storage_apply_keeps_services_stopped_when_rollback_identity_is_wrong(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)

    result = harness.run(
        "apply",
        TEST_MOUNT_RC=32,
        TEST_MOUNT_FINAL_STATE="unmounted",
        TEST_ROLLBACK_MOUNT_FINAL_STATE="wrong-source",
    )

    assert result.returncode == 1
    assert harness.apply_result()["message"] == (
        "new mount failed; previous mount rollback failed and services remain stopped"
    )
    assert harness.running_services() == set()
    assert "compose start postgres redis" not in harness.log_lines("docker.log")
    assert "previous storage mount could not be restored" in result.stderr
    assert len(harness.log_lines("mount.log")) == 2


def test_storage_apply_refuses_unmount_without_verified_last_good(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path)
    (harness.state_dir / "last-good.conf").unlink()

    result = harness.run("apply")

    assert result.returncode == 1
    assert "rollback identity is not verified" in harness.apply_result()["message"]
    assert "compose stop" not in "\n".join(harness.log_lines("docker.log"))
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []
    assert harness.running_services() == set(ALL_SERVICES)


def test_storage_up_rejects_custom_local_root_without_external_backing_mount(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=(),
    )
    default_local = tmp_path / "default-local"
    default_local.mkdir()

    result = harness.run(
        "up",
        LUMEN_STORAGE_DEFAULT_LOCAL_ROOT=default_local,
        MOCK_LOCAL_MOUNT_TARGET="/",
    )

    assert result.returncode == 1
    assert "without an external backing mount" in result.stderr
    assert harness.log_lines("mount.log") == []
    assert not (harness.state_dir / "last-good.conf").exists()


def test_storage_up_rejects_changed_external_local_backing_identity(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(
        tmp_path,
        initial_mounted=False,
        running_services=(),
    )
    default_local = tmp_path / "default-local"
    default_local.mkdir()
    mounted = harness.run(
        "up",
        LUMEN_STORAGE_DEFAULT_LOCAL_ROOT=default_local,
        MOCK_LOCAL_MOUNT_TARGET="/mnt/external",
    )
    assert mounted.returncode == 0, mounted.stderr

    harness.clear_mount_state()
    remount = harness.run(
        "up",
        LUMEN_STORAGE_DEFAULT_LOCAL_ROOT=default_local,
        MOCK_LOCAL_SOURCE="root-device",
        MOCK_LOCAL_MOUNT_TARGET="/",
        TEST_DOCKER_SYSTEMD_STATE="inactive",
        TEST_DOCKER_PS_RC=1,
    )

    assert remount.returncode == 1
    assert "backing mount identity changed" in remount.stderr
    assert len(harness.log_lines("mount.log")) == 1


def test_storage_verify_rejects_changed_smb_mount_identity(tmp_path: Path) -> None:
    harness = StorageHarness(
        tmp_path,
        mode="smb",
        initial_mounted=False,
        running_services=(),
    )
    mounted = harness.run("up")
    assert mounted.returncode == 0, mounted.stderr

    harness.set_mount_state(
        mount_id="replacement",
        source="//unexpected.example/media",
        fstype="cifs",
        options="rw,vers=3.0",
    )
    verified = harness.run("verify")

    assert verified.returncode == 1
    assert "identity verification failed" in verified.stderr


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


def test_storage_smb_up_ignores_active_unverified_candidate(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, mode="smb")

    result = harness.run("up")

    assert result.returncode == 0, result.stderr
    assert harness.log_lines("umount.log") == []
    assert harness.log_lines("mount.log") == []
    assert harness.running_services() == set(ALL_SERVICES)
    assert "SMB_HOST=old.example" in (harness.state_dir / "storage.conf").read_text(
        encoding="utf-8"
    )


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


def test_storage_apply_without_rollback_baseline_aborts_before_stop(
    tmp_path: Path,
) -> None:
    harness = StorageHarness(tmp_path, initial_mounted=False)

    result = harness.run("apply", TEST_BUSY_AFTER_STOP=1)

    assert result.returncode == 1
    assert "no verified rollback identity" in result.stderr
    assert "compose stop" not in "\n".join(harness.log_lines("docker.log"))
    assert harness.running_services() == set(ALL_SERVICES)


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
