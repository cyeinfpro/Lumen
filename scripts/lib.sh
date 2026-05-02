#!/usr/bin/env bash
# Lumen 公用 bash 库：颜色日志、交互输入、命令检查、路径解析。
# 由 install.sh / update.sh / uninstall.sh source 引入。
# 不要直接执行本文件。

if [ -z "${BASH_VERSION:-}" ]; then
    echo "scripts/lib.sh requires bash. Please run scripts with bash, not sh." >&2
    exit 1
fi

# 颜色（仅在 stdout 是 tty 时输出转义码，避免日志文件被污染）
if [ -t 1 ]; then
    LUMEN_C_RESET="$(printf '\033[0m')"
    LUMEN_C_BOLD="$(printf '\033[1m')"
    LUMEN_C_RED="$(printf '\033[31m')"
    LUMEN_C_YELLOW="$(printf '\033[33m')"
    LUMEN_C_GREEN="$(printf '\033[32m')"
    LUMEN_C_BLUE="$(printf '\033[34m')"
    LUMEN_C_CYAN="$(printf '\033[36m')"
else
    LUMEN_C_RESET=""
    LUMEN_C_BOLD=""
    LUMEN_C_RED=""
    LUMEN_C_YELLOW=""
    LUMEN_C_GREEN=""
    LUMEN_C_BLUE=""
    LUMEN_C_CYAN=""
fi

log_info() {
    printf '%s[INFO]%s %s\n' "${LUMEN_C_GREEN}" "${LUMEN_C_RESET}" "$*"
}

log_warn() {
    printf '%s[WARN]%s %s\n' "${LUMEN_C_YELLOW}" "${LUMEN_C_RESET}" "$*" >&2
}

log_error() {
    printf '%s[ERROR]%s %s\n' "${LUMEN_C_RED}" "${LUMEN_C_RESET}" "$*" >&2
}

log_step() {
    printf '\n%s%s==>%s %s%s%s\n' \
        "${LUMEN_C_BOLD}" "${LUMEN_C_BLUE}" "${LUMEN_C_RESET}" \
        "${LUMEN_C_BOLD}" "$*" "${LUMEN_C_RESET}"
}

LUMEN_DOCKER_USE_SUDO="${LUMEN_DOCKER_USE_SUDO:-0}"

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
            log_error "已有 Lumen 维护脚本在运行，当前 ${script_name} 退出。"
            log_error "锁目录：${lock_dir}"
            log_error "如果确认没有脚本在运行，可手动删除该锁目录后重试。"
            exit 1
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

lumen_handle_signal() {
    local signal="$1"
    local line="${2:-unknown}"
    local code=1
    case "${signal}" in
        INT) code=130 ;;
        TERM) code=143 ;;
    esac
    trap - INT TERM
    log_error "收到 ${signal}，脚本已中断（第 ${line} 行）。"
    exit "${code}"
}

lumen_install_signal_handlers() {
    trap 'lumen_handle_signal INT "${LINENO}"' INT
    trap 'lumen_handle_signal TERM "${LINENO}"' TERM
}

# confirm "msg"  ->  返回 0 表示 yes，1 表示 no/默认。
# 默认 N，用户必须明确输入 y/Y/yes/YES 才返回 0。
confirm() {
    local prompt="$1"
    local reply=""
    printf '%s%s%s [y/N]: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}"
    if ! IFS= read -r reply; then
        printf '\n'
        return 1
    fi
    case "${reply}" in
        y|Y|yes|YES|Yes) return 0 ;;
        *) return 1 ;;
    esac
}

# ensure_cmd <name> <install_hint>
# 检查命令是否存在；不存在则打印安装提示并 exit 1。
ensure_cmd() {
    local name="$1"
    local hint="${2:-}"
    if command -v "${name}" >/dev/null 2>&1; then
        return 0
    fi
    log_error "缺少命令 \"${name}\"。请先安装后重试。"
    if [ -n "${hint}" ]; then
        printf '       建议安装方式：%s\n' "${hint}" >&2
    fi
    exit 1
}

sudo_has_tty() {
    [ -r /dev/tty ] && [ -w /dev/tty ]
}

lumen_sudo() {
    if sudo_has_tty; then
        sudo "$@"
    else
        sudo -n "$@"
    fi
}

lumen_docker() {
    if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
        lumen_sudo docker "$@"
    else
        docker "$@"
    fi
}

lumen_docker_command_label() {
    if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
        printf 'sudo docker'
    else
        printf 'docker'
    fi
}

