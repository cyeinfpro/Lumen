#!/usr/bin/env bash
# Host runtime, systemd, filesystem ownership, and local process helpers.
# Sourced by scripts/lib.sh; do not execute directly.

lumen_run_as_root() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        lumen_sudo "$@"
    else
        return 1
    fi
}

: "${LUMEN_SYSTEMD_UNIT_DIR:=/etc/systemd/system}"
: "${LUMEN_SYSTEMD_RUNTIME_DIR:=/run/systemd/system}"
: "${LUMEN_LOCAL_SBIN_DIR:=/usr/local/sbin}"

lumen_systemd_runtime_available() {
    command -v systemctl >/dev/null 2>&1 \
        && [ -d "${LUMEN_SYSTEMD_RUNTIME_DIR}" ]
}

lumen_operations_host_artifact_paths() {
    local unit
    for unit in \
        lumen-update.path \
        lumen-update-runner.service \
        lumen-update-warm.path \
        lumen-update-warm.service \
        lumen-backup.service \
        lumen-backup.timer \
        lumen-backup.path \
        lumen-restore-runner.service \
        lumen-restore.path \
        lumen-storage-mount.service \
        lumen-storage-apply.service \
        lumen-storage-apply.path \
        lumen-storage-test.service \
        lumen-storage-test.path; do
        printf '%s/%s\n' "${LUMEN_SYSTEMD_UNIT_DIR%/}" "${unit}"
    done
    printf '%s/%s\n' "${LUMEN_LOCAL_SBIN_DIR%/}" "lumen-storage-mount"
}

