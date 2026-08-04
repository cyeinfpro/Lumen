#!/usr/bin/env bash
# System, Docker, port, dotenv, and Redis helpers for scripts/lib.sh.

confirm() {
    local prompt="$1"
    local reply=""
    printf '%s%s%s [y/N]: ' "${LUMEN_C_CYAN}" "${prompt}" "${LUMEN_C_RESET}"
    if ! IFS= read -r reply; then
        # EOF（curl|bash 远程模式 / 重定向 stdin / Ctrl-D）下视为 No，但显式
        # 提示，避免用户感觉"我啥也没按怎么就退出了"。
        printf '\n[INFO] (EOF / 非交互输入，视为 No)\n'
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

lumen_resolve_repo_root() {
    local script_dir="$1"
    local script_phys probe probe_parent
    script_phys="$(cd "${script_dir}" && pwd -P)"
    probe="$(cd "${script_phys}/.." && pwd -P)"
    probe_parent="$(cd "${probe}/.." && pwd -P)"
    if [ "$(basename "${probe_parent}")" = "releases" ]; then
        (cd "${probe_parent}/.." && pwd -P)
        return 0
    fi
    printf '%s' "${probe}"
}

lumen_canonical_deploy_root_path() {
    local raw_root="$1"
    local allow_missing="${2:-0}"
    if ! command -v python3 >/dev/null 2>&1; then
        log_error "解析部署根目录需要 python3。"
        return 1
    fi
    python3 - "${raw_root}" "${allow_missing}" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def fail(message: str) -> None:
    print(f"unsafe LUMEN_DEPLOY_ROOT: {message}", file=sys.stderr)
    raise SystemExit(1)


raw = sys.argv[1]
allow_missing = sys.argv[2] == "1"
if not raw or any(ord(char) < 32 for char in raw):
    fail("path is empty or contains control characters")
if not raw.startswith("/"):
    fail("path must be absolute")

trimmed = raw.rstrip("/") or "/"
segments = trimmed.split("/")[1:]
if any(segment in {"", ".", ".."} for segment in segments):
    fail("path contains empty, current-directory, or traversal components")
if trimmed in {
    "/",
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/home",
    "/lib",
    "/opt",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/srv",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
}:
    fail("path is a protected system directory")

root = Path(trimmed)
if root.parent.name == "releases":
    fail("path points at an individual release instead of the deployment root")

probe = Path("/")
missing_component = False
for segment in root.parts[1:]:
    probe /= segment
    if missing_component:
        continue
    try:
        metadata = probe.lstat()
    except FileNotFoundError:
        if not allow_missing:
            fail(f"path component does not exist: {probe}")
        missing_component = True
        continue
    if stat.S_ISLNK(metadata.st_mode):
        fail(f"path component is a symlink: {probe}")
    if not stat.S_ISDIR(metadata.st_mode):
        fail(f"path component is not a directory: {probe}")
if not missing_component and not root.is_dir():
    fail("path is not a directory")

releases = root / "releases"
if not missing_component and (releases.exists() or releases.is_symlink()):
    metadata = releases.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        fail("releases is not a regular directory")

for name in ("current", "previous"):
    if missing_component:
        break
    link = root / name
    try:
        metadata = link.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = link.resolve(strict=True)
            releases_resolved = releases.resolve(strict=True)
        except (FileNotFoundError, RuntimeError, OSError):
            fail(f"{name} is broken or cannot be resolved safely")
        if not target.is_dir() or target.parent != releases_resolved:
            fail(f"{name} escapes the deployment releases directory")
    elif not stat.S_ISDIR(metadata.st_mode):
        fail(f"{name} is neither a release symlink nor a directory")

print(root)
PY
}

lumen_prepare_deploy_root_for_lock() {
    local raw_root="$1"
    local candidate=""
    candidate="$(
        lumen_canonical_deploy_root_path "${raw_root}" 1
    )" || return 1
    if [ ! -d "${candidate}" ]; then
        if ! mkdir -p "${candidate}" 2>/dev/null; then
            if ! command -v lumen_run_as_root >/dev/null 2>&1 \
                    || ! lumen_run_as_root mkdir -p "${candidate}"; then
                log_error "无法创建部署根目录：${candidate}"
                return 1
            fi
        fi
    fi
    lumen_canonical_deploy_root_path "${candidate}"
}

