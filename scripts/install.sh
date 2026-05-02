#!/usr/bin/env bash
# Lumen 一键安装脚本
# 用法：  bash scripts/install.sh            # 打开运维菜单
#        bash scripts/install.sh --install  # 直接安装
# 行为：检查/自动安装依赖 -> 写 .env -> 起 PG/Redis -> uv sync
#       -> alembic upgrade -> 创建 admin -> npm ci -> 可选 build。
# 重复执行安全（幂等），中途任何失败都会立即停止。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || pwd)"

raw_have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

raw_refresh_tool_paths() {
    if [ -d /opt/homebrew/bin ]; then
        PATH="/opt/homebrew/bin:${PATH}"
    fi
    if [ -d /usr/local/bin ]; then
        PATH="/usr/local/bin:${PATH}"
    fi
    export PATH
}

raw_run_as_root() {
    if [ "${EUID:-$(id -u 2>/dev/null || echo 1)}" -eq 0 ]; then
        "$@"
    elif raw_have_cmd sudo; then
        sudo "$@"
    else
        return 1
    fi
}

raw_install_packages() {
    if raw_have_cmd apt-get; then
        raw_run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update
        raw_run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
    elif raw_have_cmd dnf; then
        raw_run_as_root dnf install -y "$@"
    elif raw_have_cmd yum; then
        raw_run_as_root yum install -y "$@"
    elif raw_have_cmd pacman; then
        raw_run_as_root pacman -Sy --noconfirm "$@"
    elif raw_have_cmd zypper; then
        raw_run_as_root zypper --non-interactive install "$@"
    elif raw_have_cmd apk; then
        raw_run_as_root apk add --no-cache "$@"
    else
        return 1
    fi
}

raw_install_git() {
    printf '[INFO] 缺少 git，尝试自动安装。\n'
    raw_refresh_tool_paths
    case "$(uname -s 2>/dev/null || echo unknown)" in
        Darwin)
            if raw_have_cmd brew; then
                brew install git
            elif raw_have_cmd curl; then
                printf '[INFO] 缺少 Homebrew，尝试先自动安装 Homebrew。\n'
                NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
                if [ -x /opt/homebrew/bin/brew ]; then
                    /opt/homebrew/bin/brew install git
                elif [ -x /usr/local/bin/brew ]; then
                    /usr/local/bin/brew install git
                else
                    return 1
                fi
                raw_refresh_tool_paths
            else
                return 1
            fi
            ;;
        Linux)
            raw_install_packages git ca-certificates curl
            ;;
        *)
            return 1
            ;;
    esac
}

bootstrap_from_raw_script() {
    local repo_url="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    local branch="${LUMEN_BRANCH:-main}"
    local install_dir="${LUMEN_INSTALL_DIR:-${HOME:-$PWD}/Lumen}"

    printf '[INFO] 当前脚本不是在完整 Lumen 仓库内运行，将进入远程 bootstrap 模式。\n'
    printf '[INFO] 仓库：%s\n' "${repo_url}"
    printf '[INFO] 分支：%s\n' "${branch}"
    printf '[INFO] 目录：%s\n' "${install_dir}"

    if ! raw_have_cmd git; then
        if ! raw_install_git || ! raw_have_cmd git; then
            printf '[ERROR] 缺少 git，且自动安装失败，无法从 GitHub 拉取 Lumen。\n' >&2
            printf '        请确认当前用户有 sudo 权限，或手动安装 git 后重试。\n' >&2
            printf '        手动拉取命令：git clone %s\n' "${repo_url}" >&2
            exit 1
        fi
        printf '[INFO] git 已安装。\n'
    fi

    if ! raw_have_cmd git; then
        printf '[ERROR] 缺少 git，无法从 GitHub 拉取 Lumen。\n' >&2
        printf '        请先安装 git，或手动执行：git clone %s\n' "${repo_url}" >&2
        exit 1
    fi

    if [ -d "${install_dir}/.git" ]; then
        printf '[INFO] 目录已存在，尝试拉取最新代码。\n'
        git -C "${install_dir}" fetch origin "${branch}"
        git -C "${install_dir}" checkout "${branch}"
        git -C "${install_dir}" pull --ff-only origin "${branch}"
    elif [ -e "${install_dir}" ]; then
        printf '[ERROR] 目标目录已存在但不是 git 仓库：%s\n' "${install_dir}" >&2
        printf '        请移走该目录，或设置 LUMEN_INSTALL_DIR 指向一个新目录。\n' >&2
        exit 1
    else
        git clone --branch "${branch}" "${repo_url}" "${install_dir}"
    fi

    if [ -r /dev/tty ]; then
        exec bash "${install_dir}/scripts/install.sh" "$@" </dev/tty
    fi
    printf '[ERROR] 无法打开 /dev/tty 读取安装参数。\n' >&2
    printf '        请改用：bash %s/scripts/install.sh\n' "${install_dir}" >&2
    exit 1
}

if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    bootstrap_from_raw_script "$@"
fi

# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OS="$(detect_os)"
LOCK_DIR="${ROOT}/.lumen-script.lock"
LOCK_HELD=0
PARALLEL_PIDS=()

usage() {
    cat <<EOF
Lumen 安装入口

用法：
  bash scripts/install.sh              打开运维菜单
  bash scripts/install.sh --install    直接安装 Lumen
  bash scripts/install.sh --update     更新 Lumen
  bash scripts/install.sh --uninstall  卸载 Lumen

EOF
}

dispatch_entrypoint() {
    local command="${1:-menu}"
    case "${command}" in
        menu|--menu)
            exec bash "${SCRIPT_DIR}/lumenctl.sh" menu
            ;;
        install|--install)
            shift || true
            if [ "$#" -gt 0 ]; then
                usage
                log_error "安装命令不接受额外参数：$*"
                exit 1
            fi
            ;;
        update|--update)
            exec bash "${SCRIPT_DIR}/update.sh"
            ;;
        uninstall|--uninstall)
            exec bash "${SCRIPT_DIR}/uninstall.sh"
            ;;
        help|-h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            log_error "未知命令：${command}"
            exit 1
            ;;
    esac
}

dispatch_entrypoint "$@"

on_error() {
    local line="$1"
    log_error "安装失败：第 ${line} 行返回非零状态。请查看上方输出修正后重试。"
}

