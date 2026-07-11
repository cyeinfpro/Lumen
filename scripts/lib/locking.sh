#!/usr/bin/env bash
# Maintenance and operation lock helpers.
# Sourced by scripts/lib.sh; do not execute directly.

# lumen_acquire_lock <root> <script_name>
# update / uninstall 共用同一把维护锁，避免同时操作 compose、迁移和依赖目录。
lumen_release_lock() {
    case "${LUMEN_LOCK_KIND:-}" in
        flock)
            flock -u 9 2>/dev/null || true
            exec 9>&- 2>/dev/null || true
            ;;
        mkdir)
            if [ -n "${LUMEN_LOCK_PATH:-}" ]; then
                rm -rf "${LUMEN_LOCK_PATH}" 2>/dev/null || true
            fi
            ;;
    esac
    LUMEN_LOCK_KIND=""
}

lumen_lock_dir_stale() {
    local lock_dir="$1"
    local owner_file="${lock_dir}/owner"
    local owner_pid="" owner_script="" owner_cmd=""
    if [ ! -f "${owner_file}" ]; then
        return 0
    fi
    owner_pid="$(grep -E '^pid=' "${owner_file}" 2>/dev/null \
        | head -1 | sed 's/^pid=//' | tr -d '[:space:]')"
    owner_script="$(grep -E '^script=' "${owner_file}" 2>/dev/null \
        | head -1 | sed 's/^script=//' | tr -d '[:space:]')"
    case "${owner_pid}" in
        ''|*[!0-9]*) return 0 ;;
    esac
    if ! kill -0 "${owner_pid}" 2>/dev/null; then
        return 0
    fi
    if [ -z "${owner_script}" ]; then
        return 1
    fi
    if ! owner_cmd="$(lumen_pid_cmdline "${owner_pid}" 2>/dev/null)"; then
        log_warn "锁 owner pid=${owner_pid} 仍存在，但命令行暂不可读；保守保留 lock。"
        return 1
    fi
    case "${owner_cmd}" in
        *"${owner_script}"*) return 1 ;;
    esac
    log_warn "锁 owner pid=${owner_pid} 仍存在，但命令行不匹配 script=${owner_script}：${owner_cmd:-<unavailable>}"
    return 0
}

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
        if ! mkdir "${lock_dir}" 2>/dev/null; then
            # mkdir 锁的 stale-check：进程被 kill -9 / OOM kill 不会跑 EXIT
            # trap，锁目录残留。读 owner 的 pid + script，并验证 pid 命令行仍是
            # 同一个维护脚本；仅 pid 存活不够，PID 复用会把 stale 锁误判为活锁。
            # flock 路径不需此逻辑，kernel 自动释放。
            local _owner_pid=""
            if [ -f "${lock_dir}/owner" ]; then
                _owner_pid="$(grep -E '^pid=' "${lock_dir}/owner" 2>/dev/null \
                    | head -1 | sed 's/^pid=//' | tr -d '[:space:]')"
            fi
            if lumen_lock_dir_stale "${lock_dir}"; then
                log_warn "检测到 stale 锁（owner pid=${_owner_pid:-未知} 已失效或不匹配），自动清理后重试..."
                rm -rf "${lock_dir}" 2>/dev/null || true
                if ! mkdir "${lock_dir}" 2>/dev/null; then
                    log_error "已有 Lumen 维护脚本在运行（stale 清理后仍冲突），当前 ${script_name} 退出。"
                    log_error "锁目录：${lock_dir}"
                    exit 1
                fi
            else
                log_error "已有 Lumen 维护脚本在运行（owner pid=${_owner_pid:-未知}），当前 ${script_name} 退出。"
                log_error "锁目录：${lock_dir}"
                log_error "如果确认没有脚本在运行，可手动删除该锁目录后重试。"
                exit 1
            fi
        fi
        LUMEN_LOCK_KIND="mkdir"
        LUMEN_LOCK_PATH="${lock_dir}"
        {
            printf 'pid=%s\n' "$$"
            printf 'script=%s\n' "${script_name}"
            printf 'started_at=%s\n' "$(date -u +%FT%TZ 2>/dev/null || date)"
        } > "${lock_dir}/owner"
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
        if ! mkdir "${lock_dir}" 2>/dev/null; then
            if lumen_lock_dir_stale "${lock_dir}"; then
                rm -rf "${lock_dir}" 2>/dev/null || true
                mkdir "${lock_dir}" 2>/dev/null || return 1
            else
                return 1
            fi
        fi
        LUMEN_LOCK_KIND="mkdir"
        LUMEN_LOCK_PATH="${lock_dir}"
        {
            printf 'pid=%s\n' "$$"
            printf 'script=%s\n' "${script_name}"
            printf 'started_at=%s\n' "$(date -u +%FT%TZ 2>/dev/null || date)"
        } > "${lock_dir}/owner" 2>/dev/null || true
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

    if ! mkdir "${lock_mkdir}" 2>/dev/null; then
        printf '{"error":{"code":"system_operation_busy","operation_id":"%s","retry_after":%s}}\n' \
            "${op_id}" "${ttl}"
        exit 75
    fi
    {
        printf 'pid=%s\n' "$$"
        printf 'operation_id=%s\n' "${op_id}"
        printf 'started_at=%s\n' "$(date -u +%FT%TZ 2>/dev/null || date)"
    } > "${lock_mkdir}/owner" 2>/dev/null || true
    "$@" || rc=$?
    rm -rf "${lock_mkdir}" 2>/dev/null || true
    return "${rc}"
}
