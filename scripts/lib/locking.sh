#!/usr/bin/env bash
# Maintenance and operation lock helpers.
# Sourced by scripts/lib.sh; do not execute directly.

# PID alone is unsafe because it can be reused after an unclean exit.  Linux
# exposes a monotonic process start tick; macOS/BSD fall back to ps lstart.
lumen_pid_start_token() {
    local pid="$1"
    local raw="" token=""
    case "${pid}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if [ -r "/proc/${pid}/stat" ]; then
        raw="$(cat "/proc/${pid}/stat" 2>/dev/null || true)"
        raw="${raw##*) }"
        token="$(printf '%s\n' "${raw}" | awk '{print $20}')"
        case "${token}" in
            ''|*[!0-9]*) ;;
            *) printf 'proc:%s\n' "${token}"; return 0 ;;
        esac
    fi
    raw="$(LC_ALL=C ps -o lstart= -p "${pid}" 2>/dev/null \
        | sed -n '1{s/^[[:space:]]*//;s/[[:space:]]*$//;p;}')"
    [ -n "${raw}" ] || return 1
    printf 'ps:%s\n' "${raw}"
}

lumen_lock_owner_value() {
    local lock_dir="$1"
    local key="$2"
    local owner_file=""
    owner_file="$(lumen_lock_owner_file "${lock_dir}")"
    if [ -n "${owner_file}" ]; then
        sed -n "s/^${key}=//p" "${owner_file}" 2>/dev/null | head -1 || true
    fi
    return 0
}

lumen_lock_owner_file() {
    local lock_dir="$1"
    local candidate=""
    local found=""

    if [ -f "${lock_dir}/owner" ]; then
        printf '%s\n' "${lock_dir}/owner"
        return 0
    fi

    for candidate in "${lock_dir}"/.owner.*; do
        [ -d "${candidate}" ] || continue
        [ -f "${candidate}/owner" ] || continue
        if [ -n "${found}" ]; then
            # Multiple owner records mean the lock is corrupt or mid-recovery.
            # Refuse to guess which process owns it.
            return 0
        fi
        found="${candidate}/owner"
    done
    [ -n "${found}" ] && printf '%s\n' "${found}"
    return 0
}

lumen_lock_owner_pid() {
    local lock_dir="$1"
    local owner_pid=""
    owner_pid="$(lumen_lock_owner_value "${lock_dir}" pid | tr -d '[:space:]')"
    if [ -z "${owner_pid}" ] && [ -f "${lock_dir}/pid" ]; then
        owner_pid="$(tr -d '[:space:]' < "${lock_dir}/pid" 2>/dev/null || true)"
    fi
    printf '%s\n' "${owner_pid}"
    return 0
}

lumen_release_owned_lock_dir() {
    local lock_dir="$1"
    local expected_owner_id="$2"
    local owner_dir=""

    case "${expected_owner_id}" in
        .owner.*) ;;
        *) return 1 ;;
    esac
    owner_dir="${lock_dir}/${expected_owner_id}"
    if ! lumen_lock_dir_owned_by_current_process \
            "${lock_dir}" "${expected_owner_id}"; then
        return 1
    fi

    # The random owner directory is never reused. If an external process
    # replaces lock_dir after the ownership check, these exact paths do not
    # exist in the replacement, so the old owner cannot delete the new lock.
    rm -f "${owner_dir}/owner" 2>/dev/null || return 1
    rmdir "${owner_dir}" 2>/dev/null || return 1
    rmdir "${lock_dir}" 2>/dev/null || return 1
    return 0
}

lumen_release_lock() {
    case "${LUMEN_LOCK_KIND:-}" in
        flock)
            flock -u 6 2>/dev/null || true
            exec 6>&- 2>/dev/null || true
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            ;;
        borrowed)
            ;;
        mkdir)
            if [ -n "${LUMEN_LOCK_LOCAL_PATH:-}" ]; then
                if ! lumen_release_owned_lock_dir \
                        "${LUMEN_LOCK_LOCAL_PATH}" \
                        "${LUMEN_LOCK_OWNER_TOKEN:-}"; then
                    log_warn "root-local maintenance 锁 owner 已变化，拒绝删除：${LUMEN_LOCK_LOCAL_PATH}"
                fi
            fi
            if [ -n "${LUMEN_LOCK_ANCHOR_PATH:-}" ]; then
                if ! lumen_release_owned_lock_dir \
                        "${LUMEN_LOCK_ANCHOR_PATH}" \
                        "${LUMEN_LOCK_ANCHOR_OWNER_TOKEN:-}"; then
                    log_warn "parent-anchor maintenance 锁 owner 已变化，拒绝删除：${LUMEN_LOCK_ANCHOR_PATH}"
                fi
            fi
            ;;
    esac
    LUMEN_LOCK_KIND=""
    LUMEN_LOCK_PATH=""
    LUMEN_LOCK_LOCAL_PATH=""
    LUMEN_LOCK_ANCHOR_PATH=""
    LUMEN_LOCK_OWNER_TOKEN=""
    LUMEN_LOCK_OWNER_CAPABILITY=""
    LUMEN_LOCK_ANCHOR_OWNER_TOKEN=""
    LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY=""
    LUMEN_LOCK_ROOT=""
    LUMEN_LOCK_ROOT_PARENT_PATH=""
    LUMEN_LOCK_ROOT_NAME=""
    LUMEN_LOCK_ROOT_PARENT_DEV=""
    LUMEN_LOCK_ROOT_PARENT_INO=""
    LUMEN_LOCK_ROOT_DEV=""
    LUMEN_LOCK_ROOT_INO=""
    LUMEN_LOCK_ROOT_ANCHOR_KEY=""
}

lumen_lock_dir_stale() {
    local lock_dir="$1"
    local owner_file=""
    local owner_pid="" owner_token="" current_token=""
    owner_file="$(lumen_lock_owner_file "${lock_dir}")"
    if [ -z "${owner_file}" ]; then
        # Legacy backup/restore locks only recorded pid. A dead legacy PID is
        # stale, but automatic removal remains disabled below.
        if [ -f "${lock_dir}/pid" ]; then
            owner_pid="$(lumen_lock_owner_pid "${lock_dir}")"
            case "${owner_pid}" in
                ''|*[!0-9]*) return 1 ;;
            esac
            if ! kill -0 "${owner_pid}" 2>/dev/null; then
                return 0
            fi
        fi
        return 1
    fi
    owner_pid="$(lumen_lock_owner_pid "${lock_dir}")"
    case "${owner_pid}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    if ! kill -0 "${owner_pid}" 2>/dev/null; then
        return 0
    fi
    owner_token="$(lumen_lock_owner_value "${lock_dir}" start_token)"
    if [ -z "${owner_token}" ]; then
        log_warn "锁 owner pid=${owner_pid} 存活但缺少 start_token；保守保留 lock。"
        return 1
    fi
    if ! current_token="$(lumen_pid_start_token "${owner_pid}" 2>/dev/null)"; then
        log_warn "锁 owner pid=${owner_pid} 存活但启动令牌不可读；保守保留 lock。"
        return 1
    fi
    [ "${current_token}" != "${owner_token}" ]
}

lumen_write_lock_owner() {
    local owner_dir="$1"
    local label_key="$2"
    local label_value="$3"
    local owner_id="${owner_dir##*/}"
    local owner_tmp="${owner_dir}/.owner.$$"
    local start_token=""
    local capability_pair=""
    local capability=""
    local capability_sha256=""
    case "${label_key}" in
        ''|*[!A-Za-z0-9_]*) return 1 ;;
    esac
    case "${label_value}" in
        *$'\n'*|*$'\r'*) return 1 ;;
    esac
    start_token="$(lumen_pid_start_token "$$")" || return 1
    capability_pair="$(
        python3 - <<'PY'
import hashlib
import secrets