kill_parallel_jobs() {
    local pid
    for pid in "${PARALLEL_PIDS[@]:-}"; do
        if kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
    for pid in "${PARALLEL_PIDS[@]:-}"; do
        wait "${pid}" 2>/dev/null || true
    done
    PARALLEL_PIDS=()
}

release_script_lock() {
    if [ "${LOCK_HELD}" -eq 1 ]; then
        rm -rf "${LOCK_DIR}"
        LOCK_HELD=0
    fi
}

cleanup() {
    local rc=$?
    trap - EXIT INT TERM ERR
    kill_parallel_jobs
    release_script_lock
    return "${rc}"
}

on_signal() {
    local signal_name="$1"
    local rc="$2"
    log_error "安装被 ${signal_name} 中断，正在清理后台任务和脚本锁。"
    kill_parallel_jobs
    exit "${rc}"
}

acquire_script_lock() {
    local lock_pid=""
    local started_at=""

    if mkdir "${LOCK_DIR}" 2>/dev/null; then
        LOCK_HELD=1
        {
            printf 'pid=%s\n' "$$"
            printf 'script=%s\n' "install.sh"
            printf 'started_at=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z' 2>/dev/null || true)"
        } > "${LOCK_DIR}/info"
        return 0
    fi

    if [ -f "${LOCK_DIR}/info" ]; then
        lock_pid="$(sed -n 's/^pid=//p' "${LOCK_DIR}/info" | head -n1 || true)"
        started_at="$(sed -n 's/^started_at=//p' "${LOCK_DIR}/info" | head -n1 || true)"
    fi

    if [[ "${lock_pid}" =~ ^[0-9]+$ ]] && kill -0 "${lock_pid}" 2>/dev/null; then
        log_error "另一个 Lumen 运维脚本正在运行（pid=${lock_pid}${started_at:+, started_at=${started_at}}）。"
        log_error "为避免 .env、依赖安装和 docker compose 竞态，本次安装已停止。"
        exit 1
    fi

    if [ ! -e "${LOCK_DIR}" ]; then
        log_error "无法创建脚本锁 ${LOCK_DIR}。请检查项目目录写权限后重试。"
        exit 1
    fi

    log_warn "发现陈旧脚本锁 ${LOCK_DIR}，将清理后重试加锁。"
    rm -rf "${LOCK_DIR}"
    if ! mkdir "${LOCK_DIR}" 2>/dev/null; then
        log_error "无法获取脚本锁 ${LOCK_DIR}。请确认没有 install/update/uninstall 正在运行后重试。"
        exit 1
    fi
    LOCK_HELD=1
    {
        printf 'pid=%s\n' "$$"
        printf 'script=%s\n' "install.sh"
        printf 'started_at=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z' 2>/dev/null || true)"
    } > "${LOCK_DIR}/info"
}

contains_control_chars() {
    local value="$1"
    printf '%s' "${value}" | LC_ALL=C grep -q '[[:cntrl:]]'
}

validate_dotenv_value() {
    local name="$1"
    local value="$2"
    if contains_control_chars "${value}"; then
        log_error "${name} 不能包含换行、制表符或其他控制字符。"
        return 1
    fi
    if [[ "${value}" == *"'"* ]]; then
        log_error "${name} 不能包含单引号，以免破坏 .env 引号边界。"
        return 1
    fi
    return 0
}

validate_redis_password() {
    local value="$1"
    validate_dotenv_value "REDIS_PASSWORD" "${value}" || return 1
    if [[ ! "${value}" =~ ^[A-Za-z0-9._~-]+$ ]]; then
        log_error "REDIS_PASSWORD 只能包含 URL 安全字符：A-Z a-z 0-9 . _ ~ -。"
        log_error "请避免 @、:、/、?、#、%、空格等会破坏 REDIS_URL 的字符。"
        return 1
    fi
    return 0
}

dotenv_quote() {
    local name="$1"
    local value="$2"
    validate_dotenv_value "${name}" "${value}" || return 1
    printf "'%s'" "${value}"
}

json_escape_string() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s' "${value}"
}

read_dotenv_value() {
    local key="$1"
    local file="$2"
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

ensure_compose_db_env_vars() {
    local file="$1"
    if grep -qE '^DB_USER=' "${file}" \
        && grep -qE '^DB_PASSWORD=' "${file}" \
        && grep -qE '^DB_NAME=' "${file}"; then
        return 0
    fi
    if ! grep -qE '^DATABASE_URL=' "${file}"; then
        log_error "${file} 缺少 DB_USER/DB_PASSWORD/DB_NAME，且无法从 DATABASE_URL 推导。"
        log_error "请补充 DB_USER、DB_PASSWORD、DB_NAME 后重跑。"
        exit 1
    fi
    python3 - "${file}" <<'PY'
from pathlib import Path
from urllib.parse import unquote, urlsplit
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
values = {}
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, raw = line.split("=", 1)
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    values[key.strip()] = raw

url = values.get("DATABASE_URL", "")
parts = urlsplit(url)
db_user = unquote(parts.username or "")
db_password = unquote(parts.password or "")
db_name = unquote(parts.path.lstrip("/"))
missing = [name for name in ("DB_USER", "DB_PASSWORD", "DB_NAME") if not values.get(name)]
if missing and (not db_user or not db_password or not db_name):
    raise SystemExit(
        "DATABASE_URL must include username, password, and database name "
        "to backfill missing DB_USER/DB_PASSWORD/DB_NAME"
    )
for key, value in (("DB_USER", db_user), ("DB_PASSWORD", db_password), ("DB_NAME", db_name)):
    if any(ord(ch) < 32 for ch in value) or "'" in value:
        raise SystemExit("{} derived from DATABASE_URL contains unsupported characters".format(key))

append = []
if "DB_USER" not in values:
    append.append("DB_USER={}".format(db_user))
if "DB_PASSWORD" not in values:
    append.append("DB_PASSWORD='{}'".format(db_password))
if "DB_NAME" not in values:
    append.append("DB_NAME={}".format(db_name))
if append:
    with path.open("a", encoding="utf-8") as f:
        f.write("\n# Backfilled for docker-compose variable interpolation.\n")
        for line in append:
            f.write(line + "\n")
PY
    log_warn "${file} 缺少 DB_USER/DB_PASSWORD/DB_NAME，已从 DATABASE_URL 补全供 docker compose 使用。"
}

available_kb_for_path() {
    local path="$1"
    python3 -c 'import shutil, sys; print(shutil.disk_usage(sys.argv[1]).free // 1024)' "${path}" 2>/dev/null
}

AUTO_INSTALL_DEPS="${LUMEN_AUTO_INSTALL_DEPS:-1}"
APT_UPDATED=0
DOCKER_USE_SUDO=0

auto_install_enabled() {
    case "${AUTO_INSTALL_DEPS}" in
        0|false|FALSE|False|no|NO|No|off|OFF|Off) return 1 ;;
        *) return 0 ;;
    esac
}

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

