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
TARGET="${LUMEN_STORAGE_TARGET:-/opt/lumendata}"
TEST_TARGET="${LUMEN_STORAGE_TEST_TARGET:-${STATE_DIR}/scratch}"
DEFAULT_LOCAL_ROOT="${LUMEN_STORAGE_DEFAULT_LOCAL_ROOT:-/var/lib/lumen-data}"

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

LUMEN_UID="${LUMEN_APP_UID:-995}"
LUMEN_GID="${LUMEN_APP_GID:-994}"

LUMEN_DOCKER_COMPOSE_DIR="${LUMEN_DOCKER_COMPOSE_DIR:-/opt/lumen/current}"
LUMEN_DOCKER_SERVICES="${LUMEN_DOCKER_SERVICES:-api worker tgbot web}"

mkdir -p "$STATE_DIR"
chmod 0775 "$STATE_DIR" 2>/dev/null || true

log() {
  printf '[lumen-storage] %s\n' "$*" >&2
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
    mode="$( . "$CONF_FILE"; printf '%s' "${MODE:-}" )"
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
  # shellcheck disable=SC1090
  . "$CONF_FILE"
  MODE="${MODE:-local}"
  LOCAL_ROOT="${LOCAL_ROOT:-$DEFAULT_LOCAL_ROOT}"
  SMB_HOST="${SMB_HOST:-}"
  # 空 → 走 mount.cifs 默认 445；其他值（数字字符串）拼到 -o port=
  SMB_PORT="${SMB_PORT:-}"
  SMB_SHARE="${SMB_SHARE:-}"
  SMB_SUBPATH="${SMB_SUBPATH:-/}"
  SMB_USERNAME="${SMB_USERNAME:-}"
  SMB_PASSWORD="${SMB_PASSWORD:-}"
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

mount_local() {
  mkdir -p "$LOCAL_ROOT"
  chown "$LUMEN_UID:$LUMEN_GID" "$LOCAL_ROOT" 2>/dev/null || true
  chmod 0775 "$LOCAL_ROOT" 2>/dev/null || true
  mkdir -p "$TARGET"
  if mountpoint -q "$TARGET"; then
    log "target $TARGET already mounted, skipping bind"
    return 0
  fi
  mount --bind "$LOCAL_ROOT" "$TARGET"
  log "bind $LOCAL_ROOT -> $TARGET OK"
}

mount_smb() {
  if [[ -z "$SMB_HOST" || -z "$SMB_SHARE" || -z "$SMB_USERNAME" || -z "$SMB_PASSWORD" ]]; then
    log "smb config incomplete (host/share/username/password)"
    return 1
  fi
  local source cred opts
  source="$(build_smb_source "$SMB_HOST" "$SMB_SHARE" "$SMB_SUBPATH")"
  cred="$(mktemp /run/lumen-smb-cred.XXXXXX)"
  # shellcheck disable=SC2064
  trap "rm -f '$cred'" RETURN
  write_smb_credentials "$SMB_USERNAME" "$SMB_PASSWORD" "$cred"
  opts="credentials=${cred},uid=${LUMEN_UID},gid=${LUMEN_GID},forceuid,forcegid,file_mode=0664,dir_mode=0775,${CIFS_OPTS_BASE}"
  if [[ -n "$SMB_PORT" ]]; then
    opts="${opts},port=${SMB_PORT}"
  fi
  mkdir -p "$TARGET"
  if mountpoint -q "$TARGET"; then
    log "target $TARGET already mounted; unmounting first"
    umount_target_force
  fi
  mount -t cifs "$source" "$TARGET" -o "$opts"
  log "cifs $source -> $TARGET OK"
}

umount_target_force() {
  if ! mountpoint -q "$TARGET"; then
    return 0
  fi
  if umount "$TARGET" 2>/dev/null; then
    return 0
  fi
  log "lazy umount $TARGET"
  umount -l "$TARGET" 2>/dev/null || true
}

cmd_up() {
  load_conf
  case "$MODE" in
    local) mount_local ;;
    smb)   mount_smb ;;
    *)     log "unknown mode: $MODE"; write_status; return 2 ;;
  esac
  write_status
}