secret = secrets.token_hex(32)
print(f"{secret}\t{hashlib.sha256(secret.encode('ascii')).hexdigest()}")
PY
    )" || return 1
    IFS=$'\t' read -r capability capability_sha256 <<< "${capability_pair}"
    [ -n "${capability}" ] && [ -n "${capability_sha256}" ] || return 1
    if ! (
        umask 077
        {
            printf 'pid=%s\n' "$$"
            printf 'start_token=%s\n' "${start_token}"
            printf 'owner_id=%s\n' "${owner_id}"
            printf 'capability_sha256=%s\n' "${capability_sha256}"
            printf '%s=%s\n' "${label_key}" "${label_value}"
            printf 'started_at=%s\n' "$(date -u +%FT%TZ 2>/dev/null || date)"
        } > "${owner_tmp}"
    ) || ! mv -f "${owner_tmp}" "${owner_dir}/owner"; then
        rm -f "${owner_tmp}" 2>/dev/null || true
        return 1
    fi
    LUMEN_LAST_LOCK_OWNER_TOKEN="${owner_id}"
    LUMEN_LAST_LOCK_OWNER_CAPABILITY="${capability}"
}

lumen_write_flock_lock_owner() {
    local fd="$1"
    local script_name="$2"
    local start_token=""
    local capability_pair=""
    local capability=""
    local capability_sha256=""
    start_token="$(lumen_pid_start_token "$$")" || return 1
    capability_pair="$(
        python3 - <<'PY'
import hashlib
import secrets

secret = secrets.token_hex(32)
print(f"{secret}\t{hashlib.sha256(secret.encode('ascii')).hexdigest()}")
PY
    )" || return 1
    IFS=$'\t' read -r capability capability_sha256 <<< "${capability_pair}"
    [ -n "${capability}" ] && [ -n "${capability_sha256}" ] || return 1
    python3 - "${fd}" "$$" "${start_token}" "${script_name}" \
            "${capability_sha256}" <<'PY' || return 1
import os
import stat
import sys

fd = int(sys.argv[1])
metadata = os.fstat(fd)
if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
    raise SystemExit(1)
mode = stat.S_IMODE(metadata.st_mode)
if mode & stat.S_IWOTH or not mode & (stat.S_IWUSR | stat.S_IWGRP):
    raise SystemExit(1)
payload = (
    f"pid={sys.argv[2]}\n"
    f"start_token={sys.argv[3]}\n"
    "owner_id=flock\n"
    f"capability_sha256={sys.argv[5]}\n"
    f"script={sys.argv[4]}\n"
).encode("ascii")
os.ftruncate(fd, 0)
os.lseek(fd, 0, os.SEEK_SET)
os.write(fd, payload)
os.fsync(fd)
PY
    LUMEN_LOCK_OWNER_TOKEN="flock"
    LUMEN_LOCK_OWNER_CAPABILITY="${capability}"
    return 0
}

lumen_try_create_owned_lock_dir() {
    local lock_dir="$1"
    local label_key="$2"
    local label_value="$3"
    local owner_dir=""
    local owner_pid=""
    LUMEN_LAST_LOCK_OWNER_TOKEN=""
    LUMEN_LAST_LOCK_OWNER_CAPABILITY=""
    # shellcheck disable=SC2034  # Public status consumed by backup/restore callers.
    LUMEN_LAST_LOCK_RECLAIMED=0
    LUMEN_LAST_LOCK_STALE=0

    if (umask 077; mkdir "${lock_dir}") 2>/dev/null; then
        owner_dir="$(mktemp -d "${lock_dir}/.owner.XXXXXXXXXX" 2>/dev/null || true)"
        if [ -n "${owner_dir}" ] \
                && lumen_write_lock_owner \
                    "${owner_dir}" "${label_key}" "${label_value}"; then
            return 0
        fi
        if [ -n "${owner_dir}" ]; then
            rm -f "${owner_dir}/.owner.$$" 2>/dev/null || true
            rmdir "${owner_dir}" 2>/dev/null || true
        fi
        rmdir "${lock_dir}" 2>/dev/null || true
        return 1
    fi

    owner_pid="$(lumen_lock_owner_pid "${lock_dir}")"
    if lumen_lock_dir_stale "${lock_dir}"; then
        # POSIX shell has no cross-platform compare-and-delete primitive for a
        # directory. Check-then-rm/rename can remove a newly acquired lock, so
        # the no-flock fallback refuses automatic stale reclamation.
        LUMEN_LAST_LOCK_STALE=1
        # shellcheck disable=SC2034  # Public diagnostic consumed by callers.
        LUMEN_LAST_STALE_LOCK_PID="${owner_pid}"
    fi
    return 1
}

lumen_lock_dir_owned_by_current_process() {
    local lock_dir="$1"
    local expected_owner_id="$2"
    local owner_file=""
    local owner_pid owner_id owner_token current_token
    case "${expected_owner_id}" in
        .owner.*) ;;
        *) return 1 ;;
    esac
    owner_file="${lock_dir}/${expected_owner_id}/owner"
    [ -f "${owner_file}" ] || return 1
    owner_pid="$(sed -n 's/^pid=//p' "${owner_file}" 2>/dev/null \
        | head -1 | tr -d '[:space:]' || true)"
    owner_id="$(sed -n 's/^owner_id=//p' "${owner_file}" 2>/dev/null \
        | head -1 || true)"
    owner_token="$(sed -n 's/^start_token=//p' "${owner_file}" 2>/dev/null \
        | head -1 || true)"
    [ "${owner_pid}" = "$$" ] || return 1
    [ "${owner_id}" = "${expected_owner_id}" ] || return 1
    current_token="$(lumen_pid_start_token "$$" 2>/dev/null)" || return 1
    [ "${current_token}" = "${owner_token}" ]
}

lumen_pid_is_ancestor() {
    local ancestor="$1"
    local descendant="$2"
    local parent=""
    case "${ancestor}:${descendant}" in
        *[!0-9:]*|:*|*:) return 1 ;;
    esac
    while [ "${descendant}" -gt 1 ] 2>/dev/null; do
        [ "${descendant}" = "${ancestor}" ] && return 0
        parent="$(
            ps -o ppid= -p "${descendant}" 2>/dev/null \
                | tr -d '[:space:]'
        )"
        case "${parent}" in
            ''|*[!0-9]*) return 1 ;;
        esac
        [ "${parent}" = "${descendant}" ] && return 1
        descendant="${parent}"
    done
    [ "${descendant}" = "${ancestor}" ]
}

lumen_capture_maintenance_root_binding() {
    local root="$1"
    local binding=""
    binding="$(
        python3 - "${root}" <<'PY'
import hashlib
import os
import stat
import sys

raw = sys.argv[1]
if (
    not raw.startswith("/")
    or raw == "/"
    or any(ord(character) < 32 for character in raw)
):
    raise SystemExit(1)
root = os.path.normpath(raw)
parent = os.path.dirname(root)
name = os.path.basename(root)
if not name or name in {".", ".."}:
    raise SystemExit(1)
flags = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
parent_before = os.stat(parent, follow_symlinks=False)
if not stat.S_ISDIR(parent_before.st_mode):
    raise SystemExit(1)
parent_fd = os.open(parent, flags)
try:
    parent_opened = os.fstat(parent_fd)
    if (
        not stat.S_ISDIR(parent_opened.st_mode)
        or (parent_before.st_dev, parent_before.st_ino)
        != (parent_opened.st_dev, parent_opened.st_ino)
    ):
        raise SystemExit(1)
    root_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
        raise SystemExit(1)
    root_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        root_opened = os.fstat(root_fd)
        root_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        parent_after = os.stat(parent, follow_symlinks=False)
        if (
            (root_before.st_dev, root_before.st_ino)
            != (root_opened.st_dev, root_opened.st_ino)
            or (root_before.st_dev, root_before.st_ino)
            != (root_after.st_dev, root_after.st_ino)
            or (parent_before.st_dev, parent_before.st_ino)
            != (parent_after.st_dev, parent_after.st_ino)
        ):
            raise SystemExit(1)
    finally:
        os.close(root_fd)
finally:
    os.close(parent_fd)
anchor_key = hashlib.sha256(root.encode("utf-8")).hexdigest()[:32]
print(
    "\t".join(
        (
            parent,
            name,
            str(parent_before.st_dev),
            str(parent_before.st_ino),
            str(root_before.st_dev),
            str(root_before.st_ino),
            anchor_key,
        )
    )
)
PY
    )" || return 1
    IFS=$'\t' read -r \
        LUMEN_CAPTURED_ROOT_PARENT_PATH \
        LUMEN_CAPTURED_ROOT_NAME \
        LUMEN_CAPTURED_ROOT_PARENT_DEV \
        LUMEN_CAPTURED_ROOT_PARENT_INO \
        LUMEN_CAPTURED_ROOT_DEV \
        LUMEN_CAPTURED_ROOT_INO \
        LUMEN_CAPTURED_ROOT_ANCHOR_KEY <<< "${binding}"
    [ -n "${LUMEN_CAPTURED_ROOT_PARENT_PATH:-}" ] \
        && [ -n "${LUMEN_CAPTURED_ROOT_NAME:-}" ] \
        && [ -n "${LUMEN_CAPTURED_ROOT_PARENT_DEV:-}" ] \
        && [ -n "${LUMEN_CAPTURED_ROOT_PARENT_INO:-}" ] \
        && [ -n "${LUMEN_CAPTURED_ROOT_DEV:-}" ] \
        && [ -n "${LUMEN_CAPTURED_ROOT_INO:-}" ] \
        && [ -n "${LUMEN_CAPTURED_ROOT_ANCHOR_KEY:-}" ]
}