prepend_path_if_dir() {
    local dir="$1"
    if [ -d "${dir}" ]; then
        case ":${PATH}:" in
            *":${dir}:"*) ;;
            *) export PATH="${dir}:${PATH}" ;;
        esac
    fi
}

refresh_tool_paths() {
    prepend_path_if_dir "${HOME:-}/.local/bin"
    prepend_path_if_dir "${HOME:-}/.cargo/bin"
    prepend_path_if_dir "/opt/homebrew/bin"
    prepend_path_if_dir "/usr/local/bin"

    if have_cmd brew; then
        local prefix
        for formula in node@20 python@3.12 openssl@3 libpq; do
            prefix="$(brew --prefix "${formula}" 2>/dev/null || true)"
            if [ -n "${prefix}" ]; then
                prepend_path_if_dir "${prefix}/bin"
                prepend_path_if_dir "${prefix}/libexec/bin"
            fi
        done
    fi
}

run_as_root() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        "$@"
    elif have_cmd sudo; then
        sudo "$@"
    else
        return 1
    fi
}

require_auto_install() {
    local name="$1"
    local hint="$2"
    if auto_install_enabled; then
        return 0
    fi
    log_error "缺少或不满足依赖：${name}。已按 LUMEN_AUTO_INSTALL_DEPS=0 跳过自动安装。"
    printf '       建议安装方式：%s\n' "${hint}" >&2
    exit 1
}

linux_package_manager() {
    if have_cmd apt-get; then
        printf 'apt'
    elif have_cmd dnf; then
        printf 'dnf'
    elif have_cmd yum; then
        printf 'yum'
    elif have_cmd pacman; then
        printf 'pacman'
    elif have_cmd zypper; then
        printf 'zypper'
    elif have_cmd apk; then
        printf 'apk'
    else
        printf 'unknown'
    fi
}

apt_update_once() {
    if [ "${APT_UPDATED}" -eq 0 ]; then
        run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update
        APT_UPDATED=1
    fi
}

install_linux_packages() {
    local pm
    pm="$(linux_package_manager)"
    case "${pm}" in
        apt)
            apt_update_once
            run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
            ;;
        dnf)
            run_as_root dnf install -y "$@"
            ;;
        yum)
            run_as_root yum install -y "$@"
            ;;
        pacman)
            run_as_root pacman -Sy --noconfirm "$@"
            ;;
        zypper)
            run_as_root zypper --non-interactive install "$@"
            ;;
        apk)
            run_as_root apk add --no-cache "$@"
            ;;
        *)
            return 1
            ;;
    esac
}

ensure_linux_base_tools() {
    if [ "${OS}" != "linux" ]; then
        return 0
    fi
    case "$(linux_package_manager)" in
        apt) install_linux_packages ca-certificates curl gnupg ;;
        dnf|yum) install_linux_packages ca-certificates curl gnupg2 ;;
        pacman) install_linux_packages ca-certificates curl gnupg ;;
        zypper) install_linux_packages ca-certificates curl gpg2 ;;
        apk) install_linux_packages ca-certificates curl gnupg ;;
        *) return 1 ;;
    esac
}

ensure_homebrew() {
    if [ "${OS}" != "macos" ]; then
        return 1
    fi
    refresh_tool_paths
    if have_cmd brew; then
        return 0
    fi
    require_auto_install "Homebrew" "安装 Homebrew：https://brew.sh/"
    if ! have_cmd curl; then
        log_error "缺少 curl，无法自动安装 Homebrew。"
        exit 1
    fi
    log_info "缺少 Homebrew，尝试自动安装。"
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    refresh_tool_paths
    if ! have_cmd brew; then
        log_error "Homebrew 自动安装后仍不可用。请按 https://brew.sh/ 完成安装后重试。"
        exit 1
    fi
}

brew_install_formula() {
    local formula="$1"
    ensure_homebrew
    if ! brew list --formula "${formula}" >/dev/null 2>&1; then
        brew install "${formula}"
    fi
    refresh_tool_paths
}

brew_install_cask() {
    local cask="$1"
    ensure_homebrew
    if ! brew list --cask "${cask}" >/dev/null 2>&1; then
        brew install --cask "${cask}"
    fi
    refresh_tool_paths
}

install_linux_docker() {
    require_auto_install "Docker" "${DOCKER_HINT}"
    ensure_linux_base_tools
    local get_docker_script
    get_docker_script="$(mktemp)"
    log_info "缺少 Docker 或 docker compose v2，尝试通过 Docker 官方脚本安装。"
    curl -fsSL https://get.docker.com -o "${get_docker_script}"
    if ! run_as_root sh "${get_docker_script}"; then
        log_warn "Docker 官方安装脚本失败，尝试改用当前软件源中的 Docker 核心包安装。"
        install_linux_docker_packages
    fi
    rm -f "${get_docker_script}"
}

install_linux_docker_packages() {
    local pm packages package available_packages
    pm="$(linux_package_manager)"
    case "${pm}" in
        apt)
            run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update
            packages=(
                docker-ce
                docker-ce-cli
                containerd.io
                docker-compose-plugin
                docker-buildx-plugin
                docker-ce-rootless-extras
            )
            available_packages=()
            for package in "${packages[@]}"; do
                if apt-cache show "${package}" >/dev/null 2>&1; then
                    available_packages+=("${package}")
                else
                    log_warn "apt 源中不存在 ${package}，跳过该可选 Docker 包。"
                fi
            done
            if [ "${#available_packages[@]}" -eq 0 ]; then
                log_warn "Docker 官方源无可安装包，尝试安装发行版 docker.io/docker-compose-v2。"
                install_linux_packages docker.io docker-compose-v2 || install_linux_packages docker.io docker-compose
            else
                run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${available_packages[@]}"
            fi
            ;;
        dnf|yum)
            install_linux_packages docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin \
                || install_linux_packages docker docker-compose
            ;;
        pacman)
            install_linux_packages docker docker-compose
            ;;
        zypper)
            install_linux_packages docker docker-compose
            ;;
        apk)
            install_linux_packages docker docker-cli-compose
            ;;
        *)
            return 1
            ;;
    esac
}

