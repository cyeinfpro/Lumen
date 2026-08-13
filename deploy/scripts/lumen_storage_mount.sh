#!/usr/bin/env bash
# Lumen storage mount controller.
# Reads /var/lib/lumen-storage/storage.conf and (un)mounts /opt/lumendata.
# Modes: local (bind mount) and smb (cifs mount). Used by:
#   - lumen-storage-mount.service        boot-time `up`
#   - lumen-storage-apply.service        admin-triggered `apply` (full reload cycle)
#   - lumen-storage-test.service         admin-triggered SMB `test`
# Result/status JSON written under $STATE_DIR for the API to read back.

set -euo pipefail

STATE_DIR="${LUMEN_STORAGE_STATE_DIR:-/var/lib/lumen-storage}"
CONF_FILE="${STATE_DIR}/storage.conf"
LAST_GOOD_CONF_FILE="${STATE_DIR}/last-good.conf"
UNMANAGED_DIRECT_FILE="${STATE_DIR}/unmanaged-direct"
DISABLED_FILE="${STATE_DIR}/disabled"
STATUS_FILE="${STATE_DIR}/status.json"
APPLY_RESULT_FILE="${STATE_DIR}/last-apply.json"
APPLY_CLAIM_FILE="${STATE_DIR}/apply.claim.json"
APPLY_REQUESTS_DIR="${STATE_DIR}/requests"
APPLY_RESULTS_DIR="${STATE_DIR}/results"
TEST_RESULT_FILE="${STATE_DIR}/last-test.json"
TEST_CONF_FILE="${STATE_DIR}/test.conf"
TEST_TRIGGER_FILE="${STATE_DIR}/test.trigger"
TARGET="${LUMEN_STORAGE_TARGET:-/opt/lumendata}"
DATASET_IDENTITY_FILE="${TARGET}/.lumen-storage-dataset-id"
TEST_TARGET="${LUMEN_STORAGE_TEST_TARGET:-${STATE_DIR}/scratch}"
DEFAULT_LOCAL_ROOT="${LUMEN_STORAGE_DEFAULT_LOCAL_ROOT:-/var/lib/lumen-data}"
DEFAULT_ALLOWED_LOCAL_ROOTS="/var/lib/lumen-data:/srv/lumen-data:/mnt:/media"

# CIFS options tuned for Lumen workload (4K large files, forceuid model, EPERM-tolerant).
# vers=3.0 — SMB3 baseline; broadly compatible.
# soft — IO returns ENETUNREACH on disconnect instead of hanging (the kernel
#   handles retries internally; cifs has no NFS-style `retrans` option — adding
#   it triggers `Unknown mount option` and aborts with mount error(22)).
# rsize/wsize=4M — large-block IO friendly (4K image task pattern).
# actimeo=60 — Lumen images are sha256-content-addressed and immutable once
#   stored; attribute cache TTL of 60s avoids per-request stat round-trips to
#   the SMB server (default actimeo=1 was hurting hot-path image reads).
# noperm — client trusts server permissions (matches our chmod EPERM tolerance).
# mfsymlinks / mapposix — symlinks + reserved-char filenames work transparently.
CIFS_OPTS_BASE="vers=3.0,soft,rsize=4194304,wsize=4194304,actimeo=60,cache=strict,echo_interval=60,noperm,mfsymlinks,mapposix,nounix,serverino,_netdev"

LUMEN_DOCKER_COMPOSE_DIR="${LUMEN_DOCKER_COMPOSE_DIR:-/opt/lumen/current}"
LUMEN_DOCKER_SERVICES="${LUMEN_DOCKER_SERVICES:-api worker tgbot web}"

APPLY_REQUEST_FILE=""
APPLY_OPERATION_ID=""
APPLY_FENCE=""
APPLY_CONFIG_SHA256=""

mkdir -p "$STATE_DIR" "$APPLY_REQUESTS_DIR" "$APPLY_RESULTS_DIR"
chmod 0775 "$STATE_DIR" 2>/dev/null || true
chmod 0770 "$APPLY_REQUESTS_DIR" "$APPLY_RESULTS_DIR" 2>/dev/null || true

log() {
  printf '[lumen-storage] %s\n' "$*" >&2
}

log_error() {
  log "$*"
}

storage_maintenance_root() {
  local compose_dir="${LUMEN_DOCKER_COMPOSE_DIR%/}"
  if [[ -n "${LUMEN_STORAGE_MAINTENANCE_ROOT:-}" ]]; then
    printf '%s\n' "$LUMEN_STORAGE_MAINTENANCE_ROOT"
  elif [[ "$compose_dir" == */current ]]; then
    printf '%s\n' "${compose_dir%/current}"
  else
    dirname "$compose_dir"
  fi
}

storage_acquire_maintenance_lock() {
  local candidate="" locking_lib="" maintenance_root=""
  for candidate in \
    "${LUMEN_STORAGE_LOCKING_LIB:-}" \
    "${LUMEN_DOCKER_COMPOSE_DIR%/}/scripts/lib/locking.sh" \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../../scripts/lib/locking.sh" \
    "/opt/lumen/current/scripts/lib/locking.sh"; do
    [[ -n "$candidate" && -f "$candidate" && ! -L "$candidate" ]] || continue
    locking_lib="$candidate"
    break
  done
  if [[ -z "$locking_lib" ]]; then
    log "maintenance lock helper is unavailable"
    return 1
  fi
  # shellcheck source=/dev/null
  . "$locking_lib"
  maintenance_root="$(storage_maintenance_root)" || return 1
  [[ -d "$maintenance_root" && ! -L "$maintenance_root" ]] || {
    log "maintenance root is invalid: $maintenance_root"
    return 1
  }
  lumen_try_acquire_lock "$maintenance_root" "lumen-storage-apply"
}

storage_require_no_active_systemd_fallback_writers() {
  local candidate="" services_lib=""
  for candidate in \
    "${LUMEN_STORAGE_SERVICES_LIB:-}" \
    "${LUMEN_DOCKER_COMPOSE_DIR%/}/scripts/lib/backup_restore_services.sh" \
    "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/../../scripts/lib/backup_restore_services.sh" \
    "/opt/lumen/current/scripts/lib/backup_restore_services.sh"; do
    [[ -n "$candidate" && -f "$candidate" && ! -L "$candidate" ]] || continue
    services_lib="$candidate"
    break
  done
  if [[ -z "$services_lib" ]]; then
    log "systemd fallback writer guard is unavailable"
    return 1
  fi
  # shellcheck source=/dev/null
  . "$services_lib"
  lumen_require_no_active_systemd_fallback_writers
}