lumen_set_maintenance_root_binding() {
    local root="$1"
    lumen_capture_maintenance_root_binding "${root}" || return 1
    LUMEN_LOCK_ROOT="${root}"
    LUMEN_LOCK_ROOT_PARENT_PATH="${LUMEN_CAPTURED_ROOT_PARENT_PATH}"
    LUMEN_LOCK_ROOT_NAME="${LUMEN_CAPTURED_ROOT_NAME}"
    LUMEN_LOCK_ROOT_PARENT_DEV="${LUMEN_CAPTURED_ROOT_PARENT_DEV}"
    LUMEN_LOCK_ROOT_PARENT_INO="${LUMEN_CAPTURED_ROOT_PARENT_INO}"
    LUMEN_LOCK_ROOT_DEV="${LUMEN_CAPTURED_ROOT_DEV}"
    LUMEN_LOCK_ROOT_INO="${LUMEN_CAPTURED_ROOT_INO}"
    LUMEN_LOCK_ROOT_ANCHOR_KEY="${LUMEN_CAPTURED_ROOT_ANCHOR_KEY}"
}

lumen_verify_maintenance_root_binding() {
    local root="$1"
    lumen_capture_maintenance_root_binding "${root}" || return 1
    [ "${root}" = "${LUMEN_LOCK_ROOT:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_PARENT_PATH}" = \
            "${LUMEN_LOCK_ROOT_PARENT_PATH:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_NAME}" = "${LUMEN_LOCK_ROOT_NAME:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_PARENT_DEV}" = \
            "${LUMEN_LOCK_ROOT_PARENT_DEV:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_PARENT_INO}" = \
            "${LUMEN_LOCK_ROOT_PARENT_INO:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_DEV}" = "${LUMEN_LOCK_ROOT_DEV:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_INO}" = "${LUMEN_LOCK_ROOT_INO:-}" ] \
        && [ "${LUMEN_CAPTURED_ROOT_ANCHOR_KEY}" = \
            "${LUMEN_LOCK_ROOT_ANCHOR_KEY:-}" ]
}

lumen_clear_borrowed_maintenance_lock() {
    unset \
        LUMEN_BORROWED_MAINTENANCE_LOCK_KIND \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT \
        LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH \
        LUMEN_BORROWED_MAINTENANCE_ROOT_NAME \
        LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV \
        LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO \
        LUMEN_BORROWED_MAINTENANCE_ROOT_DEV \
        LUMEN_BORROWED_MAINTENANCE_ROOT_INO \
        LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_FD \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_OWNER_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_CAPABILITY \
        LUMEN_BORROWED_MAINTENANCE_LOCK_PATH \
        LUMEN_BORROWED_MAINTENANCE_LOCK_FD \
        LUMEN_BORROWED_MAINTENANCE_LOCK_DEV \
        LUMEN_BORROWED_MAINTENANCE_LOCK_INO \
        LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH \
        LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY \
        LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID \
        LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY
}

lumen_flock_fd_identity() {
    local fd="$1"
    local path="$2"
    local expected_type="${3:-file}"
    python3 - "${fd}" "${path}" "${expected_type}" <<'PY'
import fcntl
import os
from pathlib import Path
import stat
import sys

try:
    fd = int(sys.argv[1])
except ValueError:
    raise SystemExit(1)
path = Path(sys.argv[2])
expected_type = sys.argv[3]
opened = os.fstat(fd)
current = os.stat(path, follow_symlinks=False)
if expected_type == "file":
    valid_type = stat.S_ISREG(opened.st_mode) and stat.S_ISREG(current.st_mode)
elif expected_type == "directory":
    valid_type = stat.S_ISDIR(opened.st_mode) and stat.S_ISDIR(current.st_mode)
else:
    raise SystemExit(1)
if not valid_type or (
    opened.st_dev,
    opened.st_ino,
) != (current.st_dev, current.st_ino):
    raise SystemExit(1)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print(f"{opened.st_dev}\t{opened.st_ino}")
PY
}

lumen_maintenance_lock_path_safe() {
    local path="$1"
    local expected_type="$2"
    [ ! -L "${path}" ] || return 1
    if [ ! -e "${path}" ]; then
        return 0
    fi
    case "${expected_type}" in
        file) [ -f "${path}" ] ;;
        directory) [ -d "${path}" ] ;;
        *) return 1 ;;
    esac
}

lumen_lock_path_identity() {
    local path="$1"
    local expected_type="$2"
    python3 - "${path}" "${expected_type}" <<'PY'
import os
import stat
import sys

metadata = os.stat(sys.argv[1], follow_symlinks=False)
if sys.argv[2] == "file":
    valid = stat.S_ISREG(metadata.st_mode)
elif sys.argv[2] == "directory":
    valid = stat.S_ISDIR(metadata.st_mode)
else:
    valid = False
if not valid:
    raise SystemExit(1)
print(f"{metadata.st_dev}\t{metadata.st_ino}")
PY
}