install_linux_compose_plugin() {
    local pm
    pm="$(linux_package_manager)"
    case "${pm}" in
        apt) install_linux_packages docker-compose-plugin ;;
        dnf|yum) install_linux_packages docker-compose-plugin ;;
        pacman) install_linux_packages docker-compose ;;
        zypper) install_linux_packages docker-compose-plugin ;;
        apk) install_linux_packages docker-cli-compose ;;
        *) return 1 ;;
    esac
}

docker_cli() {
    if [ "${DOCKER_USE_SUDO}" = "1" ]; then
        sudo docker "$@"
    else
        docker "$@"
    fi
}

start_linux_docker_service() {
    if have_cmd systemctl; then
        run_as_root systemctl enable --now docker || run_as_root systemctl start docker || true
    elif have_cmd service; then
        run_as_root service docker start || true
    fi
}

wait_for_docker_daemon() {
    local i
    for i in $(seq 1 60); do
        if docker_cli info >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

ensure_docker_access_for_current_run() {
    DOCKER_USE_SUDO=0
    if docker info >/dev/null 2>&1; then
        return 0
    fi

    if [ "${OS}" = "linux" ]; then
        start_linux_docker_service
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        if [ "${EUID:-$(id -u)}" -ne 0 ] && have_cmd sudo; then
            DOCKER_USE_SUDO=1
            if wait_for_docker_daemon; then
                if ! id -nG | tr ' ' '\n' | grep -qx docker; then
                    log_warn "当前用户不在 docker 组，本次安装将自动用 sudo 执行 docker 命令。"
                    if getent group docker >/dev/null 2>&1; then
                        run_as_root usermod -aG docker "$(id -un)" || true
                        log_warn "已尝试把当前用户加入 docker 组；重新登录后可免 sudo 使用 docker。"
                    fi
                fi
                return 0
            fi
        fi
        DOCKER_USE_SUDO=0
    elif [ "${OS}" = "macos" ]; then
        if have_cmd open; then
            log_info "Docker daemon 未运行，尝试启动 Docker Desktop。"
            open -a Docker >/dev/null 2>&1 || true
            wait_for_docker_daemon && return 0
        fi
    fi
    return 1
}

ensure_docker_ready() {
    if ! have_cmd docker; then
        case "${OS}" in
            macos)
                require_auto_install "Docker Desktop" "${DOCKER_HINT}"
                log_info "缺少 Docker，尝试安装 Docker Desktop。"
                brew_install_cask docker
                ;;
            linux)
                install_linux_docker
                ;;
        esac
    fi
    if ! have_cmd docker; then
        log_error "Docker 自动安装后仍不可用。"
        printf '       安装提示：%s\n' "${DOCKER_HINT}" >&2
        exit 1
    fi

    if ! docker_cli compose version >/dev/null 2>&1; then
        case "${OS}" in
            macos)
                require_auto_install "docker compose v2" "${DOCKER_HINT}"
                brew_install_cask docker
                ;;
            linux)
                require_auto_install "docker compose v2" "${DOCKER_HINT}"
                install_linux_compose_plugin || install_linux_docker
                ;;
        esac
    fi
    if ! docker_cli compose version >/dev/null 2>&1; then
        log_error "未检测到 docker compose v2 子命令，自动安装后仍不可用。"
        printf '       安装提示：%s\n' "${DOCKER_HINT}" >&2
        exit 1
    fi

    if ! ensure_docker_access_for_current_run; then
        log_error "Docker daemon 未运行或当前用户无法访问 Docker。"
        if [ "${OS}" = "macos" ]; then
            printf '       请确认 Docker Desktop 已启动并完成首次初始化。\n' >&2
        else
            printf '       请确认 systemd/service 可启动 docker，或当前用户有 sudo 权限。\n' >&2
        fi
        exit 1
    fi
}

ensure_uv_ready() {
    refresh_tool_paths
    if ! have_cmd uv; then
        require_auto_install "uv" "${UV_HINT}"
        if [ "${OS}" = "linux" ]; then
            ensure_linux_base_tools
        elif [ "${OS}" = "macos" ] && ! have_cmd curl; then
            brew_install_formula curl
        fi
        log_info "缺少 uv，尝试通过官方安装脚本安装。"
        curl -LsSf https://astral.sh/uv/install.sh | sh
        refresh_tool_paths
    fi
    if ! have_cmd uv; then
        log_error "uv 自动安装后仍不可用。"
        printf '       安装提示：%s\n' "${UV_HINT}" >&2
        exit 1
    fi
    log_info "确保 uv 可用 Python 3.12。"
    uv python install 3.12 >/dev/null
}

install_linux_node20() {
    require_auto_install "Node.js >= 20 + npm" "${NODE_HINT}"
    ensure_linux_base_tools
    case "$(linux_package_manager)" in
        apt)
            run_as_root bash -c 'curl -fsSL https://deb.nodesource.com/setup_20.x | bash -'
            install_linux_packages nodejs
            ;;
        dnf|yum)
            run_as_root bash -c 'curl -fsSL https://rpm.nodesource.com/setup_20.x | bash -'
            install_linux_packages nodejs
            ;;
        pacman)
            install_linux_packages nodejs npm
            ;;
        zypper)
            install_linux_packages nodejs20 npm20 || install_linux_packages nodejs npm
            ;;
        apk)
            if ! install_linux_packages nodejs-current npm; then
                install_linux_packages nodejs npm
            fi
            ;;
        *)
            return 1
            ;;
    esac
}

node_major_version() {
    node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0
}

ensure_node_ready() {
    refresh_tool_paths
    local node_major
    node_major="$(node_major_version)"
    if ! have_cmd node || ! have_cmd npm || [ "${node_major}" -lt 20 ] 2>/dev/null; then
        case "${OS}" in
            macos)
                require_auto_install "Node.js >= 20 + npm" "${NODE_HINT}"
                log_info "缺少 Node.js >= 20/npm，尝试安装 node@20。"
                brew_install_formula node@20
                ;;
            linux)
                log_info "缺少 Node.js >= 20/npm，尝试自动安装 Node.js 20。"
                install_linux_node20
                ;;
        esac
        refresh_tool_paths
        node_major="$(node_major_version)"
    fi
    if ! have_cmd node || ! have_cmd npm || [ "${node_major}" -lt 20 ] 2>/dev/null; then
        log_error "Node.js/npm 自动安装后仍不满足要求：需要 Node.js >= 20。"
        printf '       当前 node：%s\n' "$(node -v 2>/dev/null || echo missing)" >&2
        printf '       当前 npm：%s\n' "$(npm -v 2>/dev/null || echo missing)" >&2
        printf '       安装提示：%s\n' "${NODE_HINT}" >&2
        exit 1
    fi
}