lumen_detect_docker_access() {
    LUMEN_DOCKER_USE_SUDO=0
    command -v docker >/dev/null 2>&1 || return 1

    if docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
        return 0
    fi

    if [ "$(detect_os)" = "linux" ] \
        && [ "${EUID:-$(id -u)}" -ne 0 ] \
        && command -v sudo >/dev/null 2>&1; then
        if lumen_sudo docker compose version >/dev/null 2>&1 \
            && lumen_sudo docker info >/dev/null 2>&1; then
            LUMEN_DOCKER_USE_SUDO=1
            return 0
        fi
    fi

    return 1
}

lumen_require_docker_access() {
    ensure_cmd docker "请安装 Docker 后重试"
    if lumen_detect_docker_access; then
        if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
            log_warn "当前用户无法直接访问 Docker，本次将自动使用 sudo docker。"
        fi
        return 0
    fi

    if ! docker compose version >/dev/null 2>&1; then
        log_error "未检测到 docker compose v2。请升级 Docker。"
    else
        log_error "Docker daemon 未运行，或当前用户无权访问 Docker。"
    fi
    if [ "$(detect_os)" = "linux" ]; then
        log_error "请先启动 Docker：sudo systemctl start docker；若是权限问题，可将用户加入 docker 组后重新登录。"
    else
        log_error "请确认 Docker Desktop 已启动并完成初始化。"
    fi
    exit 1
}

# detect_os -> 输出 macos/linux/unknown
detect_os() {
    local uname_s
    uname_s="$(uname -s 2>/dev/null || echo unknown)"
    case "${uname_s}" in
        Darwin) printf 'macos\n' ;;
        Linux) printf 'linux\n' ;;
        *) printf 'unknown\n' ;;
    esac
}

# port_in_use <port> -> 返回 0 表示被占用，1 表示空闲（或无可用检测工具）
# 优先 lsof，其次 ss，再次 netstat。
port_in_use() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"${port}" -sTCP:LISTEN -nP >/dev/null 2>&1; then
            return 0
        fi
        return 1
    fi
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn 2>/dev/null | awk 'NR>1 {print $4}' | grep -qE "[:.]${port}\$"; then
            return 0
        fi
        return 1
    fi
    if command -v netstat >/dev/null 2>&1; then
        if netstat -an 2>/dev/null | awk '/LISTEN/ {print $4}' | grep -qE "[:.]${port}\$"; then
            return 0
        fi
        return 1
    fi
    return 1
}

lumen_process_listening_on_port() {
    port_in_use "$1"
}

lumen_http_status() {
    local url="$1"
    if ! command -v curl >/dev/null 2>&1; then
        return 1
    fi
    curl -sS -o /dev/null -w '%{http_code}' \
        --connect-timeout "${LUMEN_HEALTH_CONNECT_TIMEOUT:-2}" \
        --max-time "${LUMEN_HEALTH_MAX_TIME:-8}" \
        "${url}" 2>/dev/null
}

lumen_wait_for_http_ok() {
    local url="$1"
    local attempts="${2:-60}"
    local status=""
    local _attempt
    for _attempt in $(seq 1 "${attempts}"); do
        status="$(lumen_http_status "${url}" || true)"
        case "${status}" in
            2??|3??) return 0 ;;
        esac
        sleep 1
    done
    return 1
}