lumen_export_borrowed_maintenance_lock() {
    local root="$1"
    local expected_file="${root}/.lumen-maintenance.lock"
    local expected_local_dir="${expected_file}.d"
    local expected_anchor_dir=""
    local anchor_identity=""
    local local_identity=""
    local owner_file=""
    local owner_pid=""
    local owner_start_token=""
    local owner_id=""
    local expected_hash=""
    local actual_hash=""
    local current_start_token=""

    lumen_clear_borrowed_maintenance_lock
    lumen_verify_maintenance_root_binding "${root}" || return 1
    expected_anchor_dir="$(
        printf '%s/.lumen-maintenance.%s.lock.d' \
            "${LUMEN_LOCK_ROOT_PARENT_PATH}" \
            "${LUMEN_LOCK_ROOT_ANCHOR_KEY}"
    )"

    LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT="${root}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH="${LUMEN_LOCK_ROOT_PARENT_PATH}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_NAME="${LUMEN_LOCK_ROOT_NAME}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV="${LUMEN_LOCK_ROOT_PARENT_DEV}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO="${LUMEN_LOCK_ROOT_PARENT_INO}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_DEV="${LUMEN_LOCK_ROOT_DEV}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_INO="${LUMEN_LOCK_ROOT_INO}"
    LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY="${LUMEN_LOCK_ROOT_ANCHOR_KEY}"
    case "${LUMEN_LOCK_KIND:-}" in
        flock)
            [ "${LUMEN_LOCK_PATH:-}" = "${expected_file}" ] || return 1
            [ "${LUMEN_LOCK_LOCAL_PATH:-}" = "${expected_file}" ] || return 1
            [ "${LUMEN_LOCK_ANCHOR_PATH:-}" = \
                "${LUMEN_LOCK_ROOT_PARENT_PATH}" ] || return 1
            anchor_identity="$(
                lumen_flock_fd_identity \
                    9 "${LUMEN_LOCK_ROOT_PARENT_PATH}" directory
            )" || return 1
            local_identity="$(
                lumen_flock_fd_identity 6 "${expected_file}" file
            )" || return 1
            IFS=$'\t' read -r \
                LUMEN_BORROWED_MAINTENANCE_LOCK_DEV \
                LUMEN_BORROWED_MAINTENANCE_LOCK_INO <<< "${local_identity}"
            IFS=$'\t' read -r \
                LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV \
                LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO \
                <<< "${anchor_identity}"
            owner_file="${expected_file}"
            owner_pid="$(sed -n 's/^pid=//p' "${owner_file}" | head -1)"
            owner_start_token="$(
                sed -n 's/^start_token=//p' "${owner_file}" | head -1
            )"
            owner_id="$(sed -n 's/^owner_id=//p' "${owner_file}" | head -1)"
            expected_hash="$(
                sed -n 's/^capability_sha256=//p' "${owner_file}" | head -1
            )"
            [ "${owner_id}" = "flock" ] \
                && [ "${LUMEN_LOCK_OWNER_TOKEN:-}" = "flock" ] \
                && [ -n "${LUMEN_LOCK_OWNER_CAPABILITY:-}" ] \
                && [ -n "${owner_pid}" ] \
                && [ -n "${owner_start_token}" ] \
                && [ -n "${expected_hash}" ] || return 1
            current_start_token="$(
                lumen_pid_start_token "${owner_pid}" 2>/dev/null
            )" || return 1
            [ "${current_start_token}" = "${owner_start_token}" ] || return 1
            lumen_pid_is_ancestor "${owner_pid}" "$$" || return 1
            actual_hash="$(
                python3 - "${LUMEN_LOCK_OWNER_CAPABILITY}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest())
PY
            )" || return 1
            [ "${actual_hash}" = "${expected_hash}" ] || return 1
            LUMEN_BORROWED_MAINTENANCE_LOCK_KIND="flock"
            LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH="${LUMEN_LOCK_ROOT_PARENT_PATH}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_FD=9
            LUMEN_BORROWED_MAINTENANCE_LOCK_PATH="${expected_file}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_FD=6
            LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH="${expected_file}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN="flock"
            LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY="${LUMEN_LOCK_OWNER_CAPABILITY}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN="flock"
            LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID="${owner_pid}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN="${owner_start_token}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY="${LUMEN_LOCK_OWNER_CAPABILITY}"
            ;;
        mkdir)
            [ "${LUMEN_LOCK_PATH:-}" = "${expected_anchor_dir}" ] || return 1
            [ "${LUMEN_LOCK_ANCHOR_PATH:-}" = "${expected_anchor_dir}" ] \
                || return 1
            [ "${LUMEN_LOCK_LOCAL_PATH:-}" = "${expected_local_dir}" ] \
                || return 1
            [ -n "${LUMEN_LOCK_ANCHOR_OWNER_TOKEN:-}" ] \
                && [ -n "${LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY:-}" ] \
                && [ -n "${LUMEN_LOCK_OWNER_TOKEN:-}" ] \
                && [ -n "${LUMEN_LOCK_OWNER_CAPABILITY:-}" ] || return 1
            lumen_lock_dir_owned_by_current_process \
                "${expected_anchor_dir}" \
                "${LUMEN_LOCK_ANCHOR_OWNER_TOKEN}" || return 1
            lumen_lock_dir_owned_by_current_process \
                "${expected_local_dir}" "${LUMEN_LOCK_OWNER_TOKEN}" || return 1
            anchor_identity="$(
                lumen_lock_path_identity "${expected_anchor_dir}" directory
            )" || return 1
            IFS=$'\t' read -r \
                LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV \
                LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO \
                <<< "${anchor_identity}"
            owner_file="${expected_anchor_dir}/${LUMEN_LOCK_ANCHOR_OWNER_TOKEN}/owner"
            owner_pid="$(sed -n 's/^pid=//p' "${owner_file}" | head -1)"
            owner_start_token="$(
                sed -n 's/^start_token=//p' "${owner_file}" | head -1
            )"
            expected_hash="$(
                sed -n 's/^capability_sha256=//p' "${owner_file}" | head -1
            )"
            actual_hash="$(
                python3 - "${LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest())
PY
            )" || return 1
            [ "${actual_hash}" = "${expected_hash}" ] || return 1
            LUMEN_BORROWED_MAINTENANCE_LOCK_KIND="mkdir"
            LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH="${expected_anchor_dir}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_PATH="${expected_anchor_dir}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH="${expected_local_dir}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN="${LUMEN_LOCK_OWNER_TOKEN}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY="${LUMEN_LOCK_OWNER_CAPABILITY}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN="${LUMEN_LOCK_ANCHOR_OWNER_TOKEN}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID="${owner_pid}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN="${owner_start_token}"
            LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY="${LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY}"
            ;;
        *)
            return 1
            ;;
    esac
    export \
        LUMEN_BORROWED_MAINTENANCE_LOCK_KIND \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT \
        LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH \
        LUMEN_BORROWED_MAINTENANCE_ROOT_NAME \
        LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV \
        LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO \
        LUMEN_BORROWED_MAINTENANCE_ROOT_DEV \
        LUMEN_BORROWED_MAINTENANCE_ROOT_INO \
        LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_FD \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV \
        LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO \
        LUMEN_BORROWED_MAINTENANCE_LOCK_PATH \
        LUMEN_BORROWED_MAINTENANCE_LOCK_FD \
        LUMEN_BORROWED_MAINTENANCE_LOCK_DEV \
        LUMEN_BORROWED_MAINTENANCE_LOCK_INO \
        LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH \
        LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY \
        LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID \
        LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN \
        LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY
}