ensure_python_helper_ready() {
    refresh_tool_paths
    if ! have_cmd python3; then
        require_auto_install "python3" "${PYTHON_HINT}"
        case "${OS}" in
            macos)
                log_info "缺少 python3，尝试安装 python@3.12。"
                brew_install_formula python@3.12
                ;;
            linux)
                log_info "缺少 python3，尝试安装 Python 运行时。"
                case "$(linux_package_manager)" in
                    apt) install_linux_packages python3 python3-venv python3-pip ;;
                    dnf|yum) install_linux_packages python3 python3-pip ;;
                    pacman) install_linux_packages python python-pip ;;
                    zypper) install_linux_packages python3 python3-pip ;;
                    apk) install_linux_packages python3 py3-pip ;;
                    *) return 1 ;;
                esac
                ;;
        esac
        refresh_tool_paths
    fi
    if ! have_cmd python3; then
        log_error "python3 自动安装后仍不可用。"
        printf '       安装提示：%s\n' "${PYTHON_HINT}" >&2
        exit 1
    fi
}

ensure_openssl_ready() {
    refresh_tool_paths
    if ! have_cmd openssl; then
        require_auto_install "OpenSSL" "macOS: brew install openssl@3；Linux: 安装 openssl 包"
        case "${OS}" in
            macos)
                log_info "缺少 openssl，尝试安装 openssl@3。"
                brew_install_formula openssl@3
                ;;
            linux)
                log_info "缺少 openssl，尝试安装 openssl 包。"
                install_linux_packages openssl
                ;;
        esac
        refresh_tool_paths
    fi
    if ! have_cmd openssl; then
        log_error "openssl 自动安装后仍不可用。"
        exit 1
    fi
}

ensure_build_dependencies() {
    if have_cmd gcc && have_cmd make && have_cmd pg_config; then
        return 0
    fi
    require_auto_install "Python 编译依赖（gcc/make/pg_config）" \
        "Debian/Ubuntu: apt install build-essential libpq-dev pkg-config；macOS: brew install libpq"
    case "${OS}" in
        macos)
            brew_install_formula libpq
            ;;
        linux)
            case "$(linux_package_manager)" in
                apt) install_linux_packages build-essential libpq-dev pkg-config ;;
                dnf|yum) install_linux_packages gcc gcc-c++ make postgresql-devel pkgconf-pkg-config ;;
                pacman) install_linux_packages base-devel postgresql-libs pkgconf ;;
                zypper) install_linux_packages gcc gcc-c++ make postgresql-devel pkg-config ;;
                apk) install_linux_packages build-base postgresql-dev pkgconf ;;
                *) log_warn "无法识别包管理器，跳过编译依赖自动安装。" ;;
            esac
            ;;
    esac
    refresh_tool_paths
}

trap 'on_error ${LINENO}' ERR
trap cleanup EXIT
trap 'on_signal SIGINT 130' INT
trap 'on_signal SIGTERM 143' TERM

acquire_script_lock

log_step "Lumen 安装：检查环境（OS=${OS}）"

# ---------------------------------------------------------------------------
# 1. 依赖检查
# ---------------------------------------------------------------------------
case "${OS}" in
    macos)
        DOCKER_HINT="brew install --cask docker  （或 https://www.docker.com/products/docker-desktop）"
        UV_HINT="brew install uv  或  curl -LsSf https://astral.sh/uv/install.sh | sh"
        NODE_HINT="brew install node@20  并将其加入 PATH"
        PYTHON_HINT="brew install python@3.12"
        ;;
    linux)
        DOCKER_HINT="参考 https://docs.docker.com/engine/install/  （含 Docker Engine + Compose plugin）"
        UV_HINT="curl -LsSf https://astral.sh/uv/install.sh | sh"
        NODE_HINT="使用 nvm（curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash）后  nvm install 20"
        PYTHON_HINT="apt install python3.12 python3.12-venv  （或对应发行版的包管理器）"
        ;;
    *)
        log_error "暂不支持当前操作系统（uname -s = $(uname -s)）。仅支持 macOS 与 Linux（含 WSL2）。"
        exit 1
        ;;
esac

refresh_tool_paths
ensure_python_helper_ready
ensure_openssl_ready
ensure_node_ready
ensure_uv_ready
ensure_build_dependencies
ensure_docker_ready

log_info "依赖就绪：docker / docker compose / uv / node $(node -v) / python3 $(python3 -V 2>&1 | awk '{print $2}') / uv python 3.12"

# ---------------------------------------------------------------------------
# 2. 进入项目根
# ---------------------------------------------------------------------------
cd "${ROOT}"
log_info "项目根目录：${ROOT}"

# ---------------------------------------------------------------------------
# 2.5 环境就绪检查（磁盘 / 容器名 / 端口）
# ---------------------------------------------------------------------------
log_step "环境就绪检查"

# 磁盘空间（建议 ≥ 2 GB；docker 镜像 + node_modules + .venv 通常 1.5 GB+）
AVAILABLE_KB="$(available_kb_for_path "${ROOT}" || true)"
MIN_KB=$((2 * 1024 * 1024))
if [ -z "${AVAILABLE_KB}" ]; then
    log_warn "无法检测 ${ROOT} 所在磁盘空闲空间，继续执行。"
elif [ "${AVAILABLE_KB}" -lt "${MIN_KB}" ] 2>/dev/null; then
    log_warn "磁盘空闲约 $((AVAILABLE_KB / 1024)) MB，低于建议值 2 GB。"
    if ! confirm "仍要继续？"; then
        exit 0
    fi
fi

# 同名容器存在但不属于 docker compose -- 直接冲突，要求用户手清
for CNAME in lumen-pg lumen-redis; do
    if docker_cli ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${CNAME}"; then
        OWNER="$(docker_cli inspect "${CNAME}" --format '{{ index .Config.Labels "com.docker.compose.project" }}' 2>/dev/null || true)"
        if [ -z "${OWNER}" ]; then
            log_error "已存在同名容器 ${CNAME}，但不属于 docker compose 项目。"
            log_error "请手动清理后重跑：docker rm -f ${CNAME}"
            exit 1
        fi
    fi
