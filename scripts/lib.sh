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