lumen_verify_borrowed_maintenance_lock() {
    local root="$1"
    local kind="${LUMEN_BORROWED_MAINTENANCE_LOCK_KIND:-}"
    local expected_file="${root}/.lumen-maintenance.lock"
    local expected_local_dir="${expected_file}.d"
    local expected_anchor_dir=""
    local anchor_identity=""
    local local_identity=""
    local actual_dev=""
    local actual_ino=""
    local owner_token="${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN:-}"
    local owner_file=""
    local owner_pid=""
    local owner_start_token=""
    local owner_id=""
    local expected_hash=""
    local actual_hash=""
    local current_start_token=""
    local local_owner_token=""
    local local_owner_file=""
    local local_owner_pid=""
    local local_owner_start_token=""
    local local_owner_id=""
    local local_expected_hash=""
    local local_actual_hash=""

    [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_ROOT:-}" = "${root}" ] || return 1
    LUMEN_LOCK_ROOT="${root}"
    LUMEN_LOCK_ROOT_PARENT_PATH="${LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_PATH:-}"
    LUMEN_LOCK_ROOT_NAME="${LUMEN_BORROWED_MAINTENANCE_ROOT_NAME:-}"
    LUMEN_LOCK_ROOT_PARENT_DEV="${LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_DEV:-}"
    LUMEN_LOCK_ROOT_PARENT_INO="${LUMEN_BORROWED_MAINTENANCE_ROOT_PARENT_INO:-}"
    LUMEN_LOCK_ROOT_DEV="${LUMEN_BORROWED_MAINTENANCE_ROOT_DEV:-}"
    LUMEN_LOCK_ROOT_INO="${LUMEN_BORROWED_MAINTENANCE_ROOT_INO:-}"
    LUMEN_LOCK_ROOT_ANCHOR_KEY="${LUMEN_BORROWED_MAINTENANCE_ROOT_ANCHOR_KEY:-}"
    lumen_verify_maintenance_root_binding "${root}" || return 1
    expected_anchor_dir="$(
        printf '%s/.lumen-maintenance.%s.lock.d' \
            "${LUMEN_LOCK_ROOT_PARENT_PATH}" \
            "${LUMEN_LOCK_ROOT_ANCHOR_KEY}"
    )"
    case "${kind}" in
        flock)
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_PATH:-}" = "${expected_file}" ] \
                || return 1
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH:-}" = \
                "${expected_file}" ] || return 1
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH:-}" = \
                "${LUMEN_LOCK_ROOT_PARENT_PATH}" ] || return 1
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_FD:-}" = "9" ] \
                || return 1
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_FD:-}" = "6" ] || return 1
            anchor_identity="$(
                lumen_flock_fd_identity \
                    9 "${LUMEN_LOCK_ROOT_PARENT_PATH}" directory
            )" || return 1
            IFS=$'\t' read -r actual_dev actual_ino <<< "${anchor_identity}"
            [ "${actual_dev}" = \
                "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV:-}" ] \
                && [ "${actual_ino}" = \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO:-}" ] \
                || return 1
            local_identity="$(
                lumen_flock_fd_identity 6 "${expected_file}" file
            )" || return 1
            IFS=$'\t' read -r actual_dev actual_ino <<< "${local_identity}"
            [ "${actual_dev}" = "${LUMEN_BORROWED_MAINTENANCE_LOCK_DEV:-}" ] \
                && [ "${actual_ino}" = "${LUMEN_BORROWED_MAINTENANCE_LOCK_INO:-}" ] \
                || return 1
            [ "${owner_token}" = "flock" ] || return 1
            owner_file="${expected_file}"
            owner_pid="$(sed -n 's/^pid=//p' "${owner_file}" | head -1)"
            owner_start_token="$(
                sed -n 's/^start_token=//p' "${owner_file}" | head -1
            )"
            owner_id="$(sed -n 's/^owner_id=//p' "${owner_file}" | head -1)"
            expected_hash="$(
                sed -n 's/^capability_sha256=//p' "${owner_file}" | head -1
            )"
            [ "${owner_pid}" = "${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID:-}" ] \
                && [ "${owner_start_token}" = \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN:-}" ] \
                && [ "${owner_id}" = "flock" ] \
                && [ -n "${expected_hash}" ] || return 1
            current_start_token="$(
                lumen_pid_start_token "${owner_pid}" 2>/dev/null
            )" || return 1
            [ "${current_start_token}" = "${owner_start_token}" ] || return 1
            lumen_pid_is_ancestor "${owner_pid}" "$$" || return 1
            actual_hash="$(
                python3 - "${LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY:-}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest())
PY
            )" || return 1
            [ "${actual_hash}" = "${expected_hash}" ] || return 1
            ;;
        mkdir)
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_PATH:-}" = \
                "${expected_anchor_dir}" ] \
                || return 1
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH:-}" = \
                "${expected_anchor_dir}" ] || return 1
            [ "${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH:-}" = \
                "${expected_local_dir}" ] || return 1
            case "${owner_token}" in
                .owner.*) ;;
                *) return 1 ;;
            esac
            owner_file="${expected_anchor_dir}/${owner_token}/owner"
            [ ! -L "${expected_anchor_dir}" ] \
                && [ ! -L "${expected_anchor_dir}/${owner_token}" ] \
                && [ ! -L "${owner_file}" ] \
                && [ -f "${owner_file}" ] || return 1
            local_owner_token="$(
                printf '%s' \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN:-}"
            )"
            case "${local_owner_token}" in
                .owner.*) ;;
                *) return 1 ;;
            esac
            local_owner_file="${expected_local_dir}/${local_owner_token}/owner"
            [ ! -L "${expected_local_dir}" ] \
                && [ ! -L "${expected_local_dir}/${local_owner_token}" ] \
                && [ ! -L "${local_owner_file}" ] \
                && [ -f "${local_owner_file}" ] || return 1
            local_owner_pid="$(
                sed -n 's/^pid=//p' "${local_owner_file}" | head -1
            )"
            local_owner_start_token="$(
                sed -n 's/^start_token=//p' "${local_owner_file}" | head -1
            )"
            local_owner_id="$(
                sed -n 's/^owner_id=//p' "${local_owner_file}" | head -1
            )"
            local_expected_hash="$(
                sed -n 's/^capability_sha256=//p' \
                    "${local_owner_file}" | head -1
            )"
            [ "${local_owner_pid}" = \
                "${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID:-}" ] \
                && [ "${local_owner_start_token}" = \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN:-}" ] \
                && [ "${local_owner_id}" = "${local_owner_token}" ] \
                && [ -n "${local_expected_hash}" ] || return 1
            local_actual_hash="$(
                python3 - \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY:-}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest())
PY
            )" || return 1
            [ "${local_actual_hash}" = "${local_expected_hash}" ] || return 1
            anchor_identity="$(
                lumen_lock_path_identity "${expected_anchor_dir}" directory
            )" || return 1
            IFS=$'\t' read -r actual_dev actual_ino <<< "${anchor_identity}"
            [ "${actual_dev}" = \
                "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_DEV:-}" ] \
                && [ "${actual_ino}" = \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_INO:-}" ] \
                || return 1
            owner_pid="$(sed -n 's/^pid=//p' "${owner_file}" | head -1)"
            owner_start_token="$(
                sed -n 's/^start_token=//p' "${owner_file}" | head -1
            )"
            owner_id="$(sed -n 's/^owner_id=//p' "${owner_file}" | head -1)"
            expected_hash="$(
                sed -n 's/^capability_sha256=//p' "${owner_file}" | head -1
            )"
            [ "${owner_pid}" = "${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID:-}" ] \
                && [ "${owner_start_token}" = \
                    "${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_START_TOKEN:-}" ] \
                && [ "${owner_id}" = "${owner_token}" ] \
                && [ -n "${expected_hash}" ] || return 1
            current_start_token="$(
                lumen_pid_start_token "${owner_pid}" 2>/dev/null
            )" || return 1
            [ "${current_start_token}" = "${owner_start_token}" ] || return 1
            lumen_pid_is_ancestor "${owner_pid}" "$$" || return 1
            actual_hash="$(
                python3 - "${LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY:-}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("ascii")).hexdigest())
PY
            )" || return 1
            [ "${actual_hash}" = "${expected_hash}" ] || return 1
            ;;
        *)
            return 1
            ;;
    esac
    LUMEN_LOCK_KIND="borrowed"
    LUMEN_LOCK_PATH="${LUMEN_BORROWED_MAINTENANCE_LOCK_PATH}"
    LUMEN_LOCK_ANCHOR_PATH="${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH}"
    LUMEN_LOCK_LOCAL_PATH="${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH}"
    return 0
}

lumen_adopt_borrowed_maintenance_lock() {
    local root="$1"
    local borrowed_kind="${LUMEN_BORROWED_MAINTENANCE_LOCK_KIND:-}"
    local borrowed_path="${LUMEN_BORROWED_MAINTENANCE_LOCK_PATH:-}"
    local borrowed_owner_token="${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN:-}"
    local borrowed_owner_pid="${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_PID:-}"
    local borrowed_capability="${LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY:-}"
    local borrowed_anchor_path="${LUMEN_BORROWED_MAINTENANCE_LOCK_ANCHOR_PATH:-}"
    local borrowed_anchor_owner_token="${LUMEN_BORROWED_MAINTENANCE_LOCK_OWNER_TOKEN:-}"
    local borrowed_anchor_capability="${LUMEN_BORROWED_MAINTENANCE_LOCK_CAPABILITY:-}"
    local borrowed_local_path="${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_PATH:-}"
    local borrowed_local_owner_token="${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_OWNER_TOKEN:-}"
    local borrowed_local_capability="${LUMEN_BORROWED_MAINTENANCE_LOCK_LOCAL_CAPABILITY:-}"

    # Re-exec preserves the process identity. Only that exact owner may turn a
    # borrowed proof back into a releasable primary lock.
    [ "${borrowed_owner_pid}" = "$$" ] || return 1
    lumen_verify_borrowed_maintenance_lock "${root}" || return 1
    case "${borrowed_kind}" in
        flock|mkdir) ;;
        *) return 1 ;;
    esac
    LUMEN_LOCK_KIND="${borrowed_kind}"
    LUMEN_LOCK_PATH="${borrowed_path}"
    LUMEN_LOCK_ANCHOR_PATH="${borrowed_anchor_path}"
    LUMEN_LOCK_LOCAL_PATH="${borrowed_local_path}"
    if [ "${borrowed_kind}" = "mkdir" ]; then
        LUMEN_LOCK_ANCHOR_OWNER_TOKEN="${borrowed_anchor_owner_token}"
        LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY="${borrowed_anchor_capability}"
        LUMEN_LOCK_OWNER_TOKEN="${borrowed_local_owner_token}"
        LUMEN_LOCK_OWNER_CAPABILITY="${borrowed_local_capability}"
    else
        LUMEN_LOCK_OWNER_TOKEN="${borrowed_owner_token}"
        LUMEN_LOCK_OWNER_CAPABILITY="${borrowed_capability}"
    fi
    lumen_clear_borrowed_maintenance_lock
    trap 'lumen_release_lock' EXIT
    return 0
}