done

# 宿主端口占用（仅检查 PG/Redis；如果端口被自家 compose 容器持有则放行）
for PORT in 5432 6379; do
    case "${PORT}" in
        5432) OWN_CNAME=lumen-pg ;;
        6379) OWN_CNAME=lumen-redis ;;
    esac
    if docker_cli ps --format '{{.Names}}' 2>/dev/null | grep -qx "${OWN_CNAME}"; then
        continue
    fi
    if port_in_use "${PORT}"; then
        log_error "端口 ${PORT} 已被宿主进程占用（常见原因：本地已有 PostgreSQL / Redis 在跑）。"
        if [ "${OS}" = "macos" ]; then
            log_error "排查命令：lsof -iTCP:${PORT} -sTCP:LISTEN -nP"
        else
            log_error "排查命令：ss -ltnp \"sport = :${PORT}\""
        fi
        exit 1
    fi
done

log_info "环境就绪检查通过。"

# ---------------------------------------------------------------------------
# 3. 写 .env（pydantic-settings 已配置从 ../../.env 加载，根一份即可）
# ---------------------------------------------------------------------------
ENV_FILE="${ROOT}/.env"
if [ -f "${ENV_FILE}" ]; then
    if ! grep -qE '^PROVIDERS=.+' "${ENV_FILE}"; then
        if grep -qE '^UPSTREAM_API_KEY=.+' "${ENV_FILE}"; then
            log_warn ".env 仍使用旧 UPSTREAM_* 配置；本版本会兼容读取，建议迁移为 PROVIDERS。"
        else
            log_error ".env 已存在，但 PROVIDERS 为空（可能是上次安装中断）。"
            log_error "请编辑 ${ENV_FILE} 填入后重跑，或删除 .env 让脚本重新生成。"
            exit 1
        fi
    fi
    ensure_compose_db_env_vars "${ENV_FILE}"
    log_info ".env 已存在，跳过生成。如需重置请手动删除后重跑。"
else
    log_step "首次配置 .env（按提示填写，括号内为默认值）"

    PROVIDER_API_KEY=""
    while [ -z "${PROVIDER_API_KEY}" ]; do
        PROVIDER_API_KEY="$(read_secret 'PROVIDER_API_KEY（必填，上游图像 API 密钥；输入不回显）')"
        if [ -z "${PROVIDER_API_KEY}" ]; then
            log_warn "PROVIDER_API_KEY 不能为空。"
        elif ! validate_dotenv_value "PROVIDER_API_KEY" "${PROVIDER_API_KEY}"; then
            PROVIDER_API_KEY=""
        fi
    done

    while :; do
        PROVIDER_BASE_URL="$(read_or_default 'PROVIDER_BASE_URL' 'https://api.example.com')"
        validate_dotenv_value "PROVIDER_BASE_URL" "${PROVIDER_BASE_URL}" && break
    done
    while :; do
        PUBLIC_BASE_URL="$(read_or_default 'PUBLIC_BASE_URL（站点对外访问地址）' 'http://localhost:3000')"
        validate_dotenv_value "PUBLIC_BASE_URL" "${PUBLIC_BASE_URL}" && break
    done
    while :; do
        CORS_ALLOW_ORIGINS="$(read_or_default 'CORS_ALLOW_ORIGINS（允许访问 API 的前端来源，逗号分隔）' 'http://localhost:3000')"
        validate_dotenv_value "CORS_ALLOW_ORIGINS" "${CORS_ALLOW_ORIGINS}" && break
    done

    DEFAULT_SECRET="$(openssl rand -hex 32)"
    while :; do
        SESSION_SECRET="$(read_or_default 'SESSION_SECRET（直接回车使用随机生成）' "${DEFAULT_SECRET}")"
        validate_dotenv_value "SESSION_SECRET" "${SESSION_SECRET}" && break
    done

    # Redis 密码：默认自动生成，避免安装过程被密码交互卡住。
    # 如需自定义，安装前设置 LUMEN_REDIS_PASSWORD。
    REDIS_PASSWORD="${LUMEN_REDIS_PASSWORD:-$(openssl rand -hex 24)}"
    validate_redis_password "${REDIS_PASSWORD}" || exit 1
    if [ -n "${LUMEN_REDIS_PASSWORD:-}" ]; then
        log_info "使用 LUMEN_REDIS_PASSWORD 写入 Redis 配置。"
    else
        log_info "已自动生成 Redis 密码。"
    fi

    log_info "写入 ${ENV_FILE}"
    DB_USER="lumen_app"
    DB_NAME="lumen_app"
    DB_PASSWORD="$(openssl rand -hex 24)"
    PROVIDER_BASE_URL_JSON="$(json_escape_string "${PROVIDER_BASE_URL}")"
    PROVIDER_API_KEY_JSON="$(json_escape_string "${PROVIDER_API_KEY}")"
    PROVIDERS_JSON="[{\"name\":\"default\",\"base_url\":\"${PROVIDER_BASE_URL_JSON}\",\"api_key\":\"${PROVIDER_API_KEY_JSON}\",\"priority\":0,\"weight\":1,\"enabled\":true}]"
    DB_PASSWORD_ENV="$(dotenv_quote "DB_PASSWORD" "${DB_PASSWORD}")"
    DATABASE_URL_ENV="$(dotenv_quote "DATABASE_URL" "postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}")"
    REDIS_PASSWORD_ENV="$(dotenv_quote "REDIS_PASSWORD" "${REDIS_PASSWORD}")"
    REDIS_URL_ENV="$(dotenv_quote "REDIS_URL" "redis://:${REDIS_PASSWORD}@localhost:6379/0")"
    PROVIDERS_ENV="$(dotenv_quote "PROVIDERS" "${PROVIDERS_JSON}")"
    SESSION_SECRET_ENV="$(dotenv_quote "SESSION_SECRET" "${SESSION_SECRET}")"
    PUBLIC_BASE_URL_ENV="$(dotenv_quote "PUBLIC_BASE_URL" "${PUBLIC_BASE_URL}")"
    CORS_ALLOW_ORIGINS_ENV="$(dotenv_quote "CORS_ALLOW_ORIGINS" "${CORS_ALLOW_ORIGINS}")"
    (
    umask 077
    cat > "${ENV_FILE}" <<EOF
# 由 scripts/install.sh 自动生成，可手动编辑。
# --- Database / Cache ---
DB_USER=${DB_USER}
DB_PASSWORD=${DB_PASSWORD_ENV}
DB_NAME=${DB_NAME}
DATABASE_URL=${DATABASE_URL_ENV}
REDIS_PASSWORD=${REDIS_PASSWORD_ENV}
REDIS_URL=${REDIS_URL_ENV}

# --- Provider Pool（上游配置唯一入口）---
PROVIDERS=${PROVIDERS_ENV}

# --- Session / Auth ---
SESSION_SECRET=${SESSION_SECRET_ENV}
SESSION_TTL_MIN=10080

# --- App ---
APP_ENV=dev
APP_PORT=8000
STORAGE_ROOT=/opt/lumendata/storage
PUBLIC_BASE_URL=${PUBLIC_BASE_URL_ENV}
CORS_ALLOW_ORIGINS=${CORS_ALLOW_ORIGINS_ENV}
EOF
    )
    # 兜底：即便 umask 077 在某些 shell 配置下失效（如别名/陷阱），也强制 600。
    chmod 600 "${ENV_FILE}"
    log_info ".env 已写入（权限 600）。"
