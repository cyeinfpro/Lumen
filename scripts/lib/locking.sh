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
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            ;;
        mkdir)
            if [ -n "${LUMEN_LOCK_PATH:-}" ]; then
                if ! lumen_release_owned_lock_dir \
                        "${LUMEN_LOCK_PATH}" "${LUMEN_LOCK_OWNER_TOKEN:-}"; then
                    log_warn "维护锁 owner 已变化，拒绝删除：${LUMEN_LOCK_PATH}"
                fi
            fi
            ;;
    esac
    LUMEN_LOCK_KIND=""
    LUMEN_LOCK_PATH=""
    LUMEN_LOCK_OWNER_TOKEN=""
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
    case "${label_key}" in
        ''|*[!A-Za-z0-9_]*) return 1 ;;
    esac
    case "${label_value}" in
        *$'\n'*|*$'\r'*) return 1 ;;
    esac
    start_token="$(lumen_pid_start_token "$$")" || return 1
    if ! (
        umask 077
        {
            printf 'pid=%s\n' "$$"
            printf 'start_token=%s\n' "${start_token}"
            printf 'owner_id=%s\n' "${owner_id}"
            printf '%s=%s\n' "${label_key}" "${label_value}"
            printf 'started_at=%s\n' "$(date -u +%FT%TZ 2>/dev/null || date)"
        } > "${owner_tmp}"
    ) || ! mv -f "${owner_tmp}" "${owner_dir}/owner"; then
        rm -f "${owner_tmp}" 2>/dev/null || true
        return 1
    fi
    LUMEN_LAST_LOCK_OWNER_TOKEN="${owner_id}"
}

lumen_try_create_owned_lock_dir() {
    local lock_dir="$1"
    local label_key="$2"
    local label_value="$3"
    local owner_dir=""
    local owner_pid=""
    LUMEN_LAST_LOCK_OWNER_TOKEN=""
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
# update / uninstall 共用同一把维护锁，避免同时操作 compose、迁移和依赖目录。
lumen_acquire_lock() {
    local root="$1"
    local script_name="${2:-maintenance}"
    local lock_file="${root}/.lumen-maintenance.lock"
    local lock_dir="${lock_file}.d"

    if [ -n "${LUMEN_LOCK_KIND:-}" ]; then
        return 0
    fi

    if command -v flock >/dev/null 2>&1; then
        if ! exec 9>"${lock_file}"; then
            log_error "无法创建锁文件：${lock_file}"
            exit 1
        fi
        if ! flock -n 9; then
            log_error "已有 Lumen 维护脚本在运行，当前 ${script_name} 退出。"
            log_error "锁文件：${lock_file}"
            exit 1
        fi
        LUMEN_LOCK_KIND="flock"
        LUMEN_LOCK_PATH="${lock_file}"
    else
        if ! lumen_try_create_owned_lock_dir "${lock_dir}" script "${script_name}"; then
            local _owner_pid=""
            _owner_pid="$(lumen_lock_owner_pid "${lock_dir}")"
            if [ "${LUMEN_LAST_LOCK_STALE:-0}" = "1" ]; then
                log_error "检测到 stale Lumen 维护锁（owner pid=${_owner_pid:-未知}）；为避免删除后来 owner，不自动回收。"
                log_error "确认没有维护脚本运行后，请人工删除：${lock_dir}"
            else
                log_error "已有 Lumen 维护脚本在运行（owner pid=${_owner_pid:-未知}），当前 ${script_name} 退出。"
            fi
            log_error "锁目录：${lock_dir}"
            exit 1
        fi
        LUMEN_LOCK_KIND="mkdir"
        LUMEN_LOCK_PATH="${lock_dir}"
        LUMEN_LOCK_OWNER_TOKEN="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
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
    local lock_dir="${lock_file}.d"

    if [ -n "${LUMEN_LOCK_KIND:-}" ]; then
        return 0
    fi

    if command -v flock >/dev/null 2>&1; then
        # 注意：`exec FD>file 2>/dev/null` 会把当前 shell 的 stderr 永久重定向到
        # /dev/null（exec 无命令时所有 redirect 都作用于当前 shell）。改为不带
        # 2>/dev/null，让 exec 失败时错误正常显示，且不污染主 shell 的 fd 2。
        if ! exec 9>"${lock_file}"; then
            return 1
        fi
        if ! flock -n 9 2>/dev/null; then
            exec 9>&- || true
            return 1
        fi
        LUMEN_LOCK_KIND="flock"
        LUMEN_LOCK_PATH="${lock_file}"
    else
        if ! lumen_try_create_owned_lock_dir "${lock_dir}" script "${script_name}"; then
            return 1
        fi
        LUMEN_LOCK_KIND="mkdir"
        LUMEN_LOCK_PATH="${lock_dir}"
        LUMEN_LOCK_OWNER_TOKEN="${LUMEN_LAST_LOCK_OWNER_TOKEN}"
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
