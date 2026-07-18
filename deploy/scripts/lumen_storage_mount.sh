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
DISABLED_FILE="${STATE_DIR}/disabled"
STATUS_FILE="${STATE_DIR}/status.json"
APPLY_RESULT_FILE="${STATE_DIR}/last-apply.json"
TEST_RESULT_FILE="${STATE_DIR}/last-test.json"
TEST_CONF_FILE="${STATE_DIR}/test.conf"
APPLY_TRIGGER_FILE="${STATE_DIR}/apply.trigger"
TEST_TRIGGER_FILE="${STATE_DIR}/test.trigger"
TARGET="${LUMEN_STORAGE_TARGET:-/opt/lumendata}"
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

mkdir -p "$STATE_DIR"
chmod 0775 "$STATE_DIR" 2>/dev/null || true

log() {
  printf '[lumen-storage] %s\n' "$*" >&2
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

trigger_call_id() {
  local path="$1" value=""
  [[ -f "$path" ]] || return 1
  IFS= read -r value < "$path" || true
  if [[ ! "$value" =~ ^[0-9a-f]{32}$ ]]; then
    return 2
  fi
  printf '%s\n' "$value"
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


def readlink(path: str) -> str | None:
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
  local now
  now=$(date -u +%s)
  {
    printf '{\n'
    printf '  "call_id": %s,\n' "$(json_str "$call_id")"
    printf '  "status": %s,\n' "$(json_str "$status")"
    printf '  "message": %s,\n' "$(json_str "$message")"
    printf '  "started_at": %s,\n' "$started_at"
    printf '  "finished_at": %s\n' "$now"
    printf '}\n'
  } > "${APPLY_RESULT_FILE}.tmp"
  mv "${APPLY_RESULT_FILE}.tmp" "$APPLY_RESULT_FILE"
  chmod 0644 "$APPLY_RESULT_FILE" 2>/dev/null || true
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

# Load effective config into MODE/LOCAL_ROOT/SMB_*.
# escape hatch: when DISABLED_FILE exists, force local mode on default root.
load_conf() {
  if [[ -f "$DISABLED_FILE" ]]; then
    MODE="local"
    LOCAL_ROOT="$DEFAULT_LOCAL_ROOT"
    SMB_HOST=""; SMB_SHARE=""; SMB_SUBPATH="/"; SMB_USERNAME=""; SMB_PASSWORD=""
    log "DISABLED_FILE present, forcing local mode on $DEFAULT_LOCAL_ROOT"
    return 0
  fi
  if [[ ! -f "$CONF_FILE" ]]; then
    MODE="local"
    LOCAL_ROOT="$DEFAULT_LOCAL_ROOT"
    SMB_HOST=""; SMB_PORT=""; SMB_SHARE=""; SMB_SUBPATH="/"; SMB_USERNAME=""; SMB_PASSWORD=""
    return 0
  fi
  MODE="$(kv_value "$CONF_FILE" MODE 2>/dev/null || true)"
  MODE="${MODE:-local}"
  LOCAL_ROOT="$(kv_value "$CONF_FILE" LOCAL_ROOT 2>/dev/null || true)"
  LOCAL_ROOT="${LOCAL_ROOT:-$DEFAULT_LOCAL_ROOT}"
  SMB_HOST="$(kv_value "$CONF_FILE" SMB_HOST 2>/dev/null || true)"
  # 空 → 走 mount.cifs 默认 445；其他值（数字字符串）拼到 -o port=
  SMB_PORT="$(kv_value "$CONF_FILE" SMB_PORT 2>/dev/null || true)"
  SMB_SHARE="$(kv_value "$CONF_FILE" SMB_SHARE 2>/dev/null || true)"
  SMB_SUBPATH="$(kv_value "$CONF_FILE" SMB_SUBPATH 2>/dev/null || true)"
  SMB_SUBPATH="${SMB_SUBPATH:-/}"
  SMB_USERNAME="$(kv_value "$CONF_FILE" SMB_USERNAME 2>/dev/null || true)"
  SMB_PASSWORD="$(kv_value "$CONF_FILE" SMB_PASSWORD 2>/dev/null || true)"
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
  local target_identity="" local_identity=""
  if ! mountpoint -q "$TARGET" 2>/dev/null; then
    log "local mount verification failed: $TARGET is not a mountpoint"
    return 1
  fi
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
  if ! mountpoint -q "$TARGET" 2>/dev/null; then
    log "SMB mount verification failed: $TARGET is not a mountpoint"
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
  mkdir -p "$LOCAL_ROOT"
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

cmd_up() {
  load_conf
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
  mount_configured
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

recover_stopped_services_if_safe() {
  local start_timeout="$1" db_moves_with_target="$2"
  local old_present="$3" old_mount_id="$4" old_source="$5" old_fstype="$6"
  if [[ "$old_present" -ne 1 ]]; then
    log "no valid previous mount exists; keeping stopped services down"
    return 1
  fi
  if ! mount_snapshot_still_valid \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype"; then
    log "previous mount is no longer valid; keeping stopped services down"
    return 1
  fi
  if [[ "$db_moves_with_target" -eq 1 ]]; then
    log "previous mount is still valid; restarting postgres/redis"
    if ! compose_with_timeout "$start_timeout" start postgres redis; then
      log "postgres/redis recovery restart failed; application services remain stopped"
      return 1
    fi
  fi
  log "previous mount is still valid; restarting application services"
  compose_with_timeout "$start_timeout" start "${APP_SERVICES[@]}" || return 1
}

# Full reload cycle: stop dependent docker services, swap mount, start them.
cmd_apply() {
  local call_id=""
  local started_at
  started_at=$(date -u +%s)
  if ! call_id="$(trigger_call_id "$APPLY_TRIGGER_FILE")"; then
    log "invalid or missing apply trigger"
    write_apply_result "" "fail" "invalid or missing apply trigger" "$started_at"
    return 2
  fi

  exec 9>"${STATE_DIR}/apply.lock"
  if ! flock -n 9; then
    log "another apply in progress, abort"
    write_apply_result "$call_id" "fail" "another apply in progress" "$started_at"
    return 1
  fi

  load_conf
  log "apply start mode=$MODE"

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

  log "docker compose stop ${STOP_SERVICES[*]} (timeout ${stop_timeout}s)"
  if ! compose_with_timeout "$stop_timeout" \
      stop -t 30 "${STOP_SERVICES[@]}"; then
    log "refusing remount: docker compose stop failed or timed out"
    recover_stopped_services_if_safe "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" \
      >/dev/null 2>&1 || true
    write_apply_result "$call_id" "fail" \
      "refused remount: dependent services did not stop cleanly" "$started_at"
    return 1
  fi

  if ! storage_transition_safe 0 "${STOP_SERVICES[@]}"; then
    log "refusing remount: stopped-service or target-idle verification failed"
    recover_stopped_services_if_safe "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" || true
    write_apply_result "$call_id" "fail" \
      "refused remount: services or target remain active after stop" "$started_at"
    return 1
  fi

  if ! mount_snapshot_still_valid \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype"; then
    log "refusing remount: target mount changed while services were stopping"
    recover_stopped_services_if_safe "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" || true
    write_apply_result "$call_id" "fail" \
      "refused remount: target mount changed during stop" "$started_at"
    return 1
  fi

  if ! umount_target_force; then
    log "refusing remount: target could not be safely unmounted"
    recover_stopped_services_if_safe "$start_timeout" "$DB_MOVES_WITH_TARGET" \
      "$old_present" "$old_mount_id" "$old_source" "$old_fstype" || true
    write_apply_result "$call_id" "fail" \
      "refused remount: target remained mounted" "$started_at"
    return 1
  fi

  if ! mount_configured || ! configured_mount_valid; then
    if [[ "$DB_MOVES_WITH_TARGET" -eq 1 ]]; then
      log "mount failed; keeping postgres/redis stopped to avoid opening a different data root"
      write_status
      write_apply_result "$call_id" "fail" \
        "mount failed; database services remain stopped to avoid data split" \
        "$started_at"
      return 1
    fi
    log "mount failed, falling back to local default to keep service usable"
    local fallback_ok=0
    MODE=local
    LOCAL_ROOT="$DEFAULT_LOCAL_ROOT"
    if mount_local; then
      fallback_ok=1
    fi
    write_status
    if [[ "$fallback_ok" -eq 1 ]]; then
      compose_with_timeout "$start_timeout" start "${APP_SERVICES[@]}" >/dev/null 2>&1 || true
      write_apply_result "$call_id" "fail" \
        "mount failed; fell back to local default $DEFAULT_LOCAL_ROOT" "$started_at"
    else
      log "fallback local mount also failed; application services remain stopped"
      write_apply_result "$call_id" "fail" \
        "mount failed; fallback mount failed and services remain stopped" "$started_at"
    fi
    return 1
  fi

  if [[ "$DB_MOVES_WITH_TARGET" -eq 1 ]]; then
    log "docker compose start postgres redis (timeout ${start_timeout}s)"
    if ! compose_with_timeout "$start_timeout" start postgres redis; then
      log "postgres/redis restart failed; application services remain stopped"
      write_apply_result "$call_id" "fail" \
        "mount applied but postgres/redis restart failed" "$started_at"
      return 1
    fi
  fi
  log "docker compose start ${APP_SERVICES[*]} (timeout ${start_timeout}s)"
  if ! compose_with_timeout "$start_timeout" start "${APP_SERVICES[@]}"; then
    log "application service restart failed or timed out"
    write_apply_result "$call_id" "fail" \
      "mount applied but application service restart failed" "$started_at"
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

cmd_help() {
  cat <<EOF
Usage: $(basename "$0") {up|down|apply|test|status|help}
  up      Mount /opt/lumendata per current conf (idempotent).
  down    Unmount /opt/lumendata.
  apply   Stop dependent docker services, swap mount, restart services.
  test    Test SMB credentials in conf at $TEST_CONF_FILE.
  status  Print current mount status JSON.

Files:
  $CONF_FILE          current mount config (KEY=VAL)
  $TEST_CONF_FILE     test mount config (transient, removed after test)
  $DISABLED_FILE      escape hatch: forces local mode on $DEFAULT_LOCAL_ROOT
  $STATUS_FILE        status snapshot (read by API)
  $APPLY_RESULT_FILE  last apply result (read by API)
  $TEST_RESULT_FILE   last test result (read by API)
EOF
}

main() {
  local sub="${1:-help}"; shift || true
  case "$sub" in
    up)     cmd_up ;;
    down)   cmd_down ;;
    apply)  cmd_apply ;;
    test)   cmd_test ;;
    status) cmd_status ;;
    help|-h|--help) cmd_help ;;
    *) cmd_help; exit 2 ;;
  esac
}

main "$@"