fi

# 前端 .env.local（非敏感；即使 .env 已存在也按需补写，便于用户单独删除后恢复）
WEB_ENV="${ROOT}/apps/web/.env.local"
if [ ! -f "${WEB_ENV}" ]; then
    WEB_BACKEND_URL_ENV="$(dotenv_quote "LUMEN_BACKEND_URL" "http://127.0.0.1:8000")"
    cat > "${WEB_ENV}" <<EOF
# 前端运行时配置。
# 浏览器默认使用同源 /api，由 Next.js 服务端转发到 LUMEN_BACKEND_URL。
LUMEN_BACKEND_URL=${WEB_BACKEND_URL_ENV}
EOF
    log_info "已写入 ${WEB_ENV}"
fi

# ---------------------------------------------------------------------------
# 3.5 本地存储目录（API 上传 / 生成图落盘位置；STORAGE_ROOT 默认指向此处）
# ---------------------------------------------------------------------------
DATA_ROOT="/opt/lumendata"
if [ -e "${DATA_ROOT}" ] && [ ! -d "${DATA_ROOT}" ]; then
    log_error "${DATA_ROOT} 已存在但不是目录，请先移走或删除后重试。"
    exit 1
fi
if [ ! -d "${DATA_ROOT}" ]; then
    if [ ! -d /opt ]; then
        log_info "/opt 不存在，尝试自动创建。"
        run_as_root mkdir -p /opt || {
            log_error "无法创建 /opt。请确认当前用户有 sudo 权限后重试。"
            exit 1
        }
    fi
    log_info "创建本地存储目录：${DATA_ROOT}"
    if ! mkdir -p "${DATA_ROOT}" 2>/dev/null; then
        run_as_root mkdir -p "${DATA_ROOT}" || {
            log_error "无法创建 ${DATA_ROOT}。请确认当前用户有 sudo 权限后重试。"
            exit 1
        }
    fi
fi
if [ -d "${DATA_ROOT}" ] && [ ! -w "${DATA_ROOT}" ]; then
    log_info "修正 ${DATA_ROOT} 所有权为当前用户。"
    if ! run_as_root chown -R "$(id -un):$(id -gn)" "${DATA_ROOT}"; then
        log_error "当前用户无权写入 ${DATA_ROOT}，且自动 chown 失败。"
        log_error "请确认当前用户有 sudo 权限后重试。"
        exit 1
    fi
fi
mkdir -p "${DATA_ROOT}/storage" "${DATA_ROOT}/backup/pg" "${DATA_ROOT}/backup/redis" 2>/dev/null || {
    run_as_root mkdir -p "${DATA_ROOT}/storage" "${DATA_ROOT}/backup/pg" "${DATA_ROOT}/backup/redis"
    run_as_root chown -R "$(id -un):$(id -gn)" "${DATA_ROOT}"
}
if [ ! -w "${DATA_ROOT}/storage" ] || [ ! -w "${DATA_ROOT}/backup/pg" ] || [ ! -w "${DATA_ROOT}/backup/redis" ]; then
    log_error "本地存储目录创建后仍不可写：${DATA_ROOT}"
    exit 1
fi
log_info "存储目录就绪：${DATA_ROOT}/{storage,backup}"

# ---------------------------------------------------------------------------
# 4. 并行下载/同步依赖（docker 镜像 / Python / Node 三者无依赖，同时跑）
#    日志写入 .install-logs/，主进程只显示 [OK] / [FAIL] 摘要，避免输出交错。
# ---------------------------------------------------------------------------
log_step "并行下载/同步依赖（docker pull / uv sync / npm ci）"

LOG_DIR="${ROOT}/.install-logs"
mkdir -p "${LOG_DIR}"
PARALLEL_LOG_DIR="$(mktemp -d "${LOG_DIR}/run.XXXXXX")"
DOCKER_LOG="${PARALLEL_LOG_DIR}/docker-pull.log"
UV_LOG="${PARALLEL_LOG_DIR}/uv-sync.log"
NPM_LOG="${PARALLEL_LOG_DIR}/npm-ci.log"
DOCKER_RC="${PARALLEL_LOG_DIR}/docker-pull.rc"
UV_RC="${PARALLEL_LOG_DIR}/uv-sync.rc"
NPM_RC="${PARALLEL_LOG_DIR}/npm-ci.rc"

log_info "  docker compose pull   → ${DOCKER_LOG}"
log_info "  uv sync --frozen      → ${UV_LOG}"
log_info "  npm ci (apps/web)     → ${NPM_LOG}"
log_info "  三者并行执行；主进程会等到全部完成后再继续。"

(
    set +e
    docker_cli compose pull >"${DOCKER_LOG}" 2>&1
    echo $? > "${DOCKER_RC}"
) &
PARALLEL_PIDS+=("$!")

(
    set +e
    uv sync --frozen >"${UV_LOG}" 2>&1
    echo $? > "${UV_RC}"
) &
PARALLEL_PIDS+=("$!")

(
    set +e
    cd "${ROOT}/apps/web" && npm ci >"${NPM_LOG}" 2>&1
    echo $? > "${NPM_RC}"
) &
PARALLEL_PIDS+=("$!")