lumen_restore_saved_trap() {
    local saved_trap="$1"
    local signal="$2"
    if [ -n "${saved_trap}" ]; then
        # trap -p emits shell code that restores the exact prior disposition.
        eval "${saved_trap}"
    else
        trap - "${signal}"
    fi
}

lumen_capture_current_trap() {
    local signal="$1"
    local capture_file="$2"
    local line=""
    LUMEN_CAPTURED_TRAP=""
    if ! trap -p "${signal}" > "${capture_file}"; then
        return 1
    fi
    while IFS= read -r line || [ -n "${line}" ]; do
        if [ -n "${LUMEN_CAPTURED_TRAP}" ]; then
            LUMEN_CAPTURED_TRAP="${LUMEN_CAPTURED_TRAP}
${line}"
        else
            LUMEN_CAPTURED_TRAP="${line}"
        fi
    done < "${capture_file}"
    return 0
}

lumen_saved_trap_is_ignore() {
    local saved_trap="$1"
    local signal="$2"
    case "${saved_trap}" in
        "trap -- '' SIG${signal}"|"trap -- '' ${signal}") return 0 ;;
    esac
    return 1
}

lumen_saved_trap_command() {
    local saved_trap="$1"
    case "${saved_trap}" in
        "trap -- "*) ;;
        *) return 1 ;;
    esac
    eval "set -- ${saved_trap#trap -- }"
    [ "$#" -ge 2 ] || return 1
    LUMEN_CAPTURED_TRAP_COMMAND="$1"
}

lumen_return_status() {
    return "$1"
}

lumen_with_lock_release_owner() {
    local lock_dir="$1"
    local owner_token="$2"
    local owner_dir=""
    case "${owner_token}" in
        .owner.*) ;;
        *) return 1 ;;
    esac
    owner_dir="${lock_dir}/${owner_token}"
    if [ ! -e "${owner_dir}" ]; then
        # A signal path and the normal return path can both attempt cleanup.
        # Absence of our unique token also covers a successor lock safely.
        return 0
    fi
    if ! lumen_release_owned_lock_dir "${lock_dir}" "${owner_token}"; then
        log_warn "更新锁 owner 已变化，拒绝删除：${lock_dir}"
        return 1
    fi
    return 0
}

lumen_run_saved_exit_trap() {
    local saved_exit="$1"
    local rc="$2"
    local saved_command=""
    [ -n "${saved_exit}" ] || return 0
    lumen_saved_trap_command "${saved_exit}" || return 0
    saved_command="${LUMEN_CAPTURED_TRAP_COMMAND}"
    (
        set +e
        lumen_return_status "${rc}"
        eval "${saved_command}"
    ) || true
    return 0
}

lumen_with_lock_exit_trap() {
    local rc="$1"
    local lock_dir="$2"
    local owner_token="$3"
    local saved_exit="$4"
    local saved_int="$5"
    local saved_term="$6"

    lumen_with_lock_release_owner "${lock_dir}" "${owner_token}" || true
    lumen_restore_saved_trap "${saved_exit}" EXIT
    lumen_restore_saved_trap "${saved_int}" INT
    lumen_restore_saved_trap "${saved_term}" TERM
    lumen_run_saved_exit_trap "${saved_exit}" "${rc}"
    return "${rc}"
}

lumen_with_lock_signal_trap() {
    local interrupted_rc="$1"
    local signal="$2"
    local rc="$3"
    local lock_dir="$4"
    local owner_token="$5"
    local saved_exit="$6"
    local saved_int="$7"
    local saved_term="$8"
    local saved_signal=""
    local saved_command=""

    case "${signal}" in
        INT) saved_signal="${saved_int}" ;;
        TERM) saved_signal="${saved_term}" ;;
        *) return "${rc}" ;;
    esac

    # Keep our EXIT trap installed while replaying the caller's disposition.
    # Default termination or a custom handler that exits will therefore clean
    # this exact owner and then chain the caller's EXIT handler. A custom
    # handler that returns must leave the command and lock running.
    lumen_restore_saved_trap "${saved_signal}" "${signal}"
    if [ -n "${saved_signal}" ]; then
        if lumen_saved_trap_command "${saved_signal}"; then
            saved_command="${LUMEN_CAPTURED_TRAP_COMMAND}"
            lumen_return_status "${interrupted_rc}" || true
            eval "${saved_command}"
        fi
        lumen_install_with_lock_signal_trap \
            "${signal}" "${rc}" "${lock_dir}" "${owner_token}" \
            "${saved_exit}" "${saved_int}" "${saved_term}"
    else
        kill -s "${signal}" "$$"
    fi
    return 0
}

lumen_install_with_lock_signal_trap() {
    local signal="$1"
    local rc="$2"
    local lock_dir="$3"
    local owner_token="$4"
    local saved_exit="$5"
    local saved_int="$6"
    local saved_term="$7"
    local saved_signal=""
    local handler=""

    case "${signal}" in
        INT) saved_signal="${saved_int}" ;;
        TERM) saved_signal="${saved_term}" ;;
        *) return 1 ;;
    esac
    if lumen_saved_trap_is_ignore "${saved_signal}" "${signal}"; then
        trap '' "${signal}"
        return 0
    fi
    printf -v handler \
        'lumen_with_lock_signal_trap "$?" %q %q %q %q %q %q %q' \
        "${signal}" "${rc}" "${lock_dir}" "${owner_token}" \
        "${saved_exit}" "${saved_int}" "${saved_term}"
    # shellcheck disable=SC2064  # Handler contains shell-quoted frame values.
    trap "${handler}" "${signal}"
}

