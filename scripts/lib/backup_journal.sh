#!/usr/bin/env bash
# Durable service recovery journal for backup.sh.

LUMEN_BACKUP_EXECUTION_DOMAIN="${LUMEN_BACKUP_EXECUTION_DOMAIN:-scheduled}"
if [ -z "${LUMEN_BACKUP_JOURNAL_FILE:-}" ]; then
    case "$LUMEN_BACKUP_EXECUTION_DOMAIN" in
        scheduled)
            LUMEN_BACKUP_JOURNAL_FILE="${BACKUP_ROOT}/.recovery/backup.json"
            ;;
        update)
            _backup_journal_operation="${BACKUP_OPERATION_ID//[^A-Za-z0-9._-]/_}"
            [ -n "$_backup_journal_operation" ] || _backup_journal_operation="unknown"
            _backup_journal_root="${LUMEN_BACKUP_UPDATE_JOURNAL_ROOT:-/var/lib/lumen/backup/update}"
            LUMEN_BACKUP_JOURNAL_FILE="${_backup_journal_root}/${_backup_journal_operation}.json"
            unset _backup_journal_operation
            unset _backup_journal_root
            ;;
        *)
            log "ERROR: unsupported backup execution domain: $LUMEN_BACKUP_EXECUTION_DOMAIN"
            return 1 2>/dev/null || exit 1
            ;;
    esac
fi
LUMEN_BACKUP_JOURNAL_HELPER="${LUMEN_BACKUP_JOURNAL_HELPER:-${SCRIPT_DIR}/restore_journal.py}"
BACKUP_JOURNAL_ACTIVE=0
BACKUP_JOURNAL_RECOVERED=0

lumen_backup_journal_write() {
    local phase="$1"
    local service
    local -a args=(
        backup-write "$LUMEN_BACKUP_JOURNAL_FILE"
        --operation-id "${BACKUP_OPERATION_ID}"
        --phase "$phase"
    )
    for service in "${ACTIVE_WRITER_SERVICES[@]}"; do
        args+=(--service "$service")
    done
    if ! python3 "$LUMEN_BACKUP_JOURNAL_HELPER" "${args[@]}"; then
        log "ERROR: failed to persist backup recovery journal at phase $phase"
        return 1
    fi
    BACKUP_JOURNAL_ACTIVE=1
}

lumen_backup_journal_clear() {
    if ! python3 "$LUMEN_BACKUP_JOURNAL_HELPER" \
            backup-clear "$LUMEN_BACKUP_JOURNAL_FILE"; then
        log "ERROR: failed to clear backup recovery journal"
        return 1
    fi
    BACKUP_JOURNAL_ACTIVE=0
}

lumen_backup_journal_load() {
    local assignments rc
    set +e
    assignments="$(
        python3 "$LUMEN_BACKUP_JOURNAL_HELPER" \
            backup-load-shell "$LUMEN_BACKUP_JOURNAL_FILE"
    )"
    rc=$?
    set -e
    if [ "$rc" -eq 3 ]; then
        return 1
    fi
    if [ "$rc" -ne 0 ]; then
        log "ERROR: backup recovery journal is present but invalid"
        return 2
    fi
    eval "$assignments"
    BACKUP_JOURNAL_ACTIVE=1
}

lumen_backup_recover_interrupted() {
    local load_rc=0 service
    lumen_backup_journal_load || load_rc=$?
    if [ "$load_rc" -eq 1 ]; then
        return 0
    fi
    [ "$load_rc" -eq 0 ] || return 1
    case "$BACKUP_JOURNAL_PHASE" in
        writers_stopping|writers_stopped|writers_starting) ;;
        *)
            log "ERROR: unknown backup recovery phase: $BACKUP_JOURNAL_PHASE"
            return 1
            ;;
    esac
    ACTIVE_WRITER_SERVICES=()
    for service in $BACKUP_JOURNAL_ACTIVE_WRITER_SERVICES; do
        case "$service" in
            api|worker|tgbot) ACTIVE_WRITER_SERVICES+=("$service") ;;
            *)
                log "ERROR: invalid writer service in backup recovery journal"
                return 1
                ;;
        esac
    done
    log "recovering interrupted backup operation ${BACKUP_JOURNAL_OPERATION_ID}"
    if [ "${#ACTIVE_WRITER_SERVICES[@]}" -gt 0 ] \
            && ! lumen_start_services_verified "${ACTIVE_WRITER_SERVICES[@]}"; then
        log "ERROR: failed to restore writers after interrupted backup"
        return 1
    fi
    WRITERS_STOPPED=0
    if ! lumen_backup_journal_clear; then
        return 1
    fi
    ACTIVE_WRITER_SERVICES=()
    BACKUP_JOURNAL_RECOVERED=1
    log "interrupted backup service state recovered"
}