for pid in "${PARALLEL_PIDS[@]}"; do
    wait "${pid}" || true
done
PARALLEL_PIDS=()

PARALLEL_FAILED=0
report_parallel() {
    local name="$1"
    local rc_file="$2"
    local log_file="$3"
    local rc
    rc="$(cat "${rc_file}" 2>/dev/null || echo 99)"
    if [ "${rc}" = "0" ]; then
        log_info "  [OK]   ${name}"
    else
        log_error "  [FAIL] ${name} (exit ${rc}) — 详见 ${log_file}"
        if [ -s "${log_file}" ]; then
            log_error "  ${name} 最近日志："
            tail -n 20 "${log_file}" >&2 || true
        fi
        PARALLEL_FAILED=1
    fi
}
report_parallel "docker compose pull" "${DOCKER_RC}" "${DOCKER_LOG}"
report_parallel "uv sync --frozen"    "${UV_RC}"     "${UV_LOG}"
report_parallel "npm ci"              "${NPM_RC}"    "${NPM_LOG}"

if [ "${PARALLEL_FAILED}" -ne 0 ]; then
    log_error "并行阶段失败，请按上方 log 路径排查后重跑本脚本。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 5. 启动 PG / Redis 并等待健康（compose 自带 healthcheck，--wait 替代手写循环）
# ---------------------------------------------------------------------------
log_step "启动 PostgreSQL / Redis 并等待健康（docker compose up -d --wait）"
if ! docker_cli compose up -d --wait; then
    log_error "容器启动或健康检查失败。请运行 'docker compose logs' 排查。"
    log_error "提示：如果你的 docker compose 版本过旧不识别 --wait，请升级 Docker Desktop / docker-compose-plugin。"
    exit 1
fi
log_info "PG / Redis 已健康。"

# ---------------------------------------------------------------------------
# 6. alembic upgrade head
# ---------------------------------------------------------------------------
log_step "应用数据库迁移（alembic upgrade head）"
(
    cd "${ROOT}/apps/api"
    if ! uv run alembic upgrade head; then
        log_error "数据库迁移失败。请检查 PG 容器健康状态与 DATABASE_URL。"
        exit 1
    fi
)

# ---------------------------------------------------------------------------
# 7. 创建 / 提升 admin
# ---------------------------------------------------------------------------
log_step "创建首个管理员账号（bootstrap，幂等可重复）"
ADMIN_EMAIL="$(read_or_default '管理员邮箱' 'admin@example.com')"
ADMIN_PWD=""
while [ -z "${ADMIN_PWD}" ]; do
    ADMIN_PWD="$(read_secret '管理员密码（不少于 8 位）')"
    if [ -z "${ADMIN_PWD}" ]; then
        log_warn "密码不能为空。"
    elif [ "${#ADMIN_PWD}" -lt 8 ]; then
        log_warn "密码长度不能少于 8 位。"
        ADMIN_PWD=""
    fi
done

(
    cd "${ROOT}/apps/api"
    # bootstrap 自身可能在用户/密码已存在时报非零（脚本在 apps/api 内，跨边界无法改）；
    # 这里临时关闭 set -e 并把失败降级为 warn，避免重复运行 install.sh 时整体中断。
    set +e
    LUMEN_ADMIN_PASSWORD="${ADMIN_PWD}" uv run python - "${ADMIN_EMAIL}" <<'PY'
import asyncio
import os
import sys

from app.scripts import bootstrap

password = os.environ.pop("LUMEN_ADMIN_PASSWORD", "")
raise SystemExit(
    asyncio.run(
        bootstrap.main([sys.argv[1], "--role", "admin", "--password", password])
    )
)
PY
    BOOTSTRAP_RC=$?
    set -e
    if [ "${BOOTSTRAP_RC}" -ne 0 ]; then
        log_warn "bootstrap 返回非零（${BOOTSTRAP_RC}）。常见原因：管理员账号已存在；继续后续步骤。"
        log_warn "如需重置密码，登录后到管理面板修改，或手动 DELETE 后重跑本脚本。"
    fi
)

# ---------------------------------------------------------------------------
# 8. 可选 build（生产发布建议构建；本地开发可跳过）
# ---------------------------------------------------------------------------
if confirm "构建前端生产包（npm run build）？生产发布建议输入 y，本地开发可回车跳过"; then
    log_step "构建前端（npm run build）"
    (
        cd "${ROOT}/apps/web"
        # Next.js 只需要公开的 NEXT_PUBLIC_* 编译期变量，避免把 .env 密钥整体导出给构建进程。
        NEXT_PUBLIC_API_BASE="$(read_dotenv_value "NEXT_PUBLIC_API_BASE" "${WEB_ENV}")"
        if [ -n "${NEXT_PUBLIC_API_BASE}" ]; then
            export NEXT_PUBLIC_API_BASE
        else
            unset NEXT_PUBLIC_API_BASE
        fi
        npm run build
    )
    BUILD_DONE=1
else
    BUILD_DONE=0
fi

if [ "${BUILD_DONE}" -eq 1 ]; then
    WEB_START_CMD="cd ${ROOT}/apps/web && npm run start"
    WEB_MODE_LABEL="前端生产模式"
else
    WEB_START_CMD="cd ${ROOT}/apps/web && npm run dev"
    WEB_MODE_LABEL="前端开发模式"
fi

# ---------------------------------------------------------------------------
# 10. 总结
# ---------------------------------------------------------------------------
log_step "安装完成"
cat <<EOF

  访问地址 ......... http://<服务器IP>:3000  （${WEB_MODE_LABEL}；本机也可用 http://localhost:3000）
  API 服务 ......... http://127.0.0.1:8000  （由 Web 的 /api 转发）
  管理员邮箱 ....... ${ADMIN_EMAIL}

  启动 3 个进程（建议各开一个终端）：

    1) API（FastAPI）
       cd ${ROOT}/apps/api && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

    2) Worker（arq）
       cd ${ROOT}/apps/worker && uv run python -m arq app.main.WorkerSettings

    3) 前端
       ${WEB_START_CMD}
       （如需切换到生产模式：cd ${ROOT}/apps/web && npm run build && npm run start）

  日常运维：

    更新（拉新代码、依赖、迁移）  bash scripts/update.sh
    卸载（停容器、可选清数据）    bash scripts/uninstall.sh

  管理面板：登录后右上角 "管理"，可调整上游 API、像素预算、邀请链接。

EOF

trap - ERR
exit 0