lumen_snapshot_host_artifacts() {
    local snapshot_dir="$1"
    shift
    local manifest="${snapshot_dir}/manifest.tsv"
    local files_dir="${snapshot_dir}/files"
    local artifact token index=0
    if ! mkdir -p "${files_dir}" || ! : > "${manifest}"; then
        return 1
    fi
    for artifact in "$@"; do
        case "${artifact}" in
            /*) ;;
            *)
                log_error "host artifact 必须是绝对路径：${artifact}"
                return 1
                ;;
        esac
        index=$((index + 1))
        token="$(printf '%04d' "${index}")"
        if [ -e "${artifact}" ] || [ -L "${artifact}" ]; then
            printf 'present\t%s\t%s\n' "${token}" "${artifact}" >> "${manifest}"
            if ! cp -a "${artifact}" "${files_dir}/${token}" 2>/dev/null \
                    && ! lumen_run_as_root cp -a "${artifact}" "${files_dir}/${token}"; then
                log_error "无法快照 host artifact：${artifact}"
                return 1
            fi
        else
            printf 'absent\t%s\t%s\n' "${token}" "${artifact}" >> "${manifest}"
        fi
    done
}

lumen_snapshot_operations_host_artifacts() {
    local snapshot_dir="$1"
    local artifacts=()
    local artifact
    while IFS= read -r artifact; do
        [ -n "${artifact}" ] && artifacts+=("${artifact}")
    done < <(lumen_operations_host_artifact_paths)
    lumen_snapshot_host_artifacts "${snapshot_dir}" "${artifacts[@]}"
}

lumen_restore_host_artifacts() {
    local snapshot_dir="$1"
    local manifest="${snapshot_dir}/manifest.tsv"
    local files_dir="${snapshot_dir}/files"
    local state token artifact restore_tmp unit rc=0 touched_systemd=0
    [ -f "${manifest}" ] || return 1
    while IFS=$'\t' read -r state token artifact; do
        [ -n "${artifact}" ] || continue
        case "${artifact}" in
            "${LUMEN_SYSTEMD_UNIT_DIR%/}"/*)
                touched_systemd=1
                ;;
        esac
        case "${state}" in
            present)
                if ! lumen_run_as_root mkdir -p "$(dirname "${artifact}")"; then
                    rc=1
                    continue
                fi
                restore_tmp="${artifact}.lumen-restore.$$"
                lumen_run_as_root rm -f "${restore_tmp}" 2>/dev/null || true
                if ! lumen_run_as_root cp -a "${files_dir}/${token}" "${restore_tmp}" \
                        || ! lumen_run_as_root mv -f "${restore_tmp}" "${artifact}"; then
                    lumen_run_as_root rm -f "${restore_tmp}" 2>/dev/null || true
                    log_error "无法恢复 host artifact：${artifact}"
                    rc=1
                fi
                ;;
            absent)
                case "${artifact}" in
                    "${LUMEN_SYSTEMD_UNIT_DIR%/}"/*)
                        unit="$(basename "${artifact}")"
                        if lumen_systemd_runtime_available; then
                            lumen_run_as_root systemctl disable --now "${unit}" \
                                >/dev/null 2>&1 || true
                        fi
                        ;;
                esac
                if ! lumen_run_as_root rm -f "${artifact}"; then
                    log_error "无法删除安装前不存在的 host artifact：${artifact}"
                    rc=1
                fi
                ;;
            *)
                log_error "host artifact 快照状态非法：${state:-<empty>}"
                rc=1
                ;;
        esac
    done < "${manifest}"

    if [ "${touched_systemd}" -eq 1 ] && lumen_systemd_runtime_available; then
        lumen_run_as_root systemctl daemon-reload || rc=1
        for unit in \
            lumen-update.path \
            lumen-update-warm.path \
            lumen-backup.path \
            lumen-restore.path; do
            if [ -f "${LUMEN_SYSTEMD_UNIT_DIR%/}/${unit}" ]; then
                lumen_run_as_root systemctl try-restart "${unit}" \
                    >/dev/null 2>&1 || true
            fi
        done
    fi
    return "${rc}"
}

lumen_restore_operations_host_artifacts() {
    lumen_restore_host_artifacts "$1"
}

lumen_discard_host_artifact_snapshot() {
    local snapshot_dir="${1:-}"
    [ -n "${snapshot_dir}" ] || return 0
    rm -rf "${snapshot_dir}" 2>/dev/null \
        || lumen_run_as_root rm -rf "${snapshot_dir}" 2>/dev/null \
        || true
}

lumen_run_as_user() {
    local user="$1"
    shift
    if [ "$(id -un 2>/dev/null || true)" = "${user}" ]; then
        "$@"
    elif [ "${EUID:-$(id -u)}" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runuser -u "${user}" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        lumen_sudo -u "${user}" "$@"
    else
        return 1
    fi
}

lumen_systemd_unit_property() {
    local unit="$1"
    local prop="$2"
    command -v systemctl >/dev/null 2>&1 || return 0
    systemctl show -p "${prop}" --value "${unit}" 2>/dev/null || true
}

lumen_runtime_service_user() {
    local user=""
    if lumen_systemd_has_unit lumen-api.service; then
        user="$(lumen_systemd_unit_property lumen-api.service User)"
    elif lumen_systemd_has_unit lumen-worker.service; then
        user="$(lumen_systemd_unit_property lumen-worker.service User)"
    elif lumen_systemd_has_unit lumen-web.service; then
        user="$(lumen_systemd_unit_property lumen-web.service User)"
    fi
    printf '%s' "${user:-$(id -un)}"
}

lumen_runtime_service_group() {
    local user="$1"
    local group=""
    if lumen_systemd_has_unit lumen-api.service; then
        group="$(lumen_systemd_unit_property lumen-api.service Group)"
    elif lumen_systemd_has_unit lumen-worker.service; then
        group="$(lumen_systemd_unit_property lumen-worker.service Group)"
    elif lumen_systemd_has_unit lumen-web.service; then
        group="$(lumen_systemd_unit_property lumen-web.service Group)"
    fi
    if [ -z "${group}" ] && id "${user}" >/dev/null 2>&1; then
        group="$(id -gn "${user}" 2>/dev/null || true)"
    fi
    printf '%s' "${group:-${user}}"
}

lumen_user_can_write_dir() {
    local user="$1"
    local dir="$2"
    lumen_run_as_user "${user}" test -w "${dir}" >/dev/null 2>&1
}

lumen_ensure_dir_writable() {
    local dir="$1"
    local label="${2:-目录}"
    local owner_user="${3:-}"
    local owner_group="${4:-}"
    if [ -z "${dir}" ]; then
        log_error "${label} 为空。"
        return 1
    fi
    if [ -e "${dir}" ] && [ ! -d "${dir}" ]; then
        log_error "${label} 已存在但不是目录：${dir}"
        return 1
    fi
    if [ ! -d "${dir}" ]; then
        mkdir -p "${dir}" 2>/dev/null || lumen_run_as_root mkdir -p "${dir}" || {
            log_error "无法创建 ${label}：${dir}"
            return 1
        }
    fi
    if [ -n "${owner_user}" ]; then
        owner_group="${owner_group:-${owner_user}}"
        if ! lumen_user_can_write_dir "${owner_user}" "${dir}"; then
            lumen_run_as_root chown -R "${owner_user}:${owner_group}" "${dir}" 2>/dev/null || true
        fi
        if ! lumen_user_can_write_dir "${owner_user}" "${dir}"; then
            log_error "${label} 对运行用户 ${owner_user} 不可写：${dir}"
            return 1
        fi
        return 0
    fi
    if [ ! -w "${dir}" ]; then
        lumen_run_as_root chown -R "$(id -un):$(id -gn)" "${dir}" 2>/dev/null || true
    fi
    if [ ! -w "${dir}" ]; then
        log_error "${label} 不可写：${dir}"
        return 1
    fi
}

lumen_ensure_runtime_dirs() {
    local env_file="${1:-.env}"
    local storage_root backup_root storage_parent owner_user owner_group
    storage_root="$(lumen_env_value STORAGE_ROOT "${env_file}")"
    backup_root="$(lumen_env_value BACKUP_ROOT "${env_file}")"
    storage_root="${storage_root:-/opt/lumendata/storage}"
    backup_root="${backup_root:-/opt/lumendata/backup}"
    storage_parent="$(dirname "${storage_root}")"
    owner_user="$(lumen_runtime_service_user)"
    owner_group="$(lumen_runtime_service_group "${owner_user}")"

    lumen_ensure_dir_writable "${storage_root}" "STORAGE_ROOT" "${owner_user}" "${owner_group}" || return 1
    lumen_ensure_dir_writable "${backup_root}/pg" "PostgreSQL 备份目录" "${owner_user}" "${owner_group}" || return 1
    lumen_ensure_dir_writable "${backup_root}/redis" "Redis 备份目录" "${owner_user}" "${owner_group}" || return 1
    if [ "${storage_parent}" != "." ]; then
        log_info "运行时目录就绪：${storage_parent}（运行用户：${owner_user}:${owner_group}）"
    fi
}

lumen_systemd_has_unit() {
    local unit="$1"
    command -v systemctl >/dev/null 2>&1 || return 1
    if systemctl list-unit-files "${unit}" --no-legend 2>/dev/null \
        | awk '{print $1}' \
        | grep -Fxq "${unit}"; then
        return 0
    fi
    systemctl status "${unit}" >/dev/null 2>&1 && return 0
    return 1
}

lumen_systemd_has_any_units() {
    local unit
    for unit in "$@"; do
        if lumen_systemd_has_unit "${unit}"; then
            return 0
        fi
    done
    return 1
}

lumen_systemctl() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        systemctl "$@"
    elif command -v sudo >/dev/null 2>&1; then
        lumen_sudo systemctl "$@" || systemctl "$@"
    else
        return 1
    fi
}

lumen_install_optional_systemd_unit() {
    local tmp_dir="$1"
    local unit="$2"
    local warn_msg="$3"
    [ -f "${tmp_dir}/${unit}" ] || return 0
    lumen_run_as_root install -m 0644 "${tmp_dir}/${unit}" "${LUMEN_SYSTEMD_UNIT_DIR%/}/${unit}" \
        || log_warn "${warn_msg}"
}

lumen_ensure_backup_service_user() {
    local backup_root="${1:-${LUMEN_BACKUP_ROOT:-/opt/lumendata/backup}}"
    local user="${LUMEN_BACKUP_SERVICE_USER:-lumen-backup}"
    local group="${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}"
    local shell_path="/usr/sbin/nologin"
    [ -x "${shell_path}" ] || shell_path="/sbin/nologin"
    [ -x "${shell_path}" ] || shell_path="/bin/false"

    if command -v getent >/dev/null 2>&1; then
        if ! getent group "${group}" >/dev/null 2>&1; then
            lumen_run_as_root groupadd --system "${group}" 2>/dev/null \
                || log_warn "创建 ${group} 组失败；lumen-backup.service 可能无法启动。"
        fi
    fi
    if ! id "${user}" >/dev/null 2>&1; then
        lumen_run_as_root useradd --system --home-dir "${backup_root}" \
            --shell "${shell_path}" --gid "${group}" "${user}" 2>/dev/null \
            || log_warn "创建 ${user} 用户失败；lumen-backup.service 可能无法启动。"
    fi
    if command -v getent >/dev/null 2>&1 && getent group docker >/dev/null 2>&1; then
        lumen_run_as_root usermod -aG docker "${user}" 2>/dev/null \
            || log_warn "把 ${user} 加入 docker 组失败；备份服务可能无法访问 docker socket。"
    else
        log_warn "未找到 docker 组；请确保 ${user} 可访问 /var/run/docker.sock。"
    fi
    lumen_run_as_root mkdir -p "${backup_root}" 2>/dev/null || true
    lumen_run_as_root chgrp -R "${group}" "${backup_root}" 2>/dev/null || true
    lumen_run_as_root chmod -R g+rwX "${backup_root}" 2>/dev/null || true
}

lumen_enable_optional_systemd_unit() {
    local tmp_dir="$1"
    local unit="$2"
    local warn_msg="$3"
    [ -f "${tmp_dir}/${unit}" ] || return 0
    lumen_run_as_root systemctl enable --now "${unit}" || log_warn "${warn_msg}"
}

lumen_restart_systemd_units() {
    local units=()
    local unit
    for unit in "$@"; do
        if lumen_systemd_has_unit "${unit}"; then
            units+=("${unit}")
        else
            log_warn "未发现 systemd unit：${unit}，跳过。"
        fi
    done
    if [ "${#units[@]}" -eq 0 ]; then
        log_error "未发现可重启的 Lumen systemd unit。"
        return 1
    fi
    log_info "重启 systemd 服务：${units[*]}"
    if ! lumen_systemctl restart "${units[@]}"; then
        log_error "systemctl restart 失败：${units[*]}"
        log_error "如果脚本由管理后台触发，请确认运行用户有 sudo systemctl restart lumen-* 权限。"
        return 1
    fi
}

lumen_systemd_unit_active() {
    local unit="$1"
    if ! lumen_systemd_has_unit "${unit}"; then
        log_warn "未发现 ${unit}，跳过 active 检查。"
        return 0
    fi
    if ! lumen_systemctl is-active --quiet "${unit}"; then
        log_error "${unit} 未处于 active 状态。"
        if command -v journalctl >/dev/null 2>&1; then
            log_error "排查命令：journalctl -u ${unit} -n 160 --no-pager"
        fi
        return 1
    fi
}

lumen_check_runtime_health() {
    local api_url="${LUMEN_API_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
    local web_url="${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}"
    local api_attempts="${LUMEN_API_HEALTH_ATTEMPTS:-60}"
    local web_attempts="${LUMEN_WEB_HEALTH_ATTEMPTS:-60}"
    local check_worker="${LUMEN_CHECK_WORKER:-1}"
    local failed=0

    log_step "Lumen 运行时健康检查"
    if lumen_wait_for_http_ok "${api_url}" "${api_attempts}"; then
        log_info "API 健康检查通过：${api_url}"
    else
        log_error "API 健康检查失败：${api_url}"
        failed=1
    fi

    if lumen_wait_for_http_ok "${web_url}" "${web_attempts}"; then
        log_info "Web 健康检查通过：${web_url}"
    else
        log_error "Web 健康检查失败：${web_url}"
        failed=1
    fi

    if [ "${check_worker}" = "1" ]; then
        if lumen_systemd_has_unit lumen-worker.service; then
            lumen_systemd_unit_active lumen-worker.service || failed=1
        else
            log_warn "未发现 lumen-worker.service；请确认 Worker 进程已启动：cd apps/worker && uv run python -m arq app.main.WorkerSettings"
        fi
    fi

    return "${failed}"
}

LUMEN_LOCAL_RUNTIME_PIDS=()
LUMEN_LOCAL_RUNTIME_LOG_DIR=""

# 后台拉起的 API/Worker/Web PID 持久化文件。install.sh 退出后子进程不会主动结束，
# 写入这里方便 uninstall.sh 准确停掉，避免 8000/3000 端口悬空。
lumen_runtime_pid_file() {
    local root="$1"
    printf '%s/var/run/lumen-runtime.pids' "${root}"
}

lumen_persist_runtime_pids() {
    local root="$1"
    local pid_file
    pid_file="$(lumen_runtime_pid_file "${root}")"
    mkdir -p "$(dirname "${pid_file}")" 2>/dev/null || true
    : > "${pid_file}" 2>/dev/null || return 0
    local pid
    for pid in "${LUMEN_LOCAL_RUNTIME_PIDS[@]:-}"; do
        [ -n "${pid}" ] || continue
        printf '%s\n' "${pid}" >> "${pid_file}"
    done
    chmod 600 "${pid_file}" 2>/dev/null || true
}

lumen_stop_local_runtime_jobs() {
    local pid
    for pid in "${LUMEN_LOCAL_RUNTIME_PIDS[@]:-}"; do
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
    for pid in "${LUMEN_LOCAL_RUNTIME_PIDS[@]:-}"; do
        wait "${pid}" 2>/dev/null || true
    done
    LUMEN_LOCAL_RUNTIME_PIDS=()
}

# 列出占用某端口的 PID（每行一个）。优先 lsof，其次 ss，最后 netstat。
lumen_pids_listening_on_port() {
    local port="$1"
    local out=""
    if command -v lsof >/dev/null 2>&1; then
        out="$(lsof -tiTCP:"${port}" -sTCP:LISTEN -nP 2>/dev/null | sort -u || true)"
        if [ -z "${out}" ] && [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            out="$(lumen_run_as_root lsof -tiTCP:"${port}" -sTCP:LISTEN -nP 2>/dev/null | sort -u || true)"
        fi
        printf '%s\n' "${out}" | sed '/^$/d'
        if [ -n "${out}" ]; then
            return 0
        fi
    fi
    if command -v ss >/dev/null 2>&1; then
        # ss -ltnpH 输出含 users:(("name",pid=NNN,fd=...)) 的字段
        out="$(ss -ltnpH 2>/dev/null \
            | awk -v port="${port}" '$4 ~ ":"port"$" {print $0}' \
            | grep -oE 'pid=[0-9]+' \
            | awk -F= '{print $2}' \
            | sort -u || true)"
        if [ -z "${out}" ] && [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            out="$(lumen_run_as_root ss -ltnpH 2>/dev/null \
                | awk -v port="${port}" '$4 ~ ":"port"$" {print $0}' \
                | grep -oE 'pid=[0-9]+' \
                | awk -F= '{print $2}' \
                | sort -u || true)"
        fi
        printf '%s\n' "${out}" | sed '/^$/d'
        if [ -n "${out}" ]; then
            return 0
        fi
    fi
    if command -v netstat >/dev/null 2>&1; then
        out="$(netstat -ltnp 2>/dev/null \
            | awk -v port="${port}" '$4 ~ ":"port"$" {print $7}' \
            | awk -F'/' '{print $1}' \
            | grep -E '^[0-9]+$' \
            | sort -u || true)"
        if [ -z "${out}" ] && [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            out="$(lumen_run_as_root netstat -ltnp 2>/dev/null \
                | awk -v port="${port}" '$4 ~ ":"port"$" {print $7}' \
                | awk -F'/' '{print $1}' \
                | grep -E '^[0-9]+$' \
                | sort -u || true)"
        fi
        printf '%s\n' "${out}" | sed '/^$/d'
        return 0
    fi
    return 0
}

lumen_kill_pid() {
    local pid="$1"
    local signal="${2:-TERM}"
    [ -n "${pid}" ] || return 1
    kill "-${signal}" "${pid}" 2>/dev/null && return 0
    if [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        lumen_run_as_root kill "-${signal}" "${pid}" 2>/dev/null && return 0
    fi
    return 1
}

lumen_pid_exists() {
    local pid="$1"
    [ -n "${pid}" ] || return 1
    kill -0 "${pid}" 2>/dev/null && return 0
    if [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
        lumen_run_as_root kill -0 "${pid}" 2>/dev/null && return 0
    fi
    return 1
}

# 抓 PID 的命令行文本（用于识别是不是 lumen 自己起的进程）。
lumen_pid_cmdline() {
    local pid="$1"
    if [ -r "/proc/${pid}/cmdline" ]; then
        local proc_out
        proc_out="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null)"
        if [ -n "${proc_out}" ]; then
            printf '%s' "${proc_out}"
            return 0
        fi
    fi
    if command -v ps >/dev/null 2>&1; then
        # macOS ps 没有 /proc，用 -o command= 拿全命令行
        local out
        out="$(ps -o command= -p "${pid}" 2>/dev/null)"
        if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
        out="$(ps -o args= -p "${pid}" 2>/dev/null)"
        if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
        if [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            out="$(lumen_run_as_root ps -o command= -p "${pid}" 2>/dev/null)"
            if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
            out="$(lumen_run_as_root ps -o args= -p "${pid}" 2>/dev/null)"
            if [ -n "${out}" ]; then printf '%s' "${out}"; return 0; fi
        fi
    fi
    return 1
}

# 抓 PID 的工作目录。next-server 子进程把 process.title 改成 "next-server (v...)"，
# cmdline 已不含项目路径，必须用 cwd 才能确认是不是 lumen 起的。
lumen_pid_cwd() {
    local pid="$1"
    [ -n "${pid}" ] || return 0
    if [ -L "/proc/${pid}/cwd" ]; then
        readlink -f "/proc/${pid}/cwd" 2>/dev/null
        return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        # macOS 的 lsof 即便加 -p PID 仍可能输出全表，必须先匹配 p<pid> 再取它后面的 n 行。
        local out
        out="$(lsof -p "${pid}" -d cwd -Fpn 2>/dev/null \
            | awk -v pid="${pid}" '
                /^p/ { current = substr($0, 2); next }
                /^n/ && current == pid { sub(/^n/, ""); print; exit }
            ' || true)"
        if [ -z "${out}" ] && [ "${EUID:-$(id -u)}" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            out="$(lumen_run_as_root lsof -p "${pid}" -d cwd -Fpn 2>/dev/null \
                | awk -v pid="${pid}" '
                    /^p/ { current = substr($0, 2); next }
                    /^n/ && current == pid { sub(/^n/, ""); print; exit }
                ' || true)"
        fi
        printf '%s' "${out}"
    fi
}

# 判断给定 PID 是否 lumen 后台运行时（uvicorn app.main:app / arq app.main / next-server / npm run dev|start）。
# 防止误杀同机其它 nodejs / python 服务。
lumen_is_lumen_runtime_process() {
    local pid="$1"
    [ -n "${pid}" ] || return 1
    local cmd
    cmd="$(lumen_pid_cmdline "${pid}")"
    case "${cmd}" in
        *"uvicorn app.main:app"*) return 0 ;;
        *"arq app.main.WorkerSettings"*) return 0 ;;
        *"app.main.WorkerSettings"*) return 0 ;;
        # next-server (v...)  —— next 子进程改 process.title 后 cmdline 通常就这么短，
        # 不含 apps/web。cwd 兜底（见下）会再校验一次是不是真在 lumen 项目内。
        *"next-server"*) return 0 ;;
        *"node"*"node_modules/next/dist"*) return 0 ;;
        *"node"*"node_modules/.bin/next"*) return 0 ;;
        *"npm run dev"*|*"npm run start"*|*"npm exec next"*) return 0 ;;
        *"next dev"*|*"next start"*) return 0 ;;
    esac
    # cmdline 包含 apps/* 路径的兜底（uvicorn 走 uv run 可能少 uvicorn 字面量）
    case "${cmd}" in
        *"apps/api"*"uvicorn"*|*"apps/api"*"app.main"*) return 0 ;;
        *"apps/worker"*"arq"*|*"apps/worker"*"app.main"*) return 0 ;;
    esac
    # 进程 cwd 在 apps/api|worker|web 下：next-server 等改了 process.title 的进程靠这个识别
    local cwd
    cwd="$(lumen_pid_cwd "${pid}")"
    case "${cwd}" in
        */apps/api|*/apps/api/*) return 0 ;;
        */apps/worker|*/apps/worker/*) return 0 ;;
        */apps/web|*/apps/web/*) return 0 ;;
    esac
    return 1
}

# 关掉 PID 文件中记录的 lumen 进程（先 SIGTERM，最多等 wait_seconds 秒，必要时 SIGKILL）。
# 返回值非零表示有部分进程仍在；调用方按需要再处理。
lumen_stop_persisted_runtime() {
    local root="$1"
    local wait_seconds="${2:-15}"
    local pid_file
    pid_file="$(lumen_runtime_pid_file "${root}")"
    [ -f "${pid_file}" ] || return 0
    local -a pids=()
    local line
    while IFS= read -r line; do
        line="${line//[[:space:]]/}"
        [[ "${line}" =~ ^[0-9]+$ ]] || continue
        pids+=("${line}")
    done < "${pid_file}"
    if [ "${#pids[@]}" -eq 0 ]; then
        rm -f "${pid_file}" 2>/dev/null || true
        return 0
    fi
    local pid sent=0
    for pid in "${pids[@]}"; do
        if lumen_pid_exists "${pid}" && lumen_is_lumen_runtime_process "${pid}"; then
            lumen_kill_pid "${pid}" TERM || true
            sent=1
        fi
    done
    if [ "${sent}" -eq 1 ]; then
        local _i
        for _i in $(seq 1 "${wait_seconds}"); do
            local alive=0
            for pid in "${pids[@]}"; do
                if lumen_pid_exists "${pid}" && lumen_is_lumen_runtime_process "${pid}"; then
                    alive=1
                    break
                fi
            done
            [ "${alive}" -eq 0 ] && break
            sleep 1
        done
        for pid in "${pids[@]}"; do
            if lumen_pid_exists "${pid}" && lumen_is_lumen_runtime_process "${pid}"; then
                lumen_kill_pid "${pid}" KILL || true
            fi
        done
    fi
    rm -f "${pid_file}" 2>/dev/null || true
}

# 扫描端口 → 找 lumen 自己的进程 → kill。返回 0 表示端口已腾出（或本来就空闲）。
# 输入：端口号 + 用途描述（仅日志）。
lumen_release_port_if_lumen() {
    local port="$1"
    local label="${2:-port ${port}}"
    if ! lumen_process_listening_on_port "${port}"; then
        return 0
    fi
    local -a pids=()
    local pid
    while IFS= read -r pid; do
        [ -n "${pid}" ] || continue
        pids+=("${pid}")
    done < <(lumen_pids_listening_on_port "${port}")
    if [ "${#pids[@]}" -eq 0 ]; then
        # 拿不到 PID（无 root / 无 lsof / docker-proxy 占的端口都可能落到这里）。
        # 让调用方按 docker 或外部进程去处理，这里返回非 0。
        return 1
    fi
    local matched=0
    for pid in "${pids[@]}"; do
        if lumen_is_lumen_runtime_process "${pid}"; then
            log_warn "${label}：发现 Lumen 残留进程 pid=${pid}，发送 SIGTERM。"
            lumen_kill_pid "${pid}" TERM || true
            matched=1
        fi
    done
    if [ "${matched}" -eq 0 ]; then
        log_warn "${label}：被非 Lumen 进程占用，PID=${pids[*]}。"
        return 1
    fi
    local _i
    for _i in $(seq 1 10); do
        if ! lumen_process_listening_on_port "${port}"; then
            return 0
        fi
        sleep 1
    done
    for pid in "${pids[@]}"; do
        if lumen_pid_exists "${pid}" && lumen_is_lumen_runtime_process "${pid}"; then
            log_warn "${label}：pid=${pid} 未在 SIGTERM 后退出，发送 SIGKILL。"
            lumen_kill_pid "${pid}" KILL || true
        fi
    done
    sleep 1
    if lumen_process_listening_on_port "${port}"; then
        return 1
    fi
    return 0
}

lumen_tail_runtime_log() {
    local name="$1"
    local file="$2"
    if [ -s "${file}" ]; then
        log_error "${name} 最近日志："
        tail -n "${LUMEN_RUNTIME_LOG_TAIL_LINES:-80}" "${file}" >&2 || true
    fi
}

# 启动新进程前先把端口腾出来：lumen 自己的旧进程主动 kill；外部进程则报错。
# 返回 0 表示端口现在空闲，可以启动；返回 1 表示外部占用，调用方应放弃启动。
lumen_prepare_port_for_runtime() {
    local port="$1"
    local label="$2"
    if ! lumen_process_listening_on_port "${port}"; then
        return 0
    fi
    log_info "${label} 启动前发现 ${port} 已被占用，尝试释放（仅限 Lumen 自家进程）。"
    if lumen_release_port_if_lumen "${port}" "${label}"; then
        return 0
    fi
    log_error "${label}：端口 ${port} 被外部进程占用，无法启动。"
    log_error "排查命令：lsof -iTCP:${port} -sTCP:LISTEN -nP   或   ss -ltnp \"sport = :${port}\""
    return 1
}

lumen_start_local_runtime() {
    local root="$1"
    local web_npm_script="${2:-dev}"
    local api_log worker_log web_log
    local api_pid="" worker_pid="" web_pid=""
    local failed=0

    LUMEN_LOCAL_RUNTIME_LOG_DIR="${root}/.install-logs/runtime.$(date '+%Y%m%d%H%M%S')"
    mkdir -p "${LUMEN_LOCAL_RUNTIME_LOG_DIR}"
    api_log="${LUMEN_LOCAL_RUNTIME_LOG_DIR}/api.log"
    worker_log="${LUMEN_LOCAL_RUNTIME_LOG_DIR}/worker.log"
    web_log="${LUMEN_LOCAL_RUNTIME_LOG_DIR}/web.log"

    log_step "启动 Lumen 运行时进程"

    # 上一次 install/start 写下的 PID 文件是最可靠的清理来源；先按 PID
    # 停掉，再用端口扫描兜底处理未记录或旧版本残留的进程。
    lumen_stop_persisted_runtime "${root}" "${LUMEN_RUNTIME_STOP_WAIT_SECONDS:-15}" || true

    # 先把 8000/3000 上的旧 Lumen 进程清掉。否则这次启动的新 API/Web 会因 EADDRINUSE
    # 立刻退出，而旧进程仍在响应 healthz，健康检查会假阳性通过。
    if ! lumen_prepare_port_for_runtime 8000 "API"; then
        failed=1
    else
        log_info "启动 API → ${api_log}"
        (
            cd "${root}/apps/api" || exit 1
            exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
        ) >"${api_log}" 2>&1 &
        api_pid="$!"
        LUMEN_LOCAL_RUNTIME_PIDS+=("${api_pid}")
    fi

    log_info "启动 Worker → ${worker_log}"
    (
        cd "${root}/apps/worker" || exit 1
        exec uv run python -m arq app.main.WorkerSettings
    ) >"${worker_log}" 2>&1 &
    worker_pid="$!"
    LUMEN_LOCAL_RUNTIME_PIDS+=("${worker_pid}")

    if ! lumen_prepare_port_for_runtime 3000 "Web"; then
        failed=1
    else
        log_info "启动 Web → ${web_log}"
        (
            cd "${root}/apps/web" || exit 1
            exec npm run "${web_npm_script}"
        ) >"${web_log}" 2>&1 &
        web_pid="$!"
        LUMEN_LOCAL_RUNTIME_PIDS+=("${web_pid}")
    fi

    sleep "${LUMEN_WORKER_START_GRACE_SECONDS:-3}"
    # 校验三个 PID 仍然存活：旧进程会让 healthz 假阳性，
    # 只有「我们刚启动的进程还活着」才算真启动成功。
    if [ -n "${api_pid}" ] && ! kill -0 "${api_pid}" 2>/dev/null; then
        log_error "API 启动后立即退出（常见原因：端口 8000 冲突 / DATABASE_URL 错误 / .env 不完整）。"
        lumen_tail_runtime_log "API" "${api_log}"
        failed=1
    fi
    if [ -n "${worker_pid}" ] && ! kill -0 "${worker_pid}" 2>/dev/null; then
        log_error "Worker 启动后立即退出。"
        lumen_tail_runtime_log "Worker" "${worker_log}"
        failed=1
    fi
    if [ -n "${web_pid}" ] && ! kill -0 "${web_pid}" 2>/dev/null; then
        log_error "Web 启动后立即退出（常见原因：端口 3000 被旧进程占用 / npm build 残缺）。"
        lumen_tail_runtime_log "Web" "${web_log}"
        failed=1
    fi

    if [ "${failed}" -eq 0 ] && ! LUMEN_CHECK_WORKER=0 lumen_check_runtime_health; then
        failed=1
    fi

    if [ "${failed}" -ne 0 ]; then
        lumen_tail_runtime_log "API" "${api_log}"
        lumen_tail_runtime_log "Web" "${web_log}"
        log_error "运行时健康检查失败，日志目录：${LUMEN_LOCAL_RUNTIME_LOG_DIR}"
        lumen_stop_local_runtime_jobs
        return 1
    fi

    # 把后台 PID 写到 var/run/lumen-runtime.pids，方便 uninstall.sh 在不同 shell
    # 进程里也能停掉这些后台运行时；否则 8000/3000 会被悬空进程一直占住。
    lumen_persist_runtime_pids "${root}"
    log_info "运行时日志目录：${LUMEN_LOCAL_RUNTIME_LOG_DIR}"
}