# Success resumes writers early; EXIT finalizes the journal. Keeping the
# journal through retention makes SIGKILL recovery restart-only, not a second
# backup that pauses already healthy writers again.
lumen_backup_resume_writers() {
    local finalize="${1:-1}"
    local journal_rc=0
    if [ "${WRITERS_STOPPED:-0}" -eq 1 ]; then
        log "restarting quiesced writers: ${ACTIVE_WRITER_SERVICES[*]:-<none>}"
        if [ "${BACKUP_JOURNAL_ACTIVE:-0}" -eq 1 ]; then
            lumen_backup_journal_write "writers_starting" || journal_rc=70
        fi
        if [ "${#ACTIVE_WRITER_SERVICES[@]}" -gt 0 ] \
                && ! lumen_start_services_verified "${ACTIVE_WRITER_SERVICES[@]}"; then
            log "ERROR: failed to restart one or more backup writers"
            return 70
        fi
        # Never erase evidence when persisting the state transition failed.
        [ "$journal_rc" -eq 0 ] || return "$journal_rc"
        WRITERS_STOPPED=0
        BACKUP_WRITERS_RESUMED=1
        log "backup writers restored and readiness verified"
    fi
    if [ "$finalize" = "1" ] \
            && [ "${BACKUP_WRITERS_RESUMED:-0}" = "1" ] \
            && [ "${BACKUP_JOURNAL_ACTIVE:-0}" = "1" ]; then
        lumen_backup_journal_clear || return 70
    fi
    return 0
}

lumen_backup_capture_storage_watermark() {
    local storage_root="$1"
    python3 - "${storage_root}" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
file_count = 0
total_bytes = 0
exists = False
try:
    st = root.lstat()
    exists = stat.S_ISDIR(st.st_mode)
except OSError:
    exists = False

if exists:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    name = entry.name
                    if name == ".quarantine" or name.startswith(".tmp"):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        file_count += 1
                        total_bytes += int(info.st_size)
        except OSError:
            continue

print(
    json.dumps(
        {
            "exists": exists,
            "file_count": file_count,
            "root": str(root),
            "total_bytes": total_bytes,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
)
PY
}

lumen_backup_run_storage_hook() {
    local hook="${LUMEN_STORAGE_BACKUP_HOOK:-}"
    local timeout_sec="${LUMEN_STORAGE_BACKUP_HOOK_TIMEOUT:-300}"
    local required="${LUMEN_STORAGE_BACKUP_HOOK_REQUIRED:-0}"
    if [ -z "${hook}" ]; then
        BACKUP_STORAGE_HOOK_STATUS="not_configured"
        BACKUP_STORAGE_HOOK_RC=0
        return 0
    fi
    local outcome=""
    outcome="$(
        python3 - "${hook}" "${timeout_sec}" \
            "${BACKUP_ROOT:-}" "${STORAGE_ROOT:-}" "${TS:-}" \
            "${BACKUP_OPERATION_ID:-}" "${PG_OUT:-}" "${REDIS_OUT:-}" <<'PY'
import os
import stat
import subprocess
import sys
from pathlib import Path

hook_raw, timeout_raw, backup_root, storage_root, ts, op_id, pg_out, redis_out = sys.argv[1:9]
if (
    not hook_raw.startswith("/")
    or any(ch in hook_raw for ch in ("\x00", "\r", "\n"))
):
    print("rejected_unsafe\t126\thook path must be an absolute path without control chars")
    raise SystemExit(0)
hook_path = Path(hook_raw)
try:
    info = hook_path.lstat()
except OSError as exc:
    print(f"rejected_unsafe\t127\thook missing: {exc}")
    raise SystemExit(0)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
    print("rejected_unsafe\t126\thook must be a regular non-symlink file")
    raise SystemExit(0)
if info.st_mode & 0o002:
    print("rejected_unsafe\t126\thook must not be world-writable")
    raise SystemExit(0)
if not os.access(hook_path, os.X_OK):
    print("rejected_unsafe\t126\thook is not executable")
    raise SystemExit(0)
try:
    timeout_val = max(1.0, min(3600.0, float(timeout_raw)))
except ValueError:
    timeout_val = 300.0
env = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": os.environ.get("HOME", "/root"),
    "LANG": os.environ.get("LANG", "C.UTF-8"),
    "BACKUP_ROOT": backup_root,
    "LUMEN_BACKUP_ROOT": backup_root,
    "STORAGE_ROOT": storage_root,
    "LUMEN_STORAGE_ROOT": storage_root,
    "LUMEN_BACKUP_TIMESTAMP": ts,
    "LUMEN_BACKUP_OPERATION_ID": op_id,
    "LUMEN_PG_BACKUP_PATH": pg_out,
    "LUMEN_REDIS_BACKUP_PATH": redis_out,
}
try:
    completed = subprocess.run(
        [str(hook_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout_val,
        check=False,
    )
except subprocess.TimeoutExpired:
    print(f"timed_out\t124\texceeded {timeout_val:g}s")
    raise SystemExit(0)
except OSError as exc:
    print(f"failed\t126\t{exc}")
    raise SystemExit(0)
detail = (completed.stderr or completed.stdout or "").strip().splitlines()
first_line = detail[0][:240] if detail else ""
status = "succeeded" if completed.returncode == 0 else "failed"
print(f"{status}\t{completed.returncode}\t{first_line}")
PY
    )"
    local status="" rc=1 detail=""
    IFS=$'\t' read -r status rc detail <<< "${outcome}"
    BACKUP_STORAGE_HOOK_STATUS="${status:-failed}"
    BACKUP_STORAGE_HOOK_RC="${rc:-1}"
    if [ "${BACKUP_STORAGE_HOOK_STATUS}" = "succeeded" ]; then
        log "storage backup hook succeeded (${hook})"
        return 0
    fi
    log "WARN: storage backup hook ${BACKUP_STORAGE_HOOK_STATUS} rc=${BACKUP_STORAGE_HOOK_RC}${detail:+ (${detail})}"
    case "${required}" in
        1|true|TRUE|yes|YES|on|ON)
            log "ERROR: LUMEN_STORAGE_BACKUP_HOOK_REQUIRED=1; failing backup due to storage hook error"
            return 1
            ;;
    esac
    return 0
}