# lumen_acquire_lock <root> <script_name>
# 固定锁顺序：parent anchor -> root-local compatibility lock ->
# update/backup/restore operation lock。任何调用方都不得反向获取。
lumen_acquire_lock() {
    local root="$1"
    local script_name="${2:-maintenance}"
    local lock_file="${root}/.lumen-maintenance.lock"
    local local_lock_dir="${lock_file}.d"
    local anchor_lock_dir=""
    local anchor_identity=""
    local anchor_owner_token=""
    local anchor_owner_capability=""
    local local_owner_token=""
    local local_owner_capability=""
    local owner_pid=""

    if [ -n "${LUMEN_LOCK_KIND:-}" ]; then
        return 0
    fi
    if [ -n "${LUMEN_BORROWED_MAINTENANCE_LOCK_KIND:-}" ]; then
        if ! lumen_adopt_borrowed_maintenance_lock "${root}"; then
            log_error "继承的 maintenance 锁证明无效，拒绝重新获取：${root}"
            exit 1
        fi
        return 0
    fi
    if ! lumen_set_maintenance_root_binding "${root}"; then
        log_error "无法安全绑定 maintenance root 的 parent entry：${root}"
        exit 1
    fi
    anchor_lock_dir="$(
        printf '%s/.lumen-maintenance.%s.lock.d' \
            "${LUMEN_LOCK_ROOT_PARENT_PATH}" \
            "${LUMEN_LOCK_ROOT_ANCHOR_KEY}"
    )"

    if command -v flock >/dev/null 2>&1; then
        if ! lumen_maintenance_lock_path_safe "${lock_file}" file; then
            log_error "维护锁文件存在 symlink 或非普通文件：${lock_file}"
            exit 1
        fi
        if ! exec 9<"${LUMEN_LOCK_ROOT_PARENT_PATH}"; then
            log_error "无法打开 maintenance parent anchor：${LUMEN_LOCK_ROOT_PARENT_PATH}"
            exit 1
        fi
        if ! flock -n 9; then
            log_error "已有 Lumen 维护脚本在运行，当前 ${script_name} 退出。"
            log_error "parent anchor：${LUMEN_LOCK_ROOT_PARENT_PATH}"
            exit 1
        fi
        anchor_identity="$(
            lumen_flock_fd_identity \
                9 "${LUMEN_LOCK_ROOT_PARENT_PATH}" directory
        )" || true
        if [ "${anchor_identity}" != \
                "${LUMEN_LOCK_ROOT_PARENT_DEV}"$'\t'"${LUMEN_LOCK_ROOT_PARENT_INO}" ] \
                || ! lumen_verify_maintenance_root_binding "${root}"; then
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            log_error "maintenance parent/root entry 在加锁期间发生替换：${root}"
            exit 1
        fi
        if ! exec 6<>"${lock_file}"; then
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            log_error "无法创建 root-local maintenance 锁文件：${lock_file}"
            exit 1
        fi
        if ! flock -n 6; then
            exec 6>&- 2>/dev/null || true
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            log_error "已有 legacy/root-local maintenance 锁：${lock_file}"
            exit 1
        fi
        if ! lumen_flock_fd_identity 6 "${lock_file}" file >/dev/null 2>&1 \
                || ! lumen_verify_maintenance_root_binding "${root}"; then
            flock -u 6 2>/dev/null || true
            exec 6>&- 2>/dev/null || true
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            log_error "root-local maintenance 锁或 root entry 发生替换：${root}"
            exit 1
        fi
        if ! lumen_write_flock_lock_owner 6 "${script_name}"; then
            flock -u 6 2>/dev/null || true
            exec 6>&- 2>/dev/null || true
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            log_error "无法写入 maintenance flock owner capability：${lock_file}"
            exit 1
        fi
        LUMEN_LOCK_KIND="flock"
        LUMEN_LOCK_PATH="${lock_file}"
        LUMEN_LOCK_LOCAL_PATH="${lock_file}"
        LUMEN_LOCK_ANCHOR_PATH="${LUMEN_LOCK_ROOT_PARENT_PATH}"
    else
        if ! lumen_maintenance_lock_path_safe \
                "${anchor_lock_dir}" directory; then
            log_error "parent-anchor 锁存在 symlink 或非目录对象：${anchor_lock_dir}"
            exit 1
        fi
        if ! lumen_maintenance_lock_path_safe \
                "${local_lock_dir}" directory; then
            log_error "root-local 锁存在 symlink 或非目录对象：${local_lock_dir}"
            exit 1
        fi
        if ! lumen_try_create_owned_lock_dir \
                "${anchor_lock_dir}" script "${script_name}"; then
            owner_pid="$(lumen_lock_owner_pid "${anchor_lock_dir}")"
            if [ "${LUMEN_LAST_LOCK_STALE:-0}" = "1" ]; then
                log_error "检测到 stale Lumen 维护锁（owner pid=${owner_pid:-未知}）；为避免删除后来 owner，不自动回收。"
                log_error "确认没有维护脚本运行后，请人工删除：${anchor_lock_dir}"
            else
                log_error "已有 Lumen 维护脚本在运行（owner pid=${owner_pid:-未知}），当前 ${script_name} 退出。"
            fi
            log_error "parent anchor：${anchor_lock_dir}"
            exit 1
        fi
        anchor_owner_token="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
        anchor_owner_capability="${LUMEN_LAST_LOCK_OWNER_CAPABILITY}"
        if ! lumen_verify_maintenance_root_binding "${root}"; then
            lumen_release_owned_lock_dir \
                "${anchor_lock_dir}" "${anchor_owner_token}" 2>/dev/null || true
            log_error "maintenance root entry 在 parent anchor 后发生替换：${root}"
            exit 1
        fi
        if ! lumen_try_create_owned_lock_dir \
                "${local_lock_dir}" script "${script_name}"; then
            owner_pid="$(lumen_lock_owner_pid "${local_lock_dir}")"
            if [ "${LUMEN_LAST_LOCK_STALE:-0}" = "1" ]; then
                log_error "检测到 stale Lumen root-local 维护锁（owner pid=${owner_pid:-未知}）；为避免删除后来 owner，不自动回收。"
                log_error "确认没有维护脚本运行后，请人工删除：${local_lock_dir}"
            else
                log_error "已有 legacy/root-local 维护锁（owner pid=${owner_pid:-未知}），当前 ${script_name} 退出。"
            fi
            lumen_release_owned_lock_dir \
                "${anchor_lock_dir}" "${anchor_owner_token}" 2>/dev/null || true
            exit 1
        fi
        local_owner_token="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
        local_owner_capability="${LUMEN_LAST_LOCK_OWNER_CAPABILITY}"
        if ! lumen_verify_maintenance_root_binding "${root}"; then
            lumen_release_owned_lock_dir \
                "${local_lock_dir}" "${local_owner_token}" 2>/dev/null || true
            lumen_release_owned_lock_dir \
                "${anchor_lock_dir}" "${anchor_owner_token}" 2>/dev/null || true
            log_error "maintenance root entry 在双锁提交前发生替换：${root}"
            exit 1
        fi
        LUMEN_LOCK_KIND="mkdir"
        LUMEN_LOCK_PATH="${anchor_lock_dir}"
        LUMEN_LOCK_ANCHOR_PATH="${anchor_lock_dir}"
        LUMEN_LOCK_ANCHOR_OWNER_TOKEN="${anchor_owner_token}"
        LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY="${anchor_owner_capability}"
        LUMEN_LOCK_LOCAL_PATH="${local_lock_dir}"
        LUMEN_LOCK_OWNER_TOKEN="${local_owner_token}"
        LUMEN_LOCK_OWNER_CAPABILITY="${local_owner_capability}"
    fi

    trap 'lumen_release_lock' EXIT
}