kv_value() {
  local file="$1" key="$2"
  python3 - "$file" "$key" <<'PY'
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
target = sys.argv[2]
try:
    lines = path.read_text(encoding="utf-8").splitlines()
except OSError:
    sys.exit(1)
for raw in lines:
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if key.strip() != target:
        continue
    lexer = shlex.shlex(value.strip(), posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        parts = list(lexer)
    except ValueError:
        sys.exit(2)
    print(parts[0] if parts else "")
    sys.exit(0)
sys.exit(1)
PY
}

deploy_env_value() {
  local key="$1" value="" file
  for file in "${LUMEN_DEPLOY_ENV_FILE:-}" /opt/lumen/.env /opt/lumen/shared/.env /opt/lumen/current/.env; do
    [[ -n "$file" && -f "$file" ]] || continue
    value="$(kv_value "$file" "$key" 2>/dev/null || true)"
    if [[ -n "$value" ]]; then
      printf '%s\n' "$value"
      return 0
    fi
  done
  return 1
}

LUMEN_UID="${LUMEN_APP_UID:-$(deploy_env_value LUMEN_APP_UID 2>/dev/null || printf '10001')}"
LUMEN_GID="${LUMEN_APP_GID:-$(deploy_env_value LUMEN_APP_GID 2>/dev/null || printf '10001')}"
LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS="${LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS:-$(deploy_env_value LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS 2>/dev/null || printf '%s' "$DEFAULT_ALLOWED_LOCAL_ROOTS")}:${DEFAULT_LOCAL_ROOT}"
LUMEN_DB_ROOT="${LUMEN_DB_ROOT:-$(deploy_env_value LUMEN_DB_ROOT 2>/dev/null || deploy_env_value LUMEN_DATA_ROOT 2>/dev/null || printf '%s' "$TARGET")}"
chown "$LUMEN_UID:$LUMEN_GID" \
  "$APPLY_REQUESTS_DIR" "$APPLY_RESULTS_DIR" 2>/dev/null || true
chmod 0770 "$APPLY_REQUESTS_DIR" "$APPLY_RESULTS_DIR" 2>/dev/null || true

trigger_call_id() {
  local path="$1" value=""
  [[ -f "$path" ]] || return 1
  IFS= read -r value < "$path" || true
  if [[ ! "$value" =~ ^[0-9a-f]{32}$ ]]; then
    return 2
  fi
  printf '%s\n' "$value"
}

apply_result_path() {
  local call_id="$1" fence="$2"
  printf '%s/%s.%s.json\n' "$APPLY_RESULTS_DIR" "$call_id" "$fence"
}

apply_result_terminal_for_identity() {
  local call_id="$1" fence="$2" result_path=""
  result_path="$(apply_result_path "$call_id" "$fence")"
  [[ -f "$result_path" ]] || return 1
  python3 - "$result_path" "$call_id" "$fence" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(
    0
    if (data.get("operation_id") or data.get("call_id")) == sys.argv[2]
    and data.get("fence") == int(sys.argv[3])
    and data.get("status") in {"ok", "fail"}
    else 1
)
PY
}

select_apply_request() {
  local selected=""
  selected="$(
    python3 - "$APPLY_REQUESTS_DIR" "$APPLY_RESULTS_DIR" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

requests_dir = Path(sys.argv[1])
results_dir = Path(sys.argv[2])
pattern = re.compile(r"^(?P<operation_id>[0-9a-f]{32})\.(?P<fence>[1-9][0-9]*)\.json$")
candidates = []

for path in requests_dir.glob("*.json"):
    match = pattern.fullmatch(path.name)
    if match is None:
        print(f"ignoring invalid storage apply request name: {path}", file=sys.stderr)
        path.unlink(missing_ok=True)
        continue
    operation_id = match.group("operation_id")
    fence = int(match.group("fence"))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        config = data["config"]
        config_sha256 = data["config_sha256"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        print(f"discarding unreadable storage apply request: {path}", file=sys.stderr)
        path.unlink(missing_ok=True)
        continue
    valid = (
        data.get("schema") == 1
        and data.get("operation_id") == operation_id
        and data.get("fence") == fence
        and isinstance(config, str)
        and isinstance(config_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", config_sha256) is not None
        and hashlib.sha256(config.encode("utf-8")).hexdigest() == config_sha256
    )
    if not valid:
        print(f"discarding invalid storage apply request: {path}", file=sys.stderr)
        path.unlink(missing_ok=True)
        continue
    result_path = results_dir / path.name
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        result = None
    if (
        isinstance(result, dict)
        and (result.get("operation_id") or result.get("call_id")) == operation_id
        and result.get("fence") == fence
        and result.get("status") in {"ok", "fail"}
    ):
        path.unlink(missing_ok=True)
        continue
    candidates.append((fence, operation_id, path, config_sha256))

if not candidates:
    raise SystemExit(1)

fence, operation_id, path, config_sha256 = max(candidates)
print(f"{path}\t{operation_id}\t{fence}\t{config_sha256}")
PY
  )" || return 1
  IFS=$'\t' read -r \
    APPLY_REQUEST_FILE APPLY_OPERATION_ID APPLY_FENCE APPLY_CONFIG_SHA256 \
    <<< "$selected"
  [[ -f "$APPLY_REQUEST_FILE" \
    && "$APPLY_OPERATION_ID" =~ ^[0-9a-f]{32}$ \
    && "$APPLY_FENCE" =~ ^[1-9][0-9]*$ \
    && "$APPLY_CONFIG_SHA256" =~ ^[0-9a-f]{64}$ ]]
}

activate_apply_request() {
  python3 - \
    "$APPLY_REQUEST_FILE" "$CONF_FILE" \
    "$APPLY_OPERATION_ID" "$APPLY_FENCE" "$APPLY_CONFIG_SHA256" <<'PY'
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

request_path = Path(sys.argv[1])
conf_path = Path(sys.argv[2])
operation_id = sys.argv[3]
fence = int(sys.argv[4])
expected_sha256 = sys.argv[5]

try:
    data = json.loads(request_path.read_text(encoding="utf-8"))
    config = data["config"]
except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
    raise SystemExit(1)

if not (
    data.get("schema") == 1
    and data.get("operation_id") == operation_id
    and data.get("fence") == fence
    and isinstance(config, str)
    and re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    and data.get("config_sha256") == expected_sha256
    and hashlib.sha256(config.encode("utf-8")).hexdigest() == expected_sha256
):
    raise SystemExit(1)

conf_path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(
    prefix=f".{conf_path.name}.",
    suffix=".tmp",
    dir=conf_path.parent,
    text=True,
)
tmp_path = Path(tmp_name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(config)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, 0o660)
    except OSError:
        pass
    os.replace(tmp_path, conf_path)
    directory_fd = os.open(conf_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    tmp_path.unlink(missing_ok=True)
PY
}

claim_apply_operation() {
  local call_id="$1" fence="$2"
  python3 - \
    "$APPLY_CLAIM_FILE" "$APPLY_RESULTS_DIR" "$call_id" "$fence" <<'PY'
import json
import os
import sys
import time

claim_path, results_dir, call_id, fence_raw = sys.argv[1:]
fence = int(fence_raw)

def terminal(operation_id, operation_fence):
    result_path = os.path.join(
        results_dir,
        f"{operation_id}.{operation_fence}.json",
    )
    try:
        with open(result_path, encoding="utf-8") as handle:
            result = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return (
        (result.get("operation_id") or result.get("call_id")) == operation_id
        and result.get("fence") == operation_fence
        and result.get("status") in {"ok", "fail"}
    )

try:
    with open(claim_path, encoding="utf-8") as handle:
        previous = json.load(handle)
except FileNotFoundError:
    previous = None
except (OSError, UnicodeError, json.JSONDecodeError):
    raise SystemExit(11)

claimed_at = int(time.time())
resume_count = 0
if previous is not None:
    previous_id = previous.get("operation_id") or previous.get("call_id")
    previous_fence = previous.get("fence")
    if (
        not isinstance(previous_id, str)
        or isinstance(previous_fence, bool)
        or not isinstance(previous_fence, int)
        or previous_fence <= 0
    ):
        raise SystemExit(11)
    if previous_fence > fence:
        raise SystemExit(12)
    if previous_fence == fence and previous_id != call_id:
        raise SystemExit(11)
    if (
        previous_fence == fence
        and previous_id == call_id
        and not terminal(previous_id, previous_fence)
    ):
        previous_claimed_at = previous.get("claimed_at")
        if (
            not isinstance(previous_claimed_at, bool)
            and isinstance(previous_claimed_at, int)
            and previous_claimed_at > 0
        ):
            claimed_at = previous_claimed_at
        previous_resume_count = previous.get("resume_count")
        if (
            not isinstance(previous_resume_count, bool)
            and isinstance(previous_resume_count, int)
            and previous_resume_count >= 0
        ):
            resume_count = previous_resume_count + 1
        else:
            resume_count = 1

payload = {
    "call_id": call_id,
    "operation_id": call_id,
    "fence": fence,
    "claimed_at": claimed_at,
    "resumed_at": int(time.time()) if resume_count else None,
    "resume_count": resume_count,
}
tmp_path = f"{claim_path}.{os.getpid()}.tmp"
try:
    with open(tmp_path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, claim_path)
    directory_fd = os.open(os.path.dirname(claim_path) or ".", os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    try:
        os.unlink(tmp_path)
    except FileNotFoundError:
        pass
PY
}

normalized_path() {
  python3 - "$1" <<'PY'
import os
import sys

print(os.path.realpath(sys.argv[1]))
PY
}

path_is_within() {
  local candidate="$1" root="$2" candidate_resolved root_resolved
  candidate_resolved="$(normalized_path "$candidate")" || return 1
  root_resolved="$(normalized_path "$root")" || return 1
  case "$candidate_resolved" in
    "$root_resolved"|"$root_resolved"/*)
      return 0
      ;;
  esac
  return 1
}

local_root_allowed() {
  local candidate="$1" resolved prefix prefix_resolved
  [[ "$candidate" = /* ]] || return 1
  resolved="$(normalized_path "$candidate")" || return 1
  case "$resolved" in
    /|/etc|/usr|/var|/var/lib|/srv|/mnt|/media|/opt|/opt/lumen|/opt/lumendata|"$STATE_DIR"|"$TARGET")
      return 1
      ;;
  esac
  IFS=':' read -r -a allowed_roots <<< "$LUMEN_STORAGE_ALLOWED_LOCAL_ROOTS"
  for prefix in "${allowed_roots[@]}"; do
    [[ "$prefix" = /* ]] || continue
    prefix_resolved="$(normalized_path "$prefix")" || continue
    case "$resolved" in
      "$prefix_resolved"|"$prefix_resolved"/*)
        LOCAL_ROOT="$resolved"
        return 0
        ;;
    esac
  done
  return 1
}

compose_available() {
  [[ -d "$LUMEN_DOCKER_COMPOSE_DIR" ]] \
    && command -v docker >/dev/null 2>&1 \
    && command -v timeout >/dev/null 2>&1
}

validate_compose_services() {
  local service
  for service in "$@"; do
    if [[ ! "$service" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; then
      log "invalid docker compose service name: $service"
      return 1
    fi
  done
}

compose_with_timeout() {
  local timeout_sec="$1"
  shift
  (cd "$LUMEN_DOCKER_COMPOSE_DIR" && timeout "$timeout_sec" docker compose "$@")
}

storage_core_readiness() {
  local timeout_sec="${1:-90}"
  local ready_url="${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}"
  local attempts="${LUMEN_STORAGE_CORE_READINESS_ATTEMPTS:-$timeout_sec}"
  local interval="${LUMEN_STORAGE_CORE_READINESS_INTERVAL_SECONDS:-1}"
  local helper="${LUMEN_DOCKER_COMPOSE_DIR}/scripts/lib.sh"
  case "${attempts}:${interval}" in
    *[!0-9:]*|0:*|0[0-9]*:*)
      log "invalid storage readiness parameters"
      return 2
      ;;
  esac
  if [[ -f "$helper" && ! -L "$helper" ]]; then
    (
      export LUMEN_DEPLOY_ROOT="${LUMEN_DOCKER_COMPOSE_DIR%/current}"
      # shellcheck source=/dev/null
      . "$helper"
      lumen_require_compose_core_readiness \
        "$LUMEN_DOCKER_COMPOSE_DIR" "$ready_url" "$attempts" "$interval"
    )
    return $?
  fi

  command -v curl >/dev/null 2>&1 || return 1
  local poll=0
  for ((poll = 1; poll <= attempts; poll++)); do
    if curl --noproxy '*' -fsS --max-time 5 -o /dev/null \
        "$ready_url" 2>/dev/null \
      && compose_with_timeout "${LUMEN_STORAGE_DOCKER_PROBE_TIMEOUT:-15}" \
        exec -T worker python -m app.worker_health check \
        >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$poll" -lt "$attempts" && "$interval" -gt 0 ]]; then
      sleep "$interval"
    fi
  done
  return 1
}

docker_with_timeout() {
  local timeout_sec="$1"
  shift
  timeout "$timeout_sec" docker "$@"
}

compose_services_stopped() {
  local probe_timeout="${LUMEN_STORAGE_DOCKER_PROBE_TIMEOUT:-15}"
  local running=""
  if ! running="$(compose_with_timeout "$probe_timeout" \
      ps --status running --quiet "$@" 2>/dev/null)"; then
    return 1
  fi
  [[ -z "$running" ]]
}

docker_runtime_confirmed_inactive() {
  local state=""
  command -v systemctl >/dev/null 2>&1 || return 1
  state="$(systemctl is-active docker.service 2>/dev/null || true)"
  case "$state" in
    inactive|failed)
      ;;
    *)
      return 1
      ;;
  esac
  if command -v docker >/dev/null 2>&1 \
    && command -v timeout >/dev/null 2>&1 \
    && docker_with_timeout "${LUMEN_STORAGE_DOCKER_PROBE_TIMEOUT:-15}" \
      ps --quiet >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

running_target_container_ids() {
  local probe_timeout="${LUMEN_STORAGE_DOCKER_PROBE_TIMEOUT:-15}"
  local container_ids="" container_id="" sources="" source="" found=1
  if ! container_ids="$(docker_with_timeout "$probe_timeout" ps --quiet 2>/dev/null)"; then
    return 2
  fi
  for container_id in $container_ids; do
    if ! sources="$(docker_with_timeout "$probe_timeout" inspect \
        --format '{{range .Mounts}}{{println .Source}}{{end}}' \
        "$container_id" 2>/dev/null)"; then
      return 2
    fi
    while IFS= read -r source; do
      [[ -n "$source" ]] || continue
      if path_is_within "$source" "$TARGET"; then
        printf '%s\n' "$container_id"
        found=0
        break
      fi
    done <<< "$sources"
  done
  return "$found"
}

docker_target_containers_stopped() {
  local running="" rc=0
  if running="$(running_target_container_ids)"; then
    log "running Docker containers still use $TARGET: ${running//$'\n'/ }"
    return 1
  else
    rc=$?
  fi
  if [[ "$rc" -eq 1 ]]; then
    return 0
  fi
  log "cannot verify whether running Docker containers use $TARGET"
  return 1
}

proc_target_access_state() {
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'unsupported\n'
    return 0
  fi
  python3 - "$TARGET" "${LUMEN_STORAGE_PROC_ROOT:-/proc}" <<'PY'
import errno
import os
import re
import sys

target = os.path.realpath(sys.argv[1])
proc_root = sys.argv[2]
if not os.path.isdir(target):
    print("unknown")
    raise SystemExit(0)
if not os.path.isdir(proc_root):
    print("unsupported")
    raise SystemExit(0)

uncertain = False
octal_escape = re.compile(r"\\([0-7]{3})")


def decoded_path(value: str) -> str:
    value = octal_escape.sub(lambda match: chr(int(match.group(1), 8)), value)
    if value.endswith(" (deleted)"):
        value = value[: -len(" (deleted)")]
    return value


def within_target(value: str) -> bool:
    value = decoded_path(value)
    if not value.startswith("/"):
        return False
    candidate = os.path.realpath(value)
    try:
        return os.path.commonpath((candidate, target)) == target
    except ValueError:
        return False


def readlink(path):
    global uncertain
    try:
        return os.readlink(path)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ESRCH, errno.EINVAL):
            return None
        if exc.errno in (errno.EACCES, errno.EPERM):
            uncertain = True
            return None
        uncertain = True
        return None


for entry in os.scandir(proc_root):
    if not entry.name.isdigit() or not entry.is_dir(follow_symlinks=False):
        continue
    process_dir = entry.path
    for link_name in ("cwd", "root"):
        value = readlink(os.path.join(process_dir, link_name))
        if value is not None and within_target(value):
            print("active")
            raise SystemExit(0)

    fd_dir = os.path.join(process_dir, "fd")
    try:
        fd_names = os.listdir(fd_dir)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ESRCH):
            uncertain = True
        fd_names = ()
    for fd_name in fd_names:
        value = readlink(os.path.join(fd_dir, fd_name))
        if value is not None and within_target(value):
            print("active")
            raise SystemExit(0)

    maps_path = os.path.join(process_dir, "maps")
    try:
        with open(maps_path, encoding="utf-8", errors="replace") as maps:
            for line in maps:
                fields = line.rstrip("\n").split(None, 5)
                if len(fields) == 6 and within_target(fields[5]):
                    print("active")
                    raise SystemExit(0)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ESRCH):
            uncertain = True

print("unknown" if uncertain else "idle")
PY
}

lsof_target_access_state() {
  local users="" rc=0
  local error_file="${STATE_DIR}/.target-lsof.$$.err"
  if ! command -v lsof >/dev/null 2>&1; then
    log "cannot inspect $TARGET process references: /proc unavailable and lsof missing"
    printf 'unknown\n'
    return 0
  fi
  if ! (umask 077; : > "$error_file"); then
    log "cannot create lsof diagnostic file for $TARGET"
    printf 'unknown\n'
    return 0
  fi
  if users="$(lsof -nP -t +D "$TARGET" 2>"$error_file")"; then
    rc=0
  else
    rc=$?
  fi
  if [[ -n "${users//[[:space:]]/}" ]]; then
    rm -f "$error_file"
    printf 'active\n'
    return 0
  fi
  if [[ "$rc" -le 1 && ! -s "$error_file" ]]; then
    rm -f "$error_file"
    printf 'idle\n'
    return 0
  fi
  log "lsof could not verify process references under $TARGET (rc=$rc)"
  rm -f "$error_file"
  printf 'unknown\n'
}

target_access_state() {
  local proc_state=""
  proc_state="$(proc_target_access_state 2>/dev/null || printf 'unknown\n')"
  case "$proc_state" in
    active|idle)
      printf '%s\n' "$proc_state"
      return 0
      ;;
    unsupported)
      lsof_target_access_state
      return 0
      ;;
    *)
      log "cannot inspect all process cwd/root/fd/mmap references under $TARGET"
      printf 'unknown\n'
      return 0
      ;;
  esac
}

storage_transition_safe() {
  local allow_inactive_runtime="${1:-0}"
  shift || true
  local access_state=""

  if [[ "$allow_inactive_runtime" -eq 1 ]] && docker_runtime_confirmed_inactive; then
    log "Docker runtime is inactive; no running containers can use $TARGET"
  else
    if ! compose_available; then
      log "cannot verify declared services: docker compose is unavailable"
      return 1
    fi
    if ! compose_services_stopped "$@"; then
      log "declared Docker services are still running or could not be verified: $*"
      return 1
    fi
    if ! docker_target_containers_stopped; then
      return 1
    fi
  fi

  access_state="$(target_access_state)"
  case "$access_state" in
    idle)
      return 0
      ;;
    active)
      log "target $TARGET is still busy after Docker services stopped"
      return 1
      ;;
    *)
      log "cannot verify that target $TARGET is idle"
      return 1
      ;;
  esac
}

findmnt_value() {
  local path="$1" field="$2" value=""
  if ! value="$(findmnt -T "$path" -no "$field" 2>/dev/null)"; then
    return 1
  fi
  [[ -n "$value" ]] || return 1
  printf '%s\n' "$value"
}

path_identity() {
  python3 - "$1" <<'PY'
import os
import sys

try:
    stat = os.stat(sys.argv[1])
except OSError:
    sys.exit(1)
print(f"{stat.st_dev}:{stat.st_ino}")
PY
}

dataset_identity_value() {
  local mode="$1"
  python3 - "$DATASET_IDENTITY_FILE" "$mode" <<'PY'
import errno
import os
import re
import secrets
import stat
import sys

path = os.path.abspath(sys.argv[1])
mode = sys.argv[2]
pattern = re.compile(r"[0-9a-f]{64}")


def read_identity() -> str:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SystemExit("dataset identity is not a regular file")
    with open(path, "r", encoding="ascii") as stream:
        value = stream.read(128).strip()
    if not pattern.fullmatch(value):
        raise SystemExit("dataset identity is invalid")
    return value


if mode == "read":
    try:
        print(read_identity())
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"cannot read dataset identity: {exc}")
    raise SystemExit(0)
if mode != "ensure":
    raise SystemExit("invalid dataset identity mode")

try:
    print(read_identity())
    raise SystemExit(0)
except FileNotFoundError:
    pass
except (OSError, UnicodeError) as exc:
    raise SystemExit(f"cannot read dataset identity: {exc}")

value = secrets.token_hex(32)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
flags |= getattr(os, "O_NOFOLLOW", 0)
try:
    fd = os.open(path, flags, 0o640)
except FileExistsError:
    print(read_identity())
    raise SystemExit(0)
try:
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(value + "\n")
        stream.flush()
        os.fchmod(stream.fileno(), 0o640)
        os.fsync(stream.fileno())
except BaseException:
    try:
        os.unlink(path)
    except OSError:
        pass
    raise

directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
directory_fd = os.open(os.path.dirname(path), directory_flags)
try:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        if exc.errno not in {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            raise
finally:
    os.close(directory_fd)
print(value)
PY
}

write_kv_file() {
  local path="$1" mode="$2"
  shift 2
  if (( $# % 2 != 0 )); then
    return 2
  fi
  {
    while [[ "$#" -gt 0 ]]; do
      printf '%s\0%s\0' "$1" "$2"
      shift 2
    done
  } | python3 -c '
import os
import shlex
import sys
import tempfile

path = sys.argv[1]
mode = int(sys.argv[2], 8)
raw_fields = sys.stdin.buffer.read().split(b"\0")
if raw_fields and raw_fields[-1] == b"":
    raw_fields.pop()
fields = [value.decode("utf-8") for value in raw_fields]
if len(fields) % 2:
    raise SystemExit(2)
fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for index in range(0, len(fields), 2):
            stream.write(f"{fields[index]}={shlex.quote(fields[index + 1])}\n")
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), mode)
    os.replace(tmp, path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(os.path.dirname(path), directory_flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
' "$path" "$mode"
}

write_effective_conf_file() {
  local path="$1" mode="$2"
  write_kv_file "$path" "$mode" \
    MODE "$MODE" \
    LOCAL_ROOT "$LOCAL_ROOT" \
    SMB_HOST "$SMB_HOST" \
    SMB_PORT "${SMB_PORT:-}" \
    SMB_SHARE "$SMB_SHARE" \
    SMB_SUBPATH "$SMB_SUBPATH" \
    SMB_USERNAME "$SMB_USERNAME" \
    SMB_PASSWORD "$SMB_PASSWORD"
}

remove_unmanaged_direct_marker() {
  python3 - "$UNMANAGED_DIRECT_FILE" <<'PY'
import ctypes
import errno
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    info = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if stat.S_ISDIR(info.st_mode):
    raise SystemExit("unmanaged direct marker is unexpectedly a directory")
path.unlink()
directory_fd = os.open(
    path.parent,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
)
try:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }
        if exc.errno not in unsupported:
            raise
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            syncfs = libc.syncfs
        except (AttributeError, OSError) as syncfs_error:
            raise OSError(
                errno.ENOTSUP,
                "syncfs is unavailable for marker durability",
            ) from syncfs_error
        syncfs.argtypes = [ctypes.c_int]
        syncfs.restype = ctypes.c_int
        if syncfs(directory_fd) != 0:
            code = ctypes.get_errno() or errno.EIO
            raise OSError(code, os.strerror(code))
finally:
    os.close(directory_fd)
PY
}

config_file_matches_loaded() {
  local file="$1"
  local mode="" local_root="" smb_host="" smb_port="" smb_share=""
  local smb_subpath="" smb_username="" smb_password=""
  [[ -f "$file" ]] || return 1
  mode="$(kv_value "$file" MODE 2>/dev/null || true)"
  local_root="$(kv_value "$file" LOCAL_ROOT 2>/dev/null || true)"
  smb_host="$(kv_value "$file" SMB_HOST 2>/dev/null || true)"
  smb_port="$(kv_value "$file" SMB_PORT 2>/dev/null || true)"
  smb_share="$(kv_value "$file" SMB_SHARE 2>/dev/null || true)"
  smb_subpath="$(kv_value "$file" SMB_SUBPATH 2>/dev/null || true)"
  smb_username="$(kv_value "$file" SMB_USERNAME 2>/dev/null || true)"
  smb_password="$(kv_value "$file" SMB_PASSWORD 2>/dev/null || true)"
  [[ "$mode" == "$MODE" \
    && "$local_root" == "$LOCAL_ROOT" \
    && "$smb_host" == "$SMB_HOST" \
    && "$smb_port" == "${SMB_PORT:-}" \
    && "$smb_share" == "$SMB_SHARE" \
    && "${smb_subpath:-/}" == "$SMB_SUBPATH" \
    && "$smb_username" == "$SMB_USERNAME" \
    && "$smb_password" == "$SMB_PASSWORD" ]]
}

verified_mountpoint_identity() {
  local actual_target="" actual_id="" actual_resolved="" expected_resolved=""
  mountpoint -q "$TARGET" 2>/dev/null || return 1
  actual_target="$(findmnt_value "$TARGET" TARGET)" || return 1
  actual_id="$(findmnt_value "$TARGET" ID)" || return 1
  actual_resolved="$(normalized_path "$actual_target")" || return 1
  expected_resolved="$(normalized_path "$TARGET")" || return 1
  [[ -n "$actual_id" && "$actual_resolved" == "$expected_resolved" ]]
}

LOCAL_BACKING_ID=""
LOCAL_BACKING_TARGET=""
LOCAL_BACKING_SOURCE=""
LOCAL_BACKING_FSTYPE=""

capture_local_backing_identity() {
  LOCAL_BACKING_ID=""
  LOCAL_BACKING_TARGET=""
  LOCAL_BACKING_SOURCE=""
  LOCAL_BACKING_FSTYPE=""
  [[ -e "$LOCAL_ROOT" ]] || return 1
  LOCAL_BACKING_ID="$(findmnt_value "$LOCAL_ROOT" ID)" || return 1
  LOCAL_BACKING_TARGET="$(findmnt_value "$LOCAL_ROOT" TARGET)" || return 1
  LOCAL_BACKING_SOURCE="$(findmnt_value "$LOCAL_ROOT" SOURCE)" || return 1
  LOCAL_BACKING_FSTYPE="$(findmnt_value "$LOCAL_ROOT" FSTYPE)" || return 1
}

recorded_local_backing_matches() {
  local file="$1"
  local expected_target="" expected_source="" expected_fstype=""
  expected_target="$(kv_value "$file" LOCAL_BACKING_TARGET 2>/dev/null || true)"
  expected_source="$(kv_value "$file" LOCAL_BACKING_SOURCE 2>/dev/null || true)"
  expected_fstype="$(kv_value "$file" LOCAL_BACKING_FSTYPE 2>/dev/null || true)"
  [[ -n "$expected_target" && -n "$expected_source" && -n "$expected_fstype" ]] \
    || return 1
  capture_local_backing_identity || return 1
  [[ "$LOCAL_BACKING_TARGET" == "$expected_target" \
    && "$LOCAL_BACKING_SOURCE" == "$expected_source" \
    && "$LOCAL_BACKING_FSTYPE" == "$expected_fstype" ]]
}

local_root_backing_ready() {
  local backing_target_resolved=""
  if ! capture_local_backing_identity; then
    log "local mount verification failed: cannot identify backing mount for $LOCAL_ROOT"
    return 1
  fi
  if config_file_matches_loaded "$LAST_GOOD_CONF_FILE"; then
    if ! recorded_local_backing_matches "$LAST_GOOD_CONF_FILE"; then
      log "local mount verification failed: backing mount identity changed for $LOCAL_ROOT"
      return 1
    fi
    return 0
  fi
  if path_is_within "$LOCAL_ROOT" "$DEFAULT_LOCAL_ROOT"; then
    return 0
  fi
  backing_target_resolved="$(normalized_path "$LOCAL_BACKING_TARGET")" || return 1
  if [[ "$backing_target_resolved" == "/" ]]; then
    log "refusing custom local root without an external backing mount: $LOCAL_ROOT"
    return 1
  fi
  return 0
}

recorded_mount_transport_identity_valid() {
  local file="$1"
  local expected_target="" expected_source="" expected_fstype=""
  local actual_target="" actual_source="" actual_fstype=""
  expected_target="$(kv_value "$file" MOUNT_TARGET 2>/dev/null || true)"
  expected_source="$(kv_value "$file" MOUNT_SOURCE 2>/dev/null || true)"
  expected_fstype="$(kv_value "$file" MOUNT_FSTYPE 2>/dev/null || true)"
  [[ -n "$expected_target" && -n "$expected_source" && -n "$expected_fstype" ]] \
    || return 1
  verified_mountpoint_identity || return 1
  actual_target="$(findmnt_value "$TARGET" TARGET)" || return 1
  actual_source="$(findmnt_value "$TARGET" SOURCE)" || return 1
  actual_fstype="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  [[ "$actual_target" == "$expected_target" \
    && "$actual_source" == "$expected_source" \
    && "$actual_fstype" == "$expected_fstype" ]] || return 1
  if [[ "$MODE" == "local" ]]; then
    recorded_local_backing_matches "$file"
  fi
}

recorded_mount_identity_valid() {
  local file="$1"
  local expected_dataset_identity="" actual_dataset_identity=""
  recorded_mount_transport_identity_valid "$file" || return 1
  expected_dataset_identity="$(
    kv_value "$file" DATASET_IDENTITY 2>/dev/null || true
  )"
  [[ "$expected_dataset_identity" =~ ^[0-9a-f]{64}$ ]] || return 1
  actual_dataset_identity="$(dataset_identity_value read)" || return 1
  [[ "$actual_dataset_identity" == "$expected_dataset_identity" ]]
}

persist_last_good_mount() {
  local mount_target="" mount_source="" mount_fstype=""
  local backing_target="" backing_source="" backing_fstype=""
  local dataset_identity=""
  configured_mount_valid || return 1
  mount_target="$(findmnt_value "$TARGET" TARGET)" || return 1
  mount_source="$(findmnt_value "$TARGET" SOURCE)" || return 1
  mount_fstype="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  if [[ "$MODE" == "local" ]]; then
    capture_local_backing_identity || return 1
    backing_target="$LOCAL_BACKING_TARGET"
    backing_source="$LOCAL_BACKING_SOURCE"
    backing_fstype="$LOCAL_BACKING_FSTYPE"
  fi
  dataset_identity="$(dataset_identity_value ensure)" || return 1
  [[ "$dataset_identity" =~ ^[0-9a-f]{64}$ ]] || return 1
  chgrp "$LUMEN_GID" "$DATASET_IDENTITY_FILE" 2>/dev/null || true
  write_kv_file "$LAST_GOOD_CONF_FILE" 0640 \
    MODE "$MODE" \
    LOCAL_ROOT "$LOCAL_ROOT" \
    SMB_HOST "$SMB_HOST" \
    SMB_PORT "${SMB_PORT:-}" \
    SMB_SHARE "$SMB_SHARE" \
    SMB_SUBPATH "$SMB_SUBPATH" \
    SMB_USERNAME "$SMB_USERNAME" \
    SMB_PASSWORD "$SMB_PASSWORD" \
    MOUNT_TARGET "$mount_target" \
    MOUNT_SOURCE "$mount_source" \
    MOUNT_FSTYPE "$mount_fstype" \
    DATASET_IDENTITY "$dataset_identity" \
    LOCAL_BACKING_TARGET "$backing_target" \
    LOCAL_BACKING_SOURCE "$backing_source" \
    LOCAL_BACKING_FSTYPE "$backing_fstype" || return 1
  chgrp "$LUMEN_GID" "$LAST_GOOD_CONF_FILE" 2>/dev/null || true
  remove_unmanaged_direct_marker || return 1
}

restore_conf_from_last_good() {
  load_conf_file "$LAST_GOOD_CONF_FILE" || return 1
  write_effective_conf_file "$CONF_FILE" 0660 || return 1
  chgrp "$LUMEN_GID" "$CONF_FILE" 2>/dev/null || true
}

CAPTURED_MOUNT_PRESENT=0
CAPTURED_MOUNT_ID=""
CAPTURED_MOUNT_SOURCE=""
CAPTURED_MOUNT_FSTYPE=""

capture_mount_snapshot() {
  CAPTURED_MOUNT_PRESENT=0
  CAPTURED_MOUNT_ID=""
  CAPTURED_MOUNT_SOURCE=""
  CAPTURED_MOUNT_FSTYPE=""
  if ! mountpoint -q "$TARGET" 2>/dev/null; then
    return 0
  fi
  CAPTURED_MOUNT_ID="$(findmnt_value "$TARGET" ID)" || return 1
  CAPTURED_MOUNT_SOURCE="$(findmnt_value "$TARGET" SOURCE)" || return 1
  CAPTURED_MOUNT_FSTYPE="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  CAPTURED_MOUNT_PRESENT=1
}

mount_snapshot_still_valid() {
  local present="$1" mount_id="$2" source="$3" fstype="$4"
  local current_id="" current_source="" current_fstype=""
  if [[ "$present" -eq 0 ]]; then
    if mountpoint -q "$TARGET" 2>/dev/null; then
      return 1
    fi
    return 0
  fi
  mountpoint -q "$TARGET" 2>/dev/null || return 1
  current_id="$(findmnt_value "$TARGET" ID)" || return 1
  current_source="$(findmnt_value "$TARGET" SOURCE)" || return 1
  current_fstype="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  [[ "$current_id" == "$mount_id" \
    && "$current_source" == "$source" \
    && "$current_fstype" == "$fstype" ]]
}

json_str() {
  # Robust JSON string escaping. Prefer jq if available; fall back to python.
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$1" | jq -Rs .
    return
  fi
  python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))' <<<"$1"
}

write_status() {
  local mode="" source="" fstype="" mounted=false disabled=false
  if mountpoint -q "$TARGET" 2>/dev/null; then
    mounted=true
  fi
  source="$(findmnt -T "$TARGET" -no SOURCE 2>/dev/null || true)"
  fstype="$(findmnt -T "$TARGET" -no FSTYPE 2>/dev/null || true)"
  if [[ -f "$CONF_FILE" ]]; then
    mode="$(kv_value "$CONF_FILE" MODE 2>/dev/null || true)"
  fi
  [[ -f "$DISABLED_FILE" ]] && disabled=true
  local now
  now=$(date -u +%s)
  {
    printf '{\n'
    printf '  "mode": %s,\n' "$(json_str "$mode")"
    printf '  "mounted": %s,\n' "$mounted"
    printf '  "source": %s,\n' "$(json_str "$source")"
    printf '  "fstype": %s,\n' "$(json_str "$fstype")"
    printf '  "disabled": %s,\n' "$disabled"
    printf '  "target": %s,\n' "$(json_str "$TARGET")"
    printf '  "updated_at": %s\n' "$now"
    printf '}\n'
  } > "${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
  chmod 0644 "$STATUS_FILE" 2>/dev/null || true
}

write_apply_result() {
  local call_id="$1" status="$2" message="$3" started_at="$4"
  local now result_path=""
  now=$(date -u +%s)
  if [[ "$call_id" != "$APPLY_OPERATION_ID" \
    || ! "$APPLY_FENCE" =~ ^[1-9][0-9]*$ ]]; then
    log "refusing storage result without a valid operation identity and fence"
    return 1
  fi
  result_path="$(apply_result_path "$call_id" "$APPLY_FENCE")"
  python3 - \
    "$result_path" "$APPLY_RESULT_FILE" \
    "$call_id" "$APPLY_FENCE" "$status" "$message" "$started_at" "$now" <<'PY'
import errno
import json
import os
import sys
import tempfile
from pathlib import Path

(
    result_raw,
    latest_raw,
    operation_id,
    fence_raw,
    status,
    message,
    started_raw,
    finished_raw,
) = sys.argv[1:]
result_path = Path(result_raw)
latest_path = Path(latest_raw)
fence = int(fence_raw)
payload = {
    "call_id": operation_id,
    "operation_id": operation_id,
    "fence": fence,
    "status": status,
    "message": message,
    "started_at": int(started_raw),
    "finished_at": int(finished_raw),
}
if status not in {"ok", "fail"}:
    raise SystemExit(2)

result_path.parent.mkdir(parents=True, exist_ok=True)
fd, tmp_name = tempfile.mkstemp(
    prefix=f".{result_path.name}.",
    suffix=".tmp",
    dir=result_path.parent,
    text=True,
)
tmp_path = Path(tmp_name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp_path, 0o644)
    except OSError:
        pass
    try:
        os.link(tmp_path, result_path)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        try:
            existing = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SystemExit(3) from exc
        if not (
            (existing.get("operation_id") or existing.get("call_id"))
            == operation_id
            and existing.get("fence") == fence
            and existing.get("status") == status
        ):
            raise SystemExit(3) from exc
    directory_fd = os.open(result_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    tmp_path.unlink(missing_ok=True)

replace_latest = True
try:
    current = json.loads(latest_path.read_text(encoding="utf-8"))
except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
    current = None
if isinstance(current, dict):
    current_fence = current.get("fence")
    if (
        not isinstance(current_fence, bool)
        and isinstance(current_fence, int)
        and current_fence > fence
    ):
        replace_latest = False
if replace_latest:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, latest_tmp_name = tempfile.mkstemp(
        prefix=f".{latest_path.name}.",
        suffix=".tmp",
        dir=latest_path.parent,
        text=True,
    )
    latest_tmp = Path(latest_tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(latest_tmp, 0o644)
        except OSError:
            pass
        os.replace(latest_tmp, latest_path)
        directory_fd = os.open(latest_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        latest_tmp.unlink(missing_ok=True)
PY
  rm -f -- "$APPLY_REQUEST_FILE"
}

write_test_result() {
  local call_id="$1" status="$2" message="$3"
  local now
  now=$(date -u +%s)
  {
    printf '{\n'
    printf '  "call_id": %s,\n' "$(json_str "$call_id")"
    printf '  "status": %s,\n' "$(json_str "$status")"
    printf '  "message": %s,\n' "$(json_str "$message")"
    printf '  "tested_at": %s\n' "$now"
    printf '}\n'
  } > "${TEST_RESULT_FILE}.tmp"
  mv "${TEST_RESULT_FILE}.tmp" "$TEST_RESULT_FILE"
  chmod 0644 "$TEST_RESULT_FILE" 2>/dev/null || true
}

# Load a concrete config file into MODE/LOCAL_ROOT/SMB_*.
load_conf_file() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  MODE="$(kv_value "$file" MODE 2>/dev/null || true)"
  MODE="${MODE:-local}"
  LOCAL_ROOT="$(kv_value "$file" LOCAL_ROOT 2>/dev/null || true)"
  LOCAL_ROOT="${LOCAL_ROOT:-$DEFAULT_LOCAL_ROOT}"
  SMB_HOST="$(kv_value "$file" SMB_HOST 2>/dev/null || true)"
  # 空 → 走 mount.cifs 默认 445；其他值（数字字符串）拼到 -o port=
  SMB_PORT="$(kv_value "$file" SMB_PORT 2>/dev/null || true)"
  SMB_SHARE="$(kv_value "$file" SMB_SHARE 2>/dev/null || true)"
  SMB_SUBPATH="$(kv_value "$file" SMB_SUBPATH 2>/dev/null || true)"
  SMB_SUBPATH="${SMB_SUBPATH:-/}"
  SMB_USERNAME="$(kv_value "$file" SMB_USERNAME 2>/dev/null || true)"
  SMB_PASSWORD="$(kv_value "$file" SMB_PASSWORD 2>/dev/null || true)"
}

# Load effective config into MODE/LOCAL_ROOT/SMB_*.
# escape hatch: when DISABLED_FILE exists, force local mode on default root.
load_conf() {
  if [[ -f "$DISABLED_FILE" ]]; then
    MODE="local"
    LOCAL_ROOT="$DEFAULT_LOCAL_ROOT"
    SMB_HOST=""; SMB_PORT=""; SMB_SHARE=""; SMB_SUBPATH="/"; SMB_USERNAME=""; SMB_PASSWORD=""
    log "DISABLED_FILE present, forcing local mode on $DEFAULT_LOCAL_ROOT"
    return 0
  fi
  if [[ ! -f "$CONF_FILE" ]]; then
    MODE="local"
    LOCAL_ROOT="$DEFAULT_LOCAL_ROOT"
    SMB_HOST=""; SMB_PORT=""; SMB_SHARE=""; SMB_SUBPATH="/"; SMB_USERNAME=""; SMB_PASSWORD=""
    return 0
  fi
  load_conf_file "$CONF_FILE"
}

build_smb_source() {
  local host="$1" share="$2" subpath="$3"
  subpath="${subpath#/}"
  subpath="${subpath%/}"
  if [[ -n "$subpath" ]]; then
    printf '//%s/%s/%s' "$host" "$share" "$subpath"
  else
    printf '//%s/%s' "$host" "$share"
  fi
}

write_smb_credentials() {
  local user="$1" pass="$2" out="$3"
  install -m 0600 /dev/null "$out"
  cat > "$out" <<EOF
username=${user}
password=${pass}
EOF
}

verify_local_mount() {
  local target_source="" target_source_base="" target_fstype=""
  local local_source="" local_source_base="" local_fstype=""
  local target_identity="" local_identity="" target_mount_id=""
  if ! verified_mountpoint_identity; then
    log "local mount verification failed: $TARGET is not the expected mountpoint"
    return 1
  fi
  if ! local_root_backing_ready; then
    return 1
  fi
  target_mount_id="$(findmnt_value "$TARGET" ID)" || return 1
  target_source="$(findmnt_value "$TARGET" SOURCE)" || return 1
  target_fstype="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  local_source="$(findmnt_value "$LOCAL_ROOT" SOURCE)" || return 1
  local_fstype="$(findmnt_value "$LOCAL_ROOT" FSTYPE)" || return 1
  target_identity="$(path_identity "$TARGET")" || return 1
  local_identity="$(path_identity "$LOCAL_ROOT")" || return 1
  target_source_base="${target_source%%\[*}"
  local_source_base="${local_source%%\[*}"
  if [[ "$target_source_base" != "$local_source_base" \
    || "$target_fstype" != "$local_fstype" \
    || "$target_mount_id" == "$LOCAL_BACKING_ID" \
    || "$target_identity" != "$local_identity" ]]; then
    log "local mount verification failed: expected source=$LOCAL_ROOT fstype=$local_fstype, got source=$target_source fstype=$target_fstype"
    return 1
  fi
  return 0
}

smb_source_matches() {
  local actual="${1%/}" expected="${2%/}" options="$3"
  local base="//${SMB_HOST}/${SMB_SHARE}" subpath="${SMB_SUBPATH#/}"
  subpath="${subpath%/}"
  if [[ "$actual" == "$expected" ]]; then
    return 0
  fi
  if [[ -n "$subpath" && "$actual" == "${base}[/${subpath}]" ]]; then
    return 0
  fi
  if [[ -n "$subpath" && "$actual" == "$base" \
    && ",$options," == *",prefixpath=${subpath},"* ]]; then
    return 0
  fi
  return 1
}

verify_smb_mount() {
  local expected_source="$1" actual_source="" actual_fstype="" options=""
  if ! verified_mountpoint_identity; then
    log "SMB mount verification failed: $TARGET is not the expected mountpoint"
    return 1
  fi
  actual_source="$(findmnt_value "$TARGET" SOURCE)" || return 1
  actual_fstype="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  options="$(findmnt_value "$TARGET" OPTIONS 2>/dev/null || true)"
  if [[ "$actual_fstype" != "cifs" ]] \
    || ! smb_source_matches "$actual_source" "$expected_source" "$options"; then
    log "SMB mount verification failed: expected source=$expected_source fstype=cifs, got source=$actual_source fstype=$actual_fstype"
    return 1
  fi
  return 0
}

configured_mount_valid() {
  case "$MODE" in
    local)
      verify_local_mount
      ;;
    smb)
      verify_smb_mount "$(build_smb_source "$SMB_HOST" "$SMB_SHARE" "$SMB_SUBPATH")"
      ;;
    *)
      return 1
      ;;
  esac
}

mount_local() {
  if ! local_root_allowed "$LOCAL_ROOT"; then
    log "refusing unsafe local root: $LOCAL_ROOT"
    return 2
  fi
  if path_is_within "$LOCAL_ROOT" "$DEFAULT_LOCAL_ROOT"; then
    mkdir -p "$LOCAL_ROOT"
  else
    if [[ ! -d "$LOCAL_ROOT" ]]; then
      log "refusing missing custom local root: $LOCAL_ROOT"
      return 1
    fi
    local_root_backing_ready || return 1
  fi
  chown "$LUMEN_UID:$LUMEN_GID" "$LOCAL_ROOT" 2>/dev/null || true
  chmod 0775 "$LOCAL_ROOT" 2>/dev/null || true
  mkdir -p "$TARGET"
  if mountpoint -q "$TARGET"; then
    if verify_local_mount; then
      log "target $TARGET already has the expected local bind mount"
      return 0
    fi
    log "refusing to replace an existing non-matching mount during ordinary local up; use apply"
    return 1
  fi
  local rc=0
  mount --bind "$LOCAL_ROOT" "$TARGET" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    return "$rc"
  fi
  if ! verify_local_mount; then
    return 1
  fi
  log "bind $LOCAL_ROOT -> $TARGET OK"
}

mount_smb() {
  if [[ -z "$SMB_HOST" || -z "$SMB_SHARE" || -z "$SMB_USERNAME" || -z "$SMB_PASSWORD" ]]; then
    log "smb config incomplete (host/share/username/password)"
    return 1
  fi
  local source cred opts rc=0
  source="$(build_smb_source "$SMB_HOST" "$SMB_SHARE" "$SMB_SUBPATH")"
  mkdir -p "$TARGET"
  if mountpoint -q "$TARGET"; then
    if verify_smb_mount "$source"; then
      log "target $TARGET already has the expected CIFS mount"
      return 0
    fi
    log "refusing to replace an existing non-matching mount during ordinary SMB up; use apply"
    return 1
  fi
  cred="$(mktemp /run/lumen-smb-cred.XXXXXX)"
  # shellcheck disable=SC2064
  trap "rm -f '$cred'" RETURN EXIT
  write_smb_credentials "$SMB_USERNAME" "$SMB_PASSWORD" "$cred"
  opts="credentials=${cred},uid=${LUMEN_UID},gid=${LUMEN_GID},forceuid,forcegid,file_mode=0664,dir_mode=0775,${CIFS_OPTS_BASE}"
  if [[ -n "$SMB_PORT" ]]; then
    opts="${opts},port=${SMB_PORT}"
  fi
  if mount -t cifs "$source" "$TARGET" -o "$opts"; then
    rc=0
  else
    rc=$?
  fi
  rm -f "$cred"
  trap - RETURN
  trap - EXIT
  if [[ "$rc" -ne 0 ]]; then
    return "$rc"
  fi
  if ! verify_smb_mount "$source"; then
    return 1
  fi
  log "cifs $source -> $TARGET OK"
}

umount_target_force() {
  local regular_rc=0 lazy_rc=0
  if ! mountpoint -q "$TARGET"; then
    return 0
  fi
  umount "$TARGET" 2>/dev/null || regular_rc=$?
  if ! mountpoint -q "$TARGET"; then
    return 0
  fi
  log "regular umount left $TARGET mounted (rc=$regular_rc); trying lazy umount"
  umount -l "$TARGET" 2>/dev/null || lazy_rc=$?
  if ! mountpoint -q "$TARGET"; then
    return 0
  fi
  log "target $TARGET is still mounted after lazy umount (regular_rc=$regular_rc lazy_rc=$lazy_rc)"
  return 1
}

APP_SERVICES=()
STOP_SERVICES=()
DB_MOVES_WITH_TARGET=0

prepare_service_scope() {
  APP_SERVICES=()
  STOP_SERVICES=()
  DB_MOVES_WITH_TARGET=0
  read -r -a APP_SERVICES <<< "$LUMEN_DOCKER_SERVICES"
  if [[ "${#APP_SERVICES[@]}" -eq 0 ]]; then
    log "docker compose service list is empty"
    return 1
  fi
  if ! validate_compose_services "${APP_SERVICES[@]}"; then
    return 1
  fi
  STOP_SERVICES=("${APP_SERVICES[@]}")
  if path_is_within "$LUMEN_DB_ROOT" "$TARGET"; then
    DB_MOVES_WITH_TARGET=1
    STOP_SERVICES+=("postgres" "redis")
  fi
}

mount_configured() {
  local rc=0
  case "$MODE" in
    local) mount_local || rc=$? ;;
    smb)   mount_smb || rc=$? ;;
    *)     log "unknown mode: $MODE"; write_status; return 2 ;;
  esac
  write_status
  return "$rc"
}

unmanaged_direct_marker_valid() {
  python3 - "$UNMANAGED_DIRECT_FILE" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
try:
    info = os.lstat(path)
except OSError:
    raise SystemExit(1)
if (
    not stat.S_ISREG(info.st_mode)
    or stat.S_ISLNK(info.st_mode)
    or info.st_uid not in {0, os.geteuid()}
    or info.st_mode & 0o022
):
    raise SystemExit(1)
try:
    with open(path, "rb") as handle:
        payload = handle.read(129)
except OSError:
    raise SystemExit(1)
if payload != b"schema=1\nmode=unmanaged-direct\n":
    raise SystemExit(1)
PY
}

unmanaged_direct_storage_valid() {
  unmanaged_direct_marker_valid || return 1
  [[ -d "$TARGET" && ! -L "$TARGET" ]] || return 1
  ! mountpoint -q "$TARGET" 2>/dev/null
}

cmd_up() {
  local rc=0 use_last_good=0
  if [[ -f "$DISABLED_FILE" ]]; then
    load_conf
  elif [[ -f "$LAST_GOOD_CONF_FILE" && ! -L "$LAST_GOOD_CONF_FILE" ]]; then
    if ! restore_conf_from_last_good; then
      log "failed to restore the last verified storage config for boot"
      return 1
    fi
    use_last_good=1
  elif [[ -e "$UNMANAGED_DIRECT_FILE" || -L "$UNMANAGED_DIRECT_FILE" ]]; then
    if ! unmanaged_direct_storage_valid; then
      log "unmanaged direct storage marker is invalid or target is unexpectedly mounted"
      write_status
      return 1
    fi
    log "storage remains on the verified unmanaged direct data root"
    write_status
    return 0
  else
    load_conf
  fi
  if ! prepare_service_scope; then
    write_status
    return 2
  fi
  if ! mountpoint -q "$TARGET" 2>/dev/null \
    && ! storage_transition_safe 1 "${STOP_SERVICES[@]}"; then
    log "refusing ordinary up because storage users are not proven stopped"
    write_status
    return 1
  fi
  mount_configured || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    return "$rc"
  fi
  if [[ "$use_last_good" -eq 1 ]]; then
    if ! recorded_mount_identity_valid "$LAST_GOOD_CONF_FILE"; then
      log "boot mount does not match the last verified storage identity"
      write_status
      return 1
    fi
  elif ! persist_last_good_mount; then
    log "mounted storage identity could not be persisted; refusing startup"
    write_status
    return 1
  fi
}

cmd_verify() {
  if [[ ! -f "$LAST_GOOD_CONF_FILE" \
      && ( -e "$UNMANAGED_DIRECT_FILE" || -L "$UNMANAGED_DIRECT_FILE" ) ]]; then
    if unmanaged_direct_storage_valid; then
      return 0
    fi
    log "unmanaged direct storage verification failed"
    return 1
  fi
  load_conf
  if ! configured_mount_valid; then
    log "configured storage mount identity verification failed"
    return 1
  fi
  if ! config_file_matches_loaded "$LAST_GOOD_CONF_FILE"; then
    log "configured storage does not match the last verified mount config"
    return 1
  fi
  if ! recorded_mount_identity_valid "$LAST_GOOD_CONF_FILE"; then
    log "configured storage does not match the last verified mount identity"
    return 1
  fi
}

cmd_bind_identity() {
  local recorded_identity=""
  if [[ ! -f "$LAST_GOOD_CONF_FILE" || -L "$LAST_GOOD_CONF_FILE" ]]; then
    log "cannot bind dataset identity without a regular last-good config"
    return 1
  fi
  recorded_identity="$(
    kv_value "$LAST_GOOD_CONF_FILE" DATASET_IDENTITY 2>/dev/null || true
  )"
  if [[ -n "$recorded_identity" ]]; then
    log "last-good config already contains a dataset identity"
    return 1
  fi
  load_conf_file "$LAST_GOOD_CONF_FILE" || return 1
  if ! configured_mount_valid \
      || ! recorded_mount_transport_identity_valid "$LAST_GOOD_CONF_FILE"; then
    log "legacy last-good transport identity does not match the mounted dataset"
    return 1
  fi
  if ! persist_last_good_mount; then
    log "failed to bind durable dataset identity to legacy last-good config"
    return 1
  fi
  log "legacy last-good config upgraded with a durable dataset identity"
}

cmd_down() {
  local rc=0
  load_conf
  if ! prepare_service_scope; then
    write_status
    return 2
  fi
  if mountpoint -q "$TARGET" 2>/dev/null \
    && ! storage_transition_safe 1 "${STOP_SERVICES[@]}"; then
    log "refusing ordinary down because storage users are not proven stopped"
    write_status
    return 1
  fi
  umount_target_force || rc=$?
  write_status
  return "$rc"
}

stop_storage_services_fail_closed() {
  local stop_timeout="$1" stop_rc=0
  log "stopping storage-dependent services before rollback"
  compose_with_timeout "$stop_timeout" \
    stop -t 30 "${STOP_SERVICES[@]}" || stop_rc=$?
  if [[ "$stop_rc" -ne 0 ]]; then
    log "graceful rollback stop failed or timed out; verifying actual service state"
  fi
  if storage_transition_safe 0 "${STOP_SERVICES[@]}"; then
    return 0
  fi

  log "storage writers are not proven stopped; forcing Docker service shutdown"
  compose_with_timeout "$stop_timeout" \
    kill -s KILL "${STOP_SERVICES[@]}" >/dev/null 2>&1 || true
  if storage_transition_safe 0 "${STOP_SERVICES[@]}"; then
    return 0
  fi
  log "storage writers or target users remain active; rollback cannot continue safely"
  return 1
}

recover_stopped_services_if_safe() {
  local stop_timeout="$1" start_timeout="$2" db_moves_with_target="$3"
  local old_present="$4" old_mount_id="$5" old_source="$6" old_fstype="$7"
  if [[ "$old_present" -eq 0 ]]; then
    if [[ ! -e "$UNMANAGED_DIRECT_FILE" && ! -L "$UNMANAGED_DIRECT_FILE" ]]; then
      log "no valid previous mount exists; keeping stopped services down"
      return 1
    fi
    if ! unmanaged_direct_storage_valid; then
      log "unmanaged direct storage baseline is no longer valid; keeping services down"
      return 1
    fi
  elif [[ "$old_present" -eq 1 ]]; then
    if ! mount_snapshot_still_valid \
        "$old_present" "$old_mount_id" "$old_source" "$old_fstype"; then
      log "previous mount is no longer valid; keeping stopped services down"
      return 1
    fi
  else
    log "previous storage state is invalid; keeping stopped services down"
    return 1
  fi
  if [[ "$db_moves_with_target" -eq 1 ]]; then
    log "previous storage baseline is valid; restarting postgres/redis"
    if ! compose_with_timeout "$start_timeout" start postgres redis; then
      log "postgres/redis recovery restart failed; stopping all storage services"
      stop_storage_services_fail_closed "$stop_timeout" || true
      return 1
    fi
  fi
  log "previous storage baseline is valid; restarting application services"
  if ! compose_with_timeout "$start_timeout" start "${APP_SERVICES[@]}"; then
    log "application service recovery restart failed; stopping all storage services"
    stop_storage_services_fail_closed "$stop_timeout" || true
    return 1
  fi
  if ! storage_core_readiness "$start_timeout"; then
    log "previous mount service readiness failed; stopping all storage services"
    stop_storage_services_fail_closed "$stop_timeout" || true
    return 1
  fi
  return 0
}

rollback_config_valid_for_snapshot() {
  local old_present="$1" old_mount_id="$2" old_source="$3" old_fstype="$4"
  local rc=0
  if [[ "$old_present" -eq 0 \
      && ! -e "$LAST_GOOD_CONF_FILE" \
      && ! -L "$LAST_GOOD_CONF_FILE" ]] \
      && unmanaged_direct_storage_valid; then
    rc=0
  elif [[ "$old_present" -ne 1 || ! -f "$LAST_GOOD_CONF_FILE" ]]; then
    rc=1
  elif ! load_conf_file "$LAST_GOOD_CONF_FILE"; then
    rc=1
  elif ! configured_mount_valid; then
    log "previous mount does not match the last-good config"
    rc=1
  elif ! recorded_mount_identity_valid "$LAST_GOOD_CONF_FILE"; then
    log "previous mount does not match the last-good identity"
    rc=1
  elif ! mount_snapshot_still_valid \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype"; then
    log "previous mount changed while rollback eligibility was checked"
    rc=1
  fi
  load_conf
  return "$rc"
}

restored_mount_matches_previous_identity() {
  local old_source="$1" old_fstype="$2"
  local current_source="" current_fstype=""
  configured_mount_valid || return 1
  recorded_mount_identity_valid "$LAST_GOOD_CONF_FILE" || return 1
  current_source="$(findmnt_value "$TARGET" SOURCE)" || return 1
  current_fstype="$(findmnt_value "$TARGET" FSTYPE)" || return 1
  [[ "$current_source" == "$old_source" && "$current_fstype" == "$old_fstype" ]]
}

rollback_previous_mount() {
  local stop_timeout="$1" start_timeout="$2" db_moves_with_target="$3"
  local old_present="$4" old_source="$5" old_fstype="$6"
  if [[ "$old_present" -eq 0 ]] \
      && [[ ! -e "$LAST_GOOD_CONF_FILE" && ! -L "$LAST_GOOD_CONF_FILE" ]] \
      && [[ -e "$UNMANAGED_DIRECT_FILE" || -L "$UNMANAGED_DIRECT_FILE" ]]; then
    if mountpoint -q "$TARGET" 2>/dev/null && ! umount_target_force; then
      log "failed to remove the unsuccessful replacement mount; keeping services stopped"
      write_status
      return 1
    fi
    if ! unmanaged_direct_storage_valid; then
      log "unmanaged direct storage baseline could not be restored"
      write_status
      return 1
    fi
    log "unmanaged direct storage baseline verified; restarting stopped services"
    if ! recover_stopped_services_if_safe \
        "$stop_timeout" "$start_timeout" "$db_moves_with_target" \
        0 "" "" ""; then
      log "unmanaged direct storage was restored but service recovery failed"
      return 2
    fi
    return 0
  fi
  if [[ "$old_present" -ne 1 || ! -f "$LAST_GOOD_CONF_FILE" ]]; then
    log "no verified previous mount config exists; keeping stopped services down"
    return 1
  fi
  if ! restore_conf_from_last_good; then
    log "failed to restore the previous storage config; keeping services stopped"
    return 1
  fi
  if mountpoint -q "$TARGET" 2>/dev/null && ! umount_target_force; then
    log "failed to remove the unsuccessful replacement mount; keeping services stopped"
    write_status
    return 1
  fi
  log "restoring previous storage mount source=$old_source fstype=$old_fstype"
  if ! mount_configured; then
    log "previous storage mount could not be restored; keeping services stopped"
    write_status
    return 1
  fi
  if ! restored_mount_matches_previous_identity "$old_source" "$old_fstype"; then
    log "restored mount failed previous identity verification; keeping services stopped"
    write_status
    return 1
  fi
  if ! capture_mount_snapshot || [[ "$CAPTURED_MOUNT_PRESENT" -ne 1 ]]; then
    log "restored mount identity could not be captured; keeping services stopped"
    write_status
    return 1
  fi
  log "previous storage mount identity verified; restarting stopped services"
  if ! recover_stopped_services_if_safe \
      "$stop_timeout" "$start_timeout" "$db_moves_with_target" \
      "$CAPTURED_MOUNT_PRESENT" "$CAPTURED_MOUNT_ID" \
      "$CAPTURED_MOUNT_SOURCE" "$CAPTURED_MOUNT_FSTYPE"; then
    log "previous mount was restored but service recovery failed"
    return 2
  fi
  return 0
}

write_apply_rollback_result() {
  local call_id="$1" failure_reason="$2" rollback_rc="$3" started_at="$4"
  case "$rollback_rc" in
    0)
      write_apply_result "$call_id" "fail" \
        "$failure_reason; restored and verified previous mount" "$started_at"
      ;;
    2)
      write_apply_result "$call_id" "fail" \
        "$failure_reason; previous mount restored but service recovery failed" \
        "$started_at"
      ;;
    *)
      write_apply_result "$call_id" "fail" \
        "$failure_reason; previous mount rollback failed and services remain stopped" \
        "$started_at"
      ;;
  esac
}

rollback_started_replacement() {
  local stop_timeout="$1" start_timeout="$2" db_moves_with_target="$3"
  local old_present="$4" old_source="$5" old_fstype="$6"
  if ! stop_storage_services_fail_closed "$stop_timeout"; then
    log "replacement storage rollback blocked because writers are not stopped"
    return 1
  fi
  rollback_previous_mount \
    "$stop_timeout" "$start_timeout" "$db_moves_with_target" \
    "$old_present" "$old_source" "$old_fstype"
}

fail_started_replacement_apply() {
  local call_id="$1" failure_reason="$2" started_at="$3"
  local stop_timeout="$4" start_timeout="$5" db_moves_with_target="$6"
  local old_present="$7" old_source="$8" old_fstype="$9"
  local rollback_rc=0
  rollback_started_replacement \
    "$stop_timeout" "$start_timeout" "$db_moves_with_target" \
    "$old_present" "$old_source" "$old_fstype" || rollback_rc=$?
  write_status
  write_apply_rollback_result \
    "$call_id" "$failure_reason" "$rollback_rc" "$started_at"
}

# Full reload cycle: stop dependent docker services, swap mount, start them.
cmd_apply() {
  local call_id="" fence=""
  local started_at
  started_at=$(date -u +%s)

  exec 8>"${STATE_DIR}/apply.lock"
  if ! flock -n 8; then
    log "another apply in progress, abort"
    return 75
  fi
  if ! select_apply_request; then
    log "no pending storage apply request"
    return 0
  fi
  call_id="$APPLY_OPERATION_ID"
  fence="$APPLY_FENCE"
  if apply_result_terminal_for_identity "$call_id" "$fence"; then
    log "operation $call_id fence=$fence already has a terminal result"
    rm -f -- "$APPLY_REQUEST_FILE"
    return 0
  fi
  local claim_rc=0
  claim_apply_operation "$call_id" "$fence" || claim_rc=$?
  case "$claim_rc" in
    0)
      ;;
    12)
      log "operation $call_id fence=$fence is older than the host fence"
      write_apply_result "$call_id" "fail" \
        "stale storage apply fence rejected by host" "$started_at"
      return 0
      ;;
    *)
      log "another unresolved or invalid storage apply claim exists"
      write_apply_result "$call_id" "fail" \
        "another unresolved or invalid storage apply claim exists" "$started_at"
      return 1
      ;;
  esac
  if apply_result_terminal_for_identity "$call_id" "$fence"; then
    log "operation $call_id became terminal before host side effects"
    rm -f -- "$APPLY_REQUEST_FILE"
    return 0
  fi
  if ! storage_acquire_maintenance_lock; then
    log "another maintenance operation is active, abort"
    write_apply_result "$call_id" "fail" \
      "another maintenance operation is active" "$started_at"
    return 1
  fi
  if ! storage_require_no_active_systemd_fallback_writers; then
    log "systemd fallback writers are active or unverifiable, abort"
    write_apply_result "$call_id" "fail" \
      "systemd fallback writers are active or unverifiable" "$started_at"
    return 1
  fi

  if ! activate_apply_request; then
    log "storage apply request failed identity/hash validation"
    write_apply_result "$call_id" "fail" \
      "storage apply request failed identity/hash validation" "$started_at"
    return 2
  fi
  load_conf
  log "apply start operation=$call_id fence=$fence mode=$MODE"

  # docker compose stop/start 加 timeout 防卡死。stop 用 -t 30 + 整体 timeout 60s
  # （worker stop_grace_period=1830s 但我们必须跳过这个 grace 否则 apply 一卡半小时）。
  # start 90s 给容器拉起 + healthcheck 余地。
  local stop_timeout="${LUMEN_STORAGE_DOCKER_STOP_TIMEOUT:-60}"
  local start_timeout="${LUMEN_STORAGE_DOCKER_START_TIMEOUT:-90}"
  local old_present=0 old_mount_id="" old_source="" old_fstype=""
  if ! prepare_service_scope; then
    write_apply_result "$call_id" "fail" "invalid docker compose service list" "$started_at"
    return 2
  fi
  if [[ "$DB_MOVES_WITH_TARGET" -eq 1 ]]; then
    log "database root $LUMEN_DB_ROOT moves with $TARGET; postgres/redis require a clean stop"
  fi

  if ! compose_available; then
    log "refusing remount: docker compose is unavailable"
    write_apply_result "$call_id" "fail" \
      "refused remount: cannot perform the required Docker stop workflow" "$started_at"
    return 1
  fi

  if ! capture_mount_snapshot; then
    log "refusing remount: cannot snapshot the current mount identity"
    write_apply_result "$call_id" "fail" \
      "refused remount: cannot verify the current mount identity" "$started_at"
    return 1
  fi
  old_present="$CAPTURED_MOUNT_PRESENT"
  old_mount_id="$CAPTURED_MOUNT_ID"
  old_source="$CAPTURED_MOUNT_SOURCE"
  old_fstype="$CAPTURED_MOUNT_FSTYPE"
  if ! rollback_config_valid_for_snapshot \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype"; then
    log "refusing remount: previous mount has no verified rollback identity"
    write_apply_result "$call_id" "fail" \
      "refused remount: previous mount rollback identity is not verified" \
      "$started_at"
    return 1
  fi

  log "docker compose stop ${STOP_SERVICES[*]} (timeout ${stop_timeout}s)"
  if ! compose_with_timeout "$stop_timeout" \
      stop -t 30 "${STOP_SERVICES[@]}"; then
    log "refusing remount: docker compose stop failed or timed out"
    recover_stopped_services_if_safe "$stop_timeout" "$start_timeout" \
      "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" \
      >/dev/null 2>&1 || true
    write_apply_result "$call_id" "fail" \
      "refused remount: dependent services did not stop cleanly" "$started_at"
    return 1
  fi

  if ! storage_transition_safe 0 "${STOP_SERVICES[@]}"; then
    log "refusing remount: stopped-service or target-idle verification failed"
    recover_stopped_services_if_safe "$stop_timeout" "$start_timeout" \
      "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" || true
    write_apply_result "$call_id" "fail" \
      "refused remount: services or target remain active after stop" "$started_at"
    return 1
  fi

  if ! mount_snapshot_still_valid \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype"; then
    log "refusing remount: target mount changed while services were stopping"
    recover_stopped_services_if_safe "$stop_timeout" "$start_timeout" \
      "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" || true
    write_apply_result "$call_id" "fail" \
      "refused remount: target mount changed during stop" "$started_at"
    return 1
  fi

  if ! umount_target_force; then
    log "refusing remount: target could not be safely unmounted"
    recover_stopped_services_if_safe "$stop_timeout" "$start_timeout" \
      "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" || true
    write_apply_result "$call_id" "fail" \
      "refused remount: target remained mounted" "$started_at"
    return 1
  fi

  if ! mount_configured || ! configured_mount_valid; then
    local rollback_rc=0
    log "new storage mount failed identity verification; attempting verified rollback"
    rollback_previous_mount "$stop_timeout" "$start_timeout" \
      "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_source" "$old_fstype" || rollback_rc=$?
    write_status
    write_apply_rollback_result \
      "$call_id" "new mount failed" "$rollback_rc" "$started_at"
    return 1
  fi

  if [[ "$DB_MOVES_WITH_TARGET" -eq 1 ]]; then
    log "docker compose start postgres redis (timeout ${start_timeout}s)"
    if ! compose_with_timeout "$start_timeout" start postgres redis; then
      log "postgres/redis restart failed on replacement storage; rolling back"
      fail_started_replacement_apply \
        "$call_id" "new mount postgres/redis startup failed" "$started_at" \
        "$stop_timeout" "$start_timeout" "$DB_MOVES_WITH_TARGET" \
        "$old_present" "$old_source" "$old_fstype"
      return 1
    fi
  fi
  log "docker compose start ${APP_SERVICES[*]} (timeout ${start_timeout}s)"
  if ! compose_with_timeout "$start_timeout" start "${APP_SERVICES[@]}"; then
    log "application service restart failed on replacement storage; rolling back"
    fail_started_replacement_apply \
      "$call_id" "new mount application startup failed" "$started_at" \
      "$stop_timeout" "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_source" "$old_fstype"
    return 1
  fi
  if ! storage_core_readiness "$start_timeout"; then
    log "replacement storage API/Worker readiness failed; rolling back"
    fail_started_replacement_apply \
      "$call_id" "new mount readiness failed" "$started_at" \
      "$stop_timeout" "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_source" "$old_fstype"
    return 1
  fi
  if ! persist_last_good_mount; then
    log "replacement storage readiness passed but last-good promotion failed"
    fail_started_replacement_apply \
      "$call_id" "new mount last-good promotion failed" "$started_at" \
      "$stop_timeout" "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_source" "$old_fstype"
    return 1
  fi

  log "apply done"
  write_apply_result "$call_id" "ok" "applied mode=$MODE" "$started_at"
}

# SMB connectivity test against $TEST_CONF_FILE; mounts to $TEST_TARGET, write-probes, unmounts.
cmd_test() {
  local call_id=""
  if ! call_id="$(trigger_call_id "$TEST_TRIGGER_FILE")"; then
    log "invalid or missing test trigger"
    write_test_result "" "fail" "invalid or missing test trigger"
    return 2
  fi
  if [[ ! -f "$TEST_CONF_FILE" ]]; then
    write_test_result "$call_id" "fail" "test conf not found at $TEST_CONF_FILE"
    return 1
  fi
  SMB_HOST="$(kv_value "$TEST_CONF_FILE" SMB_HOST 2>/dev/null || true)"
  SMB_PORT="$(kv_value "$TEST_CONF_FILE" SMB_PORT 2>/dev/null || true)"
  SMB_SHARE="$(kv_value "$TEST_CONF_FILE" SMB_SHARE 2>/dev/null || true)"
  SMB_SUBPATH="$(kv_value "$TEST_CONF_FILE" SMB_SUBPATH 2>/dev/null || true)"
  SMB_USERNAME="$(kv_value "$TEST_CONF_FILE" SMB_USERNAME 2>/dev/null || true)"
  SMB_PASSWORD="$(kv_value "$TEST_CONF_FILE" SMB_PASSWORD 2>/dev/null || true)"
  if [[ -z "${SMB_HOST:-}" || -z "${SMB_SHARE:-}" || -z "${SMB_USERNAME:-}" || -z "${SMB_PASSWORD:-}" ]]; then
    write_test_result "$call_id" "fail" "test config incomplete (host/share/username/password)"
    rm -f "$TEST_CONF_FILE"
    return 1
  fi
  local source cred opts msg
  source="$(build_smb_source "$SMB_HOST" "$SMB_SHARE" "${SMB_SUBPATH:-/}")"
  cred="$(mktemp /run/lumen-smb-test-cred.XXXXXX)"
  # shellcheck disable=SC2064
  trap "rm -f '$cred'" RETURN EXIT
  write_smb_credentials "$SMB_USERNAME" "$SMB_PASSWORD" "$cred"
  opts="credentials=${cred},uid=${LUMEN_UID},gid=${LUMEN_GID},forceuid,forcegid,file_mode=0664,dir_mode=0775,${CIFS_OPTS_BASE}"
  if [[ -n "${SMB_PORT:-}" ]]; then
    opts="${opts},port=${SMB_PORT}"
  fi
  mkdir -p "$TEST_TARGET"
  mountpoint -q "$TEST_TARGET" && umount -l "$TEST_TARGET" 2>/dev/null || true
  if msg="$(mount -t cifs "$source" "$TEST_TARGET" -o "$opts" 2>&1)"; then
    local probe="${TEST_TARGET}/.lumen_test_$$"
    if touch "$probe" 2>/dev/null; then
      rm -f "$probe"
      umount -l "$TEST_TARGET" 2>/dev/null || true
      write_test_result "$call_id" "ok" "connected to $source, write OK"
      rm -f "$cred"
      trap - RETURN
      trap - EXIT
      rm -f "$TEST_CONF_FILE"
      return 0
    fi
    umount -l "$TEST_TARGET" 2>/dev/null || true
    write_test_result "$call_id" "fail" "mounted but write probe failed at $TEST_TARGET"
    rm -f "$cred"
    trap - RETURN
    trap - EXIT
    rm -f "$TEST_CONF_FILE"
    return 1
  fi
  write_test_result "$call_id" "fail" "mount failed: ${msg}"
  rm -f "$cred"
  trap - RETURN
  trap - EXIT
  rm -f "$TEST_CONF_FILE"
  return 1
}

cmd_status() {
  write_status
  cat "$STATUS_FILE"
}

cmd_apply_result_terminal() {
  if ! select_apply_request; then
    return 0
  fi
  apply_result_terminal_for_identity "$APPLY_OPERATION_ID" "$APPLY_FENCE"
}

cmd_help() {
  cat <<EOF
Usage: $(basename "$0") {up|verify|bind-identity|down|apply|apply-result-terminal|test|status|help}
  up      Mount /opt/lumendata per current conf (idempotent).
  verify  Verify current mount against config and last-good identity.
  bind-identity  Upgrade a verified legacy last-good with a dataset marker.
  down    Unmount /opt/lumendata.
  apply   Stop dependent docker services, swap mount, restart services.
  apply-result-terminal  Check whether the highest request has a terminal result.
  test    Test SMB credentials in conf at $TEST_CONF_FILE.
  status  Print current mount status JSON.

Files:
  $CONF_FILE          current mount config (KEY=VAL)
  $LAST_GOOD_CONF_FILE last verified config and mount identity
  $TEST_CONF_FILE     test mount config (transient, removed after test)
  $DISABLED_FILE      escape hatch: forces local mode on $DEFAULT_LOCAL_ROOT
  $STATUS_FILE        status snapshot (read by API)
  $APPLY_REQUESTS_DIR  immutable API apply requests named operation_id.fence.json
  $APPLY_RESULTS_DIR   immutable host results with the same identity and fence
  $APPLY_RESULT_FILE   compatibility snapshot of the newest apply result
  $APPLY_CLAIM_FILE    durable monotonic host fence and operation claim
  $TEST_RESULT_FILE   last test result (read by API)
EOF
}

main() {
  local sub="${1:-help}"; shift || true
  case "$sub" in
    up)     cmd_up ;;
    verify) cmd_verify ;;
    bind-identity) cmd_bind_identity ;;
    down)   cmd_down ;;
    apply)  cmd_apply ;;
    apply-result-terminal) cmd_apply_result_terminal ;;
    test)   cmd_test ;;
    status) cmd_status ;;
    help|-h|--help) cmd_help ;;
    *) cmd_help; exit 2 ;;
  esac
}

main "$@"