lumen_wait_for_port() {
    local port="$1"
    local attempts="${2:-60}"
    local _attempt
    for _attempt in $(seq 1 "${attempts}"); do
        if lumen_process_listening_on_port "${port}"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

lumen_env_value() {
    local key="$1"
    local file="${2:-.env}"
    local raw=""
    raw="$(sed -n "s/^${key}=//p" "${file}" 2>/dev/null | head -n1 || true)"
    raw="${raw%$'\r'}"
    if [[ "${raw}" == \'*\' && "${raw}" == *\' ]]; then
        raw="${raw:1:${#raw}-2}"
    elif [[ "${raw}" == \"*\" && "${raw}" == *\" ]]; then
        raw="${raw:1:${#raw}-2}"
    fi
    printf '%s' "${raw}"
}

lumen_run_as_root() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        lumen_sudo "$@"
    else
        return 1
    fi
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

lumen_tail_runtime_log() {
    local name="$1"
    local file="$2"
    if [ -s "${file}" ]; then
        log_error "${name} 最近日志："
        tail -n "${LUMEN_RUNTIME_LOG_TAIL_LINES:-80}" "${file}" >&2 || true
    fi
}

lumen_start_local_runtime() {
    local root="$1"
    local web_npm_script="${2:-dev}"
    local api_log worker_log web_log worker_pid=""
    local failed=0

    LUMEN_LOCAL_RUNTIME_LOG_DIR="${root}/.install-logs/runtime.$(date '+%Y%m%d%H%M%S')"
    mkdir -p "${LUMEN_LOCAL_RUNTIME_LOG_DIR}"
    api_log="${LUMEN_LOCAL_RUNTIME_LOG_DIR}/api.log"
    worker_log="${LUMEN_LOCAL_RUNTIME_LOG_DIR}/worker.log"
    web_log="${LUMEN_LOCAL_RUNTIME_LOG_DIR}/web.log"

    log_step "启动 Lumen 运行时进程"

    if lumen_process_listening_on_port 8000; then
        log_warn "端口 8000 已有进程监听，跳过启动 API。"
    else
        log_info "启动 API → ${api_log}"
        (
            cd "${root}/apps/api"
            exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
        ) >"${api_log}" 2>&1 &
        LUMEN_LOCAL_RUNTIME_PIDS+=("$!")
    fi

    log_info "启动 Worker → ${worker_log}"
    (
        cd "${root}/apps/worker"
        exec uv run python -m arq app.main.WorkerSettings
    ) >"${worker_log}" 2>&1 &
    worker_pid="$!"
    LUMEN_LOCAL_RUNTIME_PIDS+=("${worker_pid}")

    if lumen_process_listening_on_port 3000; then
        log_warn "端口 3000 已有进程监听，跳过启动 Web。"
    else
        log_info "启动 Web → ${web_log}"
        (
            cd "${root}/apps/web"
            exec npm run "${web_npm_script}"
        ) >"${web_log}" 2>&1 &
        LUMEN_LOCAL_RUNTIME_PIDS+=("$!")
    fi

    sleep "${LUMEN_WORKER_START_GRACE_SECONDS:-3}"
    if [ -n "${worker_pid}" ] && ! kill -0 "${worker_pid}" 2>/dev/null; then
        log_error "Worker 启动后立即退出。"
        lumen_tail_runtime_log "Worker" "${worker_log}"
        failed=1
    fi

    if ! LUMEN_CHECK_WORKER=0 lumen_check_runtime_health; then
        failed=1
    fi

    if [ "${failed}" -ne 0 ]; then
        lumen_tail_runtime_log "API" "${api_log}"
        lumen_tail_runtime_log "Web" "${web_log}"
        log_error "运行时健康检查失败，日志目录：${LUMEN_LOCAL_RUNTIME_LOG_DIR}"
        lumen_stop_local_runtime_jobs
        return 1
    fi

    log_info "运行时日志目录：${LUMEN_LOCAL_RUNTIME_LOG_DIR}"
}

# lumen_root —— 解析 BASH_SOURCE[1] 所在目录的上级（项目根目录）
# 注意：本函数依赖调用者通过 source 引入本文件，从其所在的脚本路径反推。
# 用法： ROOT="$(lumen_root)"
lumen_root() {
    local src="${BASH_SOURCE[1]:-${BASH_SOURCE[0]}}"
    local script_dir
    script_dir="$(cd "$(dirname "${src}")" && pwd)"
    # script_dir 形如 /path/to/lumen/scripts，取上级
    (cd "${script_dir}/.." && pwd)
}

# read_or_default "msg" "default" -> 输出用户输入或默认值
# 用法：  val="$(read_or_default '提示' '默认值')"
read_or_default() {
    local prompt="$1"
    local default="${2:-}"
    local reply=""
    if [ -n "${default}" ]; then
        printf '%s%s%s [%s]: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}" "${default}" >&2
    else
        printf '%s%s%s: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}" >&2
    fi
    if [ -r /dev/tty ] && IFS= read -r reply 2>/dev/null </dev/tty; then
        :
    elif ! IFS= read -r reply; then
        reply=""
    fi
    if [ -z "${reply}" ]; then
        printf '%s' "${default}"
    else
        printf '%s' "${reply}"
    fi
}

# read_secret "msg" -> 用 read -s 静默读密码，输出到 stdout（用法： pwd="$(read_secret 'Password')"）
read_secret() {
    local prompt="$1"
    local reply=""
    printf '%s%s%s: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}" >&2
    # -s 静默读取；某些终端不支持 -s，则降级
    if [ -r /dev/tty ] && IFS= read -rs reply </dev/tty 2>/dev/null; then
        printf '\n' >&2
    elif [ -r /dev/tty ]; then
        if ! IFS= read -r reply </dev/tty; then
            reply=""
        fi
    else
        if ! IFS= read -r reply; then
            reply=""
        fi
    fi
    printf '%s' "${reply}"
}
