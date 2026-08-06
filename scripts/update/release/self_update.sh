#!/usr/bin/env bash
# Lock phase initialization and release-bound self-update phase.

lumen_update_expected_scripts_state_path() {
    case "${OPERATION_ID:-}" in
        ''|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    printf '%s/.lumen-update-state/scripts-%s.commit\n' \
        "${ROOT:?}" "${OPERATION_ID}"
}

lumen_update_expected_scripts_state_file() {
    local action="$1"
    local state_path="$2"
    local value="${3:-}"
    python3 - "${action}" "${state_path}" "${value}" <<'PY'
import errno
import os
from pathlib import Path
import secrets
import stat
import sys

action = sys.argv[1]
path = Path(sys.argv[2])
value = sys.argv[3]
directory = path.parent
name = path.name
no_follow = getattr(os, "O_NOFOLLOW", 0)
directory_flag = getattr(os, "O_DIRECTORY", 0)


def fsync_directory(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
            raise


def open_control_dir(*, create: bool):
    if create:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SystemExit("update control path is not a real directory")
    if info.st_uid != os.geteuid():
        raise SystemExit("update control directory owner is invalid")
    if info.st_mode & 0o077:
        os.chmod(directory, 0o700)
    fd = os.open(
        directory,
        os.O_RDONLY | directory_flag | no_follow | getattr(os, "O_CLOEXEC", 0),
    )
    opened = os.fstat(fd)
    if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
        os.close(fd)
        raise SystemExit("update control directory changed while opening")
    return fd


if action == "read":
    directory_fd = open_control_dir(create=False)
    if directory_fd is None:
        raise SystemExit(0)
    try:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            raise SystemExit(0)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SystemExit("expected scripts state is not a regular file")
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise SystemExit("expected scripts state permissions are invalid")
            if info.st_size <= 0 or info.st_size > 128:
                raise SystemExit("expected scripts state size is invalid")
            data = os.read(fd, 129)
            if len(data) != info.st_size:
                raise SystemExit("expected scripts state changed while reading")
            sys.stdout.write(data.decode("ascii"))
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
elif action == "write":
    directory_fd = open_control_dir(create=True)
    if directory_fd is None:
        raise SystemExit("cannot create update control directory")
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | no_follow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        payload = (value + "\n").encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        fsync_directory(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
elif action == "remove":
    directory_fd = open_control_dir(create=False)
    if directory_fd is None:
        raise SystemExit(0)
    try:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        fsync_directory(directory_fd)
    finally:
        os.close(directory_fd)
else:
    raise SystemExit("unknown expected scripts state action")
PY
}

lumen_update_script_unit_complete() {
    local scripts_dir="$1"
    local expected_commit="${LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT:-}"
    local source_marker="${scripts_dir}/.lumen-self-update.source"
    [[ "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] || return 1
    [ -f "${source_marker}" ] \
        && [ ! -L "${source_marker}" ] \
        && [ "$(head -n1 "${source_marker}" 2>/dev/null | tr -d '[:space:]')" = "${expected_commit}" ] \
        || return 1
    lumen_self_update_integrity_valid \
        "${scripts_dir}" \
        "${expected_commit}" \
        "${scripts_dir}/.lumen-self-update.integrity" \
        "${_LUMEN_UPDATE_SCRIPT_UNIT_FILES[@]}"
}

lumen_update_bind_expected_scripts_commit() {
    local scripts_dir="${1:-${SCRIPT_DIR:-}}"
    local state_path=""
    local state_commit=""
    local state_rc=0
    local expected_commit="${LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT:-}"
    local source_marker="${scripts_dir}/.lumen-self-update.source"

    state_path="$(lumen_update_expected_scripts_state_path)" || return 1
    state_commit="$(
        lumen_update_expected_scripts_state_file read "${state_path}" 2>/dev/null
    )" || state_rc=$?
    if [ "${state_rc}" -ne 0 ]; then
        log_error "[self_update_scripts] expected scripts commit state 不安全：${state_path}"
        return 1
    fi
    state_commit="$(printf '%s' "${state_commit}" | tr -d '[:space:]')"
    if [ -n "${state_commit}" ]; then
        [[ "${state_commit}" =~ ^[0-9a-f]{40}$ ]] || {
            log_error "[self_update_scripts] expected scripts commit state 无效。"
            return 1
        }
    fi
    if [ -n "${expected_commit}" ] \
            && [[ ! "${expected_commit}" =~ ^[0-9a-f]{40}$ ]]; then
        log_error "[self_update_scripts] LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT 无效。"
        return 1
    fi
    if [ -n "${expected_commit}" ] \
            && [ -n "${state_commit}" ] \
            && [ "${expected_commit}" != "${state_commit}" ]; then
        log_error "[self_update_scripts] expected scripts commit 在 reexec/resume 中发生漂移：env=${expected_commit} state=${state_commit}"
        return 1
    fi
    expected_commit="${expected_commit:-${state_commit}}"
    if [ -z "${expected_commit}" ] \
            && [ -f "${source_marker}" ] \
            && [ ! -L "${source_marker}" ]; then
        expected_commit="$(
            head -n1 "${source_marker}" 2>/dev/null | tr -d '[:space:]'
        )"
        if [[ ! "${expected_commit}" =~ ^[0-9a-f]{40}$ ]] \
                || ! lumen_self_update_integrity_valid \
                    "${scripts_dir}" \
                    "${expected_commit}" \
                    "${scripts_dir}/.lumen-self-update.integrity" \
                    "${_LUMEN_UPDATE_SCRIPT_UNIT_FILES[@]}"; then
            expected_commit=""
        fi
    fi
    [ -n "${expected_commit}" ] || return 0

    LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT="${expected_commit}"
    export LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT
    if [ -n "${state_commit}" ]; then
        return 0
    fi
    if ! lumen_update_expected_scripts_state_file \
            write "${state_path}" "${expected_commit}"; then
        log_error "[self_update_scripts] 无法安全持久化 expected scripts commit。"
        return 1
    fi
}

lumen_update_clear_expected_scripts_commit() {
    local state_path=""
    state_path="$(lumen_update_expected_scripts_state_path)" || return 0
    lumen_update_expected_scripts_state_file \
        remove "${state_path}" >/dev/null 2>&1 || true
}

# Phase: lock
update_phase_lock() {
emit_start lock
emit_info lock operation_id "${OPERATION_ID}"

# CURRENT_RELEASE 提前到 self_update_scripts 前解析（check phase 内仍会重赋值，幂等）；
# 不放进 check phase 是为了 self_update_scripts 在 noop 判断之前就能拿到 release scripts 目录。
CURRENT_RELEASE=""
CURRENT_ID=""
if [ -L "${ROOT}/current" ]; then
    CURRENT_RELEASE="$(lumen_release_current_path "${ROOT}" || true)"
    [ -n "${CURRENT_RELEASE}" ] && CURRENT_ID="$(basename "${CURRENT_RELEASE}")"
fi
emit_done  lock 0
}

# Phase: self_update_scripts
update_phase_self_update_scripts() {
emit_start self_update_scripts

if [ "${LUMEN_UPDATE_SELF_UPDATE_SCRIPTS:-1}" = "0" ]; then
    log_info "[self_update_scripts] 关闭（LUMEN_UPDATE_SELF_UPDATE_SCRIPTS=0）。"
    emit_done self_update_scripts 0
elif [ -z "${CURRENT_RELEASE}" ] || [ ! -d "${CURRENT_RELEASE}/scripts" ]; then
    log_info "[self_update_scripts] 不是 release 布局（CURRENT_RELEASE 为空），跳过。"
    emit_done self_update_scripts 0
else
    SELF_UPDATE_REF="${LUMEN_UPDATE_EXPECTED_SCRIPTS_COMMIT:-${LUMEN_UPDATE_SCRIPTS_REF:-}}"
    if [ -z "${SELF_UPDATE_REF}" ] && [ -f "${CURRENT_RELEASE}/.image-tag" ]; then
        SELF_UPDATE_REF="$(head -n1 "${CURRENT_RELEASE}/.image-tag" 2>/dev/null | tr -d '[:space:]')"
    fi
    if ! printf '%s\n' "${SELF_UPDATE_REF}" \
            | grep -Eq '^(v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?|[0-9a-f]{40})$'; then
        log_error "[self_update_scripts] 缺少 immutable release tag/commit。"
        emit_info self_update_scripts source_ref "${SELF_UPDATE_REF:-<none>}"
        emit_fail self_update_scripts 78
        return 78
    else
    local self_update_rc=0
    if lumen_self_update_scripts \
        "${CURRENT_RELEASE}/scripts" \
        "${SELF_UPDATE_REF}" \
        60 \
        "${_LUMEN_UPDATE_SCRIPT_UNIT_FILES[@]}"; then
        :
    else
        self_update_rc=$?
    fi
    if [ "${self_update_rc}" -ne 0 ]; then
        log_error "[self_update_scripts] 无法取得并验证与 release 绑定的 updater；拒绝继续。"
        emit_info self_update_scripts result "${LUMEN_SELF_UPDATE_RESULT:-unknown}"
        emit_fail self_update_scripts "${self_update_rc}"
        return "${self_update_rc}"
    fi
    case "${LUMEN_SELF_UPDATE_RESULT:-}" in
        ok)
            if ! lumen_update_bind_expected_scripts_commit \
                    "${CURRENT_RELEASE}/scripts"; then
                emit_fail self_update_scripts 78
                return 78
            fi
            if [ -n "${LUMEN_SELF_UPDATE_CHANGED:-}" ]; then
                emit_info self_update_scripts source "${LUMEN_SELF_UPDATE_SOURCE}"
                emit_info self_update_scripts commit "${LUMEN_SELF_UPDATE_SOURCE_COMMIT}"
                emit_info self_update_scripts changed "${LUMEN_SELF_UPDATE_CHANGED}"
                emit_info self_update_scripts backup_suffix ".bak.${LUMEN_SELF_UPDATE_BACKUP_TS}"
                # 已加载的 updater/lock/permission contract 变化时必须在同一把
                # maintenance 锁内 re-exec，避免旧 shell 函数调用新 helper。
                case " ${LUMEN_SELF_UPDATE_CHANGED} " in
                    *" update.sh "*|*" update/"*|*" lib.sh "*|\
                    *" lib/locking.sh "*|*" lib/runtime.sh "*|\
                    *" backup_permissions.py "*)
                        local self_update_hops="${LUMEN_UPDATE_SELF_UPDATED:-0}"
                        case "${self_update_hops}" in
                            ''|*[!0-9]*) self_update_hops=0 ;;
                        esac
                        if [ "${self_update_hops}" -ge 2 ]; then
                            log_error "[self_update_scripts] updater contract 连续变化超过两跳，拒绝继续。"
                            emit_fail self_update_scripts 78
                            return 78
                        else
                            if ! lumen_export_borrowed_maintenance_lock \
                                    "${ROOT}"; then
                                log_error "[self_update_scripts] 无法导出 maintenance 锁证明，拒绝无锁 re-exec。"
                                emit_fail self_update_scripts 78
                                return 78
                            fi
                            log_info "[self_update_scripts] updater contract 已变更，在原 maintenance 锁内 re-exec 新版。"
                            emit_done self_update_scripts 0
                            export LUMEN_UPDATE_SELF_UPDATED=$((self_update_hops + 1))
                            export LUMEN_UPDATE_RESUME=1
                            export OPERATION_ID
                            exec bash "${CURRENT_RELEASE}/scripts/update.sh" "$@"
                        fi
                        ;;
                esac
            fi
            emit_done self_update_scripts 0
            ;;
        skipped)
            if ! lumen_update_bind_expected_scripts_commit \
                    "${CURRENT_RELEASE}/scripts"; then
                emit_fail self_update_scripts 78
                return 78
            fi
            emit_done self_update_scripts 0
            ;;
        *)
            log_error "[self_update_scripts] 非成功终态：${LUMEN_SELF_UPDATE_RESULT:-unknown}"
            emit_info self_update_scripts result "${LUMEN_SELF_UPDATE_RESULT:-unknown}"
            emit_fail self_update_scripts 78
            return 78
            ;;
    esac
    fi
fi
}