# lumen_try_acquire_lock <root> <script_name>
# 非阻塞版本：占用时返回 1（不 exit）；成功时和 lumen_acquire_lock 一致。
# 用途：定时 backup 等场景"被占用则跳过本次"。
lumen_try_acquire_lock() {
    local root="$1"
    local script_name="${2:-maintenance}"
    local lock_file="${root}/.lumen-maintenance.lock"
    local local_lock_dir="${lock_file}.d"
    local anchor_lock_dir=""
    local anchor_identity=""
    local anchor_owner_token=""
    local anchor_owner_capability=""
    local local_owner_token=""
    local local_owner_capability=""

    if [ -n "${LUMEN_LOCK_KIND:-}" ]; then
        return 0
    fi
    lumen_set_maintenance_root_binding "${root}" || return 1
    anchor_lock_dir="$(
        printf '%s/.lumen-maintenance.%s.lock.d' \
            "${LUMEN_LOCK_ROOT_PARENT_PATH}" \
            "${LUMEN_LOCK_ROOT_ANCHOR_KEY}"
    )"

    if command -v flock >/dev/null 2>&1; then
        if ! lumen_maintenance_lock_path_safe "${lock_file}" file; then
            return 1
        fi
        if ! exec 9<"${LUMEN_LOCK_ROOT_PARENT_PATH}"; then
            return 1
        fi
        if ! flock -n 9 2>/dev/null; then
            exec 9>&- || true
            return 1
        fi
        anchor_identity="$(
            lumen_flock_fd_identity \
                9 "${LUMEN_LOCK_ROOT_PARENT_PATH}" directory
        )" || true
        if [ "${anchor_identity}" != \
                "${LUMEN_LOCK_ROOT_PARENT_DEV}"$'\t'"${LUMEN_LOCK_ROOT_PARENT_INO}" ] \
                || ! lumen_verify_maintenance_root_binding "${root}"; then
            flock -u 9 2>/dev/null || true
            exec 9>&- || true
            return 1
        fi
        if ! exec 6<>"${lock_file}"; then
            flock -u 9 2>/dev/null || true
            exec 9>&- || true
            return 1
        fi
        if ! flock -n 6 2>/dev/null; then
            exec 6>&- || true
            flock -u 9 2>/dev/null || true
            exec 9>&- || true
            return 1
        fi
        if ! lumen_flock_fd_identity 6 "${lock_file}" file >/dev/null 2>&1 \
                || ! lumen_verify_maintenance_root_binding "${root}"; then
            flock -u 6 2>/dev/null || true
            exec 6>&- || true
            flock -u 9 2>/dev/null || true
            exec 9>&- || true
            return 1
        fi
        if ! lumen_write_flock_lock_owner 6 "${script_name}"; then
            flock -u 6 2>/dev/null || true
            exec 6>&- || true
            flock -u 9 2>/dev/null || true
            exec 9>&- || true
            return 1
        fi
        LUMEN_LOCK_KIND="flock"
        LUMEN_LOCK_PATH="${lock_file}"
        LUMEN_LOCK_LOCAL_PATH="${lock_file}"
        LUMEN_LOCK_ANCHOR_PATH="${LUMEN_LOCK_ROOT_PARENT_PATH}"
    else
        if ! lumen_maintenance_lock_path_safe \
                "${anchor_lock_dir}" directory \
                || ! lumen_maintenance_lock_path_safe \
                    "${local_lock_dir}" directory; then
            return 1
        fi
        if ! lumen_try_create_owned_lock_dir \
                "${anchor_lock_dir}" script "${script_name}"; then
            return 1
        fi
        anchor_owner_token="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
        anchor_owner_capability="${LUMEN_LAST_LOCK_OWNER_CAPABILITY}"
        if ! lumen_verify_maintenance_root_binding "${root}" \
                || ! lumen_try_create_owned_lock_dir \
                    "${local_lock_dir}" script "${script_name}"; then
            lumen_release_owned_lock_dir \
                "${anchor_lock_dir}" "${anchor_owner_token}" 2>/dev/null || true
            return 1
        fi
        local_owner_token="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
        local_owner_capability="${LUMEN_LAST_LOCK_OWNER_CAPABILITY}"
        if ! lumen_verify_maintenance_root_binding "${root}"; then
            lumen_release_owned_lock_dir \
                "${local_lock_dir}" "${local_owner_token}" 2>/dev/null || true
            lumen_release_owned_lock_dir \
                "${anchor_lock_dir}" "${anchor_owner_token}" 2>/dev/null || true
            return 1
        fi
        LUMEN_LOCK_KIND="mkdir"
        LUMEN_LOCK_PATH="${anchor_lock_dir}"
        LUMEN_LOCK_ANCHOR_PATH="${anchor_lock_dir}"
        LUMEN_LOCK_ANCHOR_OWNER_TOKEN="${anchor_owner_token}"
        LUMEN_LOCK_ANCHOR_OWNER_CAPABILITY="${anchor_owner_capability}"
        LUMEN_LOCK_LOCAL_PATH="${local_lock_dir}"
        LUMEN_LOCK_OWNER_TOKEN="${local_owner_token}"
        LUMEN_LOCK_OWNER_CAPABILITY="${local_owner_capability}"
    fi

    trap 'lumen_release_lock' EXIT
    return 0
}

# 全局更新锁（§12.5）：优先 flock，macOS / 精简环境无 flock 时用 mkdir 目录锁兜底。
# 用法：lumen_with_lock <operation_id> <ttl_seconds> <cmd...>；占用时输出 system_operation_busy 并退出 75。
lumen_with_lock() {
    local op_id="$1"
    local ttl="$2"
    shift 2 || true
    local lock_dir="${LUMEN_BACKUP_ROOT:-/opt/lumendata/backup}"
    local lock_file="${lock_dir}/.lumen-update.lock"
    local lock_mkdir="${lock_file}.d"
    local rc=0
    mkdir -p "${lock_dir}" 2>/dev/null || true

    if command -v flock >/dev/null 2>&1; then
        # 历史 bug：`exec 8>file 2>/dev/null` 会把整个 shell 的 stderr 永久指向
        # /dev/null（exec 无命令时所有 redirect 作用于当前 shell），后续 do_update
        # 的 log_warn / log_error 全部丢失。已修复为不重定向 fd 2。
        if ! exec 8>"${lock_file}"; then
            log_error "无法打开更新锁文件：${lock_file}"
            exit 1
        fi
        if ! flock -n 8; then
            printf '{"error":{"code":"system_operation_busy","operation_id":"%s","retry_after":%s}}\n' \
                "${op_id}" "${ttl}"
            exec 8>&- || true
            exit 75
        fi
        "$@" || rc=$?
        flock -u 8 2>/dev/null || true
        exec 8>&- || true
        return "${rc}"
    fi

    local saved_exit="" saved_int="" saved_term=""
    local trap_capture_file=""
    trap_capture_file="$(mktemp "${lock_dir}/.lumen-traps.XXXXXXXXXX" 2>/dev/null || true)"
    if [ -z "${trap_capture_file}" ]; then
        log_error "无法保存调用方 trap 状态：${lock_dir}"
        exit 1
    fi
    if ! lumen_capture_current_trap EXIT "${trap_capture_file}"; then
        rm -f "${trap_capture_file}" 2>/dev/null || true
        log_error "无法读取调用方 EXIT trap"
        exit 1
    fi
    saved_exit="${LUMEN_CAPTURED_TRAP}"
    if ! lumen_capture_current_trap INT "${trap_capture_file}"; then
        rm -f "${trap_capture_file}" 2>/dev/null || true
        log_error "无法读取调用方 INT trap"
        exit 1
    fi
    saved_int="${LUMEN_CAPTURED_TRAP}"
    if ! lumen_capture_current_trap TERM "${trap_capture_file}"; then
        rm -f "${trap_capture_file}" 2>/dev/null || true
        log_error "无法读取调用方 TERM trap"
        exit 1
    fi
    saved_term="${LUMEN_CAPTURED_TRAP}"
    rm -f "${trap_capture_file}" 2>/dev/null || true

    if ! lumen_try_create_owned_lock_dir "${lock_mkdir}" operation_id "${op_id}"; then
        printf '{"error":{"code":"system_operation_busy","operation_id":"%s","retry_after":%s}}\n' \
            "${op_id}" "${ttl}"
        exit 75
    fi
    local owner_token="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
    local exit_handler=""
    printf -v exit_handler \
        'lumen_with_lock_exit_trap "$?" %q %q %q %q %q' \
        "${lock_mkdir}" "${owner_token}" \
        "${saved_exit}" "${saved_int}" "${saved_term}"
    # shellcheck disable=SC2064  # Handlers already contain shell-quoted frame values.
    trap "${exit_handler}" EXIT
    lumen_install_with_lock_signal_trap \
        INT 130 "${lock_mkdir}" "${owner_token}" \
        "${saved_exit}" "${saved_int}" "${saved_term}"
    lumen_install_with_lock_signal_trap \
        TERM 143 "${lock_mkdir}" "${owner_token}" \
        "${saved_exit}" "${saved_int}" "${saved_term}"

    "$@" || rc=$?
    lumen_with_lock_release_owner "${lock_mkdir}" "${owner_token}" || true
    lumen_restore_saved_trap "${saved_exit}" EXIT
    lumen_restore_saved_trap "${saved_int}" INT
    lumen_restore_saved_trap "${saved_term}" TERM
    return "${rc}"
}