cmd_down() {
  umount_target_force
  write_status
}

# Full reload cycle: stop dependent docker services, swap mount, start them.
cmd_apply() {
  local call_id="${LUMEN_STORAGE_APPLY_CALL_ID:-}"
  local started_at
  started_at=$(date -u +%s)

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

  if [[ -d "$LUMEN_DOCKER_COMPOSE_DIR" ]] && command -v docker >/dev/null 2>&1; then
    log "docker compose stop $LUMEN_DOCKER_SERVICES (timeout ${stop_timeout}s)"
    # shellcheck disable=SC2086
    (cd "$LUMEN_DOCKER_COMPOSE_DIR" && timeout "${stop_timeout}" docker compose stop -t 30 $LUMEN_DOCKER_SERVICES) \
      || log "docker compose stop returned non-zero or timed out (continuing)"
  fi

  umount_target_force

  if ! cmd_up; then
    log "mount failed, falling back to local default to keep service usable"
    MODE=local LOCAL_ROOT="$DEFAULT_LOCAL_ROOT" mount_local || true
    write_status
    if [[ -d "$LUMEN_DOCKER_COMPOSE_DIR" ]] && command -v docker >/dev/null 2>&1; then
      # shellcheck disable=SC2086
      (cd "$LUMEN_DOCKER_COMPOSE_DIR" && timeout "${start_timeout}" docker compose start $LUMEN_DOCKER_SERVICES) || true
    fi
    write_apply_result "$call_id" "fail" "mount failed; fell back to local default $DEFAULT_LOCAL_ROOT" "$started_at"
    return 1
  fi

  if [[ -d "$LUMEN_DOCKER_COMPOSE_DIR" ]] && command -v docker >/dev/null 2>&1; then
    log "docker compose start $LUMEN_DOCKER_SERVICES (timeout ${start_timeout}s)"
    # shellcheck disable=SC2086
    (cd "$LUMEN_DOCKER_COMPOSE_DIR" && timeout "${start_timeout}" docker compose start $LUMEN_DOCKER_SERVICES) \
      || log "docker compose start failed or timed out (services may still recover via restart policy)"
  fi

  log "apply done"
  write_apply_result "$call_id" "ok" "applied mode=$MODE" "$started_at"
}

# SMB connectivity test against $TEST_CONF_FILE; mounts to $TEST_TARGET, write-probes, unmounts.
cmd_test() {
  local call_id="${LUMEN_STORAGE_TEST_CALL_ID:-}"
  if [[ ! -f "$TEST_CONF_FILE" ]]; then
    write_test_result "$call_id" "fail" "test conf not found at $TEST_CONF_FILE"
    return 1
  fi
  # shellcheck disable=SC1090
  . "$TEST_CONF_FILE"
  if [[ -z "${SMB_HOST:-}" || -z "${SMB_SHARE:-}" || -z "${SMB_USERNAME:-}" || -z "${SMB_PASSWORD:-}" ]]; then
    write_test_result "$call_id" "fail" "test config incomplete (host/share/username/password)"
    rm -f "$TEST_CONF_FILE"
    return 1
  fi
  local source cred opts msg
  source="$(build_smb_source "$SMB_HOST" "$SMB_SHARE" "${SMB_SUBPATH:-/}")"
  cred="$(mktemp /run/lumen-smb-test-cred.XXXXXX)"
  # shellcheck disable=SC2064
  trap "rm -f '$cred'" RETURN
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
      rm -f "$TEST_CONF_FILE"
      return 0
    fi
    umount -l "$TEST_TARGET" 2>/dev/null || true
    write_test_result "$call_id" "fail" "mounted but write probe failed at $TEST_TARGET"
    rm -f "$TEST_CONF_FILE"
    return 1
  fi
  write_test_result "$call_id" "fail" "mount failed: ${msg}"
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