lumen_resolve_install_deploy_root() {
    local script_dir="$1"
    local requested_root="${2:-}"
    local script_root=""
    script_root="$(lumen_resolve_repo_root "${script_dir}")" || return 1
    if [ -z "${requested_root}" ]; then
        case "${script_root}" in
            /opt/lumen|/opt/lumen/*)
                requested_root="/opt/lumen"
                ;;
            *)
                requested_root="${script_root}"
                ;;
        esac
    fi
    lumen_prepare_deploy_root_for_lock "${requested_root}"
}

lumen_resolve_deploy_root() {
    local script_dir="$1"
    local requested_root="${2:-}"
    local legacy_root="${3:-}"
    local inferred_root=""
    local canonical_root=""
    local canonical_legacy=""

    if [ -z "${requested_root}" ]; then
        requested_root="${legacy_root}"
    fi
    if [ -z "${requested_root}" ]; then
        inferred_root="$(lumen_resolve_repo_root "${script_dir}")" || return 1
        requested_root="${inferred_root}"
    fi
    canonical_root="$(
        lumen_canonical_deploy_root_path "${requested_root}"
    )" || return 1

    if [ -n "${legacy_root}" ]; then
        canonical_legacy="$(
            lumen_canonical_deploy_root_path "${legacy_root}"
        )" || return 1
        if [ "${canonical_legacy}" != "${canonical_root}" ]; then
            log_error "LUMEN_MAINT_ROOT/LUMEN_UPDATE_ROOT 与 LUMEN_DEPLOY_ROOT 指向不同部署目录。"
            return 1
        fi
    fi

    printf '%s' "${canonical_root}"
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
    # 健康检查走本地 loopback。admin 面板触发 update 时会注入 HTTP_PROXY=socks5h://...
    # 给 git/uv/npm 用，但本地 healthz 千万不能走代理——curl 会把 "connect 127.0.0.1" 投递
    # 到代理服务器，落到那台机器自己的 loopback，永远拿不到 lumen-api 的响应。
    curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
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

lumen_find_shared_env() {
    local script_root="${1:-}"
    local candidate
    for candidate in \
        "${LUMEN_ENV_FILE:-}" \
        "${script_root:+${script_root}/.env}" \
        "${script_root:+${script_root}/shared/.env}" \
        "${LUMEN_DEPLOY_ROOT:-/opt/lumen}/shared/.env"; do
        [ -n "${candidate}" ] || continue
        if [ -f "${candidate}" ] && [ ! -L "${candidate}" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

lumen_dotenv_export_if_unset() {
    local key="$1"
    local file="$2"
    local value=""
    if [ -n "${!key:-}" ]; then
        return 0
    fi
    if [ -L "${file}" ] || [ ! -f "${file}" ]; then
        return 0
    fi
    value="$(lumen_env_value "${key}" "${file}")"
    if [ -n "${value}" ]; then
        export "${key}=${value}"
    fi
}

lumen_redis_password_from_url() {
    local url="${1:-}"
    case "${url}" in
        redis://*|rediss://*) ;;
        *) return 1 ;;
    esac
    local rest="${url#*://}"
    case "${rest}" in
        *@*) ;;
        *) return 1 ;;
    esac
    local userpass="${rest%@*}"
    case "${userpass}" in
        *:*) printf '%s' "${userpass#*:}" ;;
        *)   printf '%s' "${userpass}" ;;
    esac
}

# 优先以 REDIS_URL 嵌入密码为准（与 docker-compose 中 api/worker 共用同一 URL，
# 即容器实际 requirepass）；fallback 到 .env 单独那一行 REDIS_PASSWORD。
# 调用前确保 REDIS_URL / REDIS_PASSWORD 已 export 到当前 shell。
lumen_redis_resolve_password() {
    local from_url=""
    if [ -n "${REDIS_URL:-}" ]; then
        from_url="$(lumen_redis_password_from_url "${REDIS_URL}" 2>/dev/null || true)"
    fi
    if [ -n "${from_url}" ]; then
        printf '%s' "${from_url}"
        return 0
    fi
    printf '%s' "${REDIS_PASSWORD:-}"
}

# Redis 协议错误（NOAUTH / WRONGPASS / ERR ...）会以正常输出形式返回 stdout
# 且 redis-cli 进程仍 exit 0；wrapper 必须主动识别避免后续把错误当数据处理。
lumen_redis_is_error_reply() {
    case "${1:-}" in
        "(error) "*|"NOAUTH "*|"WRONGPASS "*|"AUTH failed"*|"ERR "*|"ERROR "*|"NOPERM "*|"NOSCRIPT "*)
            return 0
            ;;
    esac
    return 1
}
