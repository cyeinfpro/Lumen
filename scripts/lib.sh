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

lumen_read_dotenv_value() {
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

lumen_ensure_compose_db_env_vars() {
    local file="$1"
    if [ ! -f "${file}" ]; then
        log_error "${file} 不存在，无法为 docker compose 读取 DB_USER/DB_PASSWORD/DB_NAME。"
        return 1
    fi
    if grep -qE '^DB_USER=.+' "${file}" \
        && grep -qE '^DB_PASSWORD=.+' "${file}" \
        && grep -qE '^DB_NAME=.+' "${file}"; then
        return 0
    fi
    if ! grep -qE '^DATABASE_URL=.+' "${file}"; then
        log_error "${file} 缺少 DB_USER/DB_PASSWORD/DB_NAME，且无法从 DATABASE_URL 推导。"
        log_error "请补充 DB_USER、DB_PASSWORD、DB_NAME 后重跑。"
        return 1
    fi
    if ! python3 - "${file}" <<'PY'
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
if not values.get("DB_USER"):
    append.append("DB_USER={}".format(db_user))
if not values.get("DB_PASSWORD"):
    append.append("DB_PASSWORD='{}'".format(db_password))
if not values.get("DB_NAME"):
    append.append("DB_NAME={}".format(db_name))
if append:
    with path.open("a", encoding="utf-8") as f:
        f.write("\n# Backfilled for docker-compose variable interpolation.\n")
        for line in append:
            f.write(line + "\n")
PY
    then
        return 1
    fi
    log_warn "${file} 缺少 DB_USER/DB_PASSWORD/DB_NAME，已从 DATABASE_URL 补全供 docker compose 使用。"
}

lumen_release_ensure_shared_env() {
    local root="$1"
    local shared_env="${root}/shared/.env"
    local root_env="${root}/.env"
    local current_env="${root}/current/.env"

    mkdir -p "${root}/shared" 2>/dev/null || true

    if [ -f "${shared_env}" ]; then
        return 0
    fi

    if [ -f "${root_env}" ] && [ ! -L "${root_env}" ]; then
        log_warn "shared/.env 缺失，检测到 ROOT/.env；自动移入 shared/.env 并保留软链。"
        if ! mv "${root_env}" "${shared_env}"; then
            log_error "无法把 ${root_env} 移入 ${shared_env}。"
            return 1
        fi
        ln -sfn "shared/.env" "${root_env}" 2>/dev/null || true
        return 0
    fi

    if [ -f "${current_env}" ]; then
        log_warn "shared/.env 缺失，检测到 current/.env；自动复制到 shared/.env。"
        if ! cp "${current_env}" "${shared_env}"; then
            log_error "无法把 ${current_env} 复制到 ${shared_env}。"
            return 1
        fi
        if [ ! -e "${root_env}" ] || [ -L "${root_env}" ]; then
            ln -sfn "shared/.env" "${root_env}" 2>/dev/null || true
        fi
        return 0
    fi

    log_error "shared/.env 缺失，且未找到可恢复的 ROOT/.env 或 current/.env。"
    log_error "请把生产 .env 放到 ${shared_env} 后重跑 update。"
    return 1
}

# ---------------------------------------------------------------------------
# Step protocol（结构化阶段协议）
# 由 admin_update.py 通过 .update.log 解析；格式必须严格保持。
# 三种行：
#   ::lumen-step:: phase=<name> status=start ts=<ISO8601>
#   ::lumen-step:: phase=<name> status=done  rc=<int> dur_ms=<int> ts=<ISO>
#   ::lumen-info:: phase=<name> key=<k> value=<v>
# ---------------------------------------------------------------------------

# 当前正在进行中的 phase（由 lumen_step_begin 设置，lumen_step_end 清除）。
# 可以被 trap/error handler 读取以输出 status=done rc=非零。
LUMEN_CURRENT_PHASE=""
# 该 phase 的起始时间（毫秒），用于计算 dur_ms。
LUMEN_CURRENT_PHASE_START_MS=""

# 所有合法的 phase 枚举（与 update.sh 严格对齐）。
# rollback 是异常分支，不计入正常流程，但允许在 begin/end 中使用。
LUMEN_VALID_PHASES="prepare fetch link_shared containers deps_python migrate_db deps_node build_web switch restart health_post cleanup rollback"

lumen_iso_now() {
    # GNU date / BSD date 都支持 -u +%FT%TZ
    date -u +%FT%TZ 2>/dev/null || date
}

# 当前时间，毫秒级（用于 dur_ms 计算）。
# 优先 GNU date %N；BSD date 没有 %N，则用 perl/python 兜底；最后退回到秒×1000。
lumen_now_ms() {
    local out
    out="$(date -u +%s%3N 2>/dev/null || true)"
    case "${out}" in
        ''|*[!0-9]*) ;;
        *N*) ;;
        *)
            # GNU date: 已经是毫秒数
            printf '%s' "${out}"
            return 0
            ;;
    esac
    if command -v perl >/dev/null 2>&1; then
        perl -MTime::HiRes=time -e 'printf "%d", time()*1000' 2>/dev/null && return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        python3 -c 'import time;print(int(time.time()*1000))' 2>/dev/null && return 0
    fi
    # 兜底：秒级精度。dur_ms 会偏差 0~999ms，但仍可读。
    printf '%s000' "$(date -u +%s 2>/dev/null || echo 0)"
}

lumen_step_phase_is_valid() {
    local phase="$1"
    case " ${LUMEN_VALID_PHASES} " in
        *" ${phase} "*) return 0 ;;
        *) return 1 ;;
    esac
}

# lumen_step_begin <phase>
# 输出 ::lumen-step:: phase=<name> status=start ts=<iso>
# 同时记录 LUMEN_CURRENT_PHASE / 起始时间，便于 lumen_step_end 计算 dur_ms。
lumen_step_begin() {
    local phase="$1"
    if [ -z "${phase}" ]; then
        log_warn "lumen_step_begin: 空 phase 参数。"
        return 0
    fi
    if ! lumen_step_phase_is_valid "${phase}"; then
        log_warn "lumen_step_begin: 未登记的 phase=${phase}（允许列表：${LUMEN_VALID_PHASES}）。"
    fi
    LUMEN_CURRENT_PHASE="${phase}"
    LUMEN_CURRENT_PHASE_START_MS="$(lumen_now_ms)"
    printf '::lumen-step:: phase=%s status=start ts=%s\n' \
        "${phase}" "$(lumen_iso_now)"
}

# lumen_step_end <phase> <rc>
# 输出 ::lumen-step:: phase=<name> status=done rc=<int> dur_ms=<int> ts=<iso>
# 在成功路径里手动调用；失败时由 trap 调用（rc 由 trap 计算）。
lumen_step_end() {
    local phase="$1"
    local rc="${2:-0}"
    local dur_ms=0
    if [ -z "${phase}" ]; then
        return 0
    fi
    if [ -n "${LUMEN_CURRENT_PHASE_START_MS:-}" ]; then
        local now_ms
        now_ms="$(lumen_now_ms)"
        # 纯算术：bash 内置即可；避开外部 expr 的字符串风险。
        dur_ms=$(( now_ms - LUMEN_CURRENT_PHASE_START_MS ))
        if [ "${dur_ms}" -lt 0 ]; then
            dur_ms=0
        fi
    fi
    printf '::lumen-step:: phase=%s status=done rc=%s dur_ms=%s ts=%s\n' \
        "${phase}" "${rc}" "${dur_ms}" "$(lumen_iso_now)"
    # 只在结束的是“当前”phase 时清空，避免乱序调用导致状态被错误清空。
    if [ "${LUMEN_CURRENT_PHASE:-}" = "${phase}" ]; then
        LUMEN_CURRENT_PHASE=""
        LUMEN_CURRENT_PHASE_START_MS=""
    fi
}

# lumen_step_info <phase> <key> <value...>
# 输出 ::lumen-info:: phase=<name> key=<k> value=<v>
# value 中的换行 / CR 会被替换为空格，避免破坏单行协议。
lumen_step_info() {
    local phase="$1"
    local key="$2"
    shift 2 || true
    local raw="$*"
    local value
    # 把 CR/LF 折叠成空格，防止输出多行污染协议。
    value="$(printf '%s' "${raw}" | tr '\r\n' '  ')"
    printf '::lumen-info:: phase=%s key=%s value=%s\n' \
        "${phase}" "${key}" "${value}"
}

# 在 ERR/EXIT trap 里调用：如果当前还有进行中的 phase，输出失败的 done 行。
# 防止协议解析方因为缺少 done 行而把整个 phase 误判为悬挂。
lumen_step_finalize_failure() {
    local rc="${1:-1}"
    if [ -n "${LUMEN_CURRENT_PHASE:-}" ]; then
        lumen_step_end "${LUMEN_CURRENT_PHASE}" "${rc}"
    fi
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
    if command -v lsof >/dev/null 2>&1; then
        lsof -tiTCP:"${port}" -sTCP:LISTEN -nP 2>/dev/null | sort -u
        return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        # ss -ltnpH 输出含 users:(("name",pid=NNN,fd=...)) 的字段
        ss -ltnpH 2>/dev/null \
            | awk -v port="${port}" '$4 ~ ":"port"$" {print $0}' \
            | grep -oE 'pid=[0-9]+' \
            | awk -F= '{print $2}' \
            | sort -u
        return 0
    fi
    if command -v netstat >/dev/null 2>&1; then
        netstat -ltnp 2>/dev/null \
            | awk -v port="${port}" '$4 ~ ":"port"$" {print $7}' \
            | awk -F'/' '{print $1}' \
            | grep -E '^[0-9]+$' \
            | sort -u
        return 0
    fi
    return 0
}

# 抓 PID 的命令行文本（用于识别是不是 lumen 自己起的进程）。
lumen_pid_cmdline() {
    local pid="$1"
    if [ -r "/proc/${pid}/cmdline" ]; then
        tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null
        return 0
    fi
    if command -v ps >/dev/null 2>&1; then
        # macOS ps 没有 /proc，用 -o command= 拿全命令行
        ps -o command= -p "${pid}" 2>/dev/null || ps -o args= -p "${pid}" 2>/dev/null || true
    fi
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
        lsof -p "${pid}" -d cwd -Fpn 2>/dev/null \
            | awk -v pid="${pid}" '
                /^p/ { current = substr($0, 2); next }
                /^n/ && current == pid { sub(/^n/, ""); print; exit }
            '
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
        if kill -0 "${pid}" 2>/dev/null && lumen_is_lumen_runtime_process "${pid}"; then
            kill "${pid}" 2>/dev/null || true
            sent=1
        fi
    done
    if [ "${sent}" -eq 1 ]; then
        local _i
        for _i in $(seq 1 "${wait_seconds}"); do
            local alive=0
            for pid in "${pids[@]}"; do
                if kill -0 "${pid}" 2>/dev/null && lumen_is_lumen_runtime_process "${pid}"; then
                    alive=1
                    break
                fi
            done
            [ "${alive}" -eq 0 ] && break
            sleep 1
        done
        for pid in "${pids[@]}"; do
            if kill -0 "${pid}" 2>/dev/null && lumen_is_lumen_runtime_process "${pid}"; then
                kill -9 "${pid}" 2>/dev/null || true
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
            kill "${pid}" 2>/dev/null || true
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
        if kill -0 "${pid}" 2>/dev/null && lumen_is_lumen_runtime_process "${pid}"; then
            log_warn "${label}：pid=${pid} 未在 SIGTERM 后退出，发送 SIGKILL。"
            kill -9 "${pid}" 2>/dev/null || true
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
            cd "${root}/apps/api"
            exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
        ) >"${api_log}" 2>&1 &
        api_pid="$!"
        LUMEN_LOCAL_RUNTIME_PIDS+=("${api_pid}")
    fi

    log_info "启动 Worker → ${worker_log}"
    (
        cd "${root}/apps/worker"
        exec uv run python -m arq app.main.WorkerSettings
    ) >"${worker_log}" 2>&1 &
    worker_pid="$!"
    LUMEN_LOCAL_RUNTIME_PIDS+=("${worker_pid}")

    if ! lumen_prepare_port_for_runtime 3000 "Web"; then
        failed=1
    else
        log_info "启动 Web → ${web_log}"
        (
            cd "${root}/apps/web"
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

# ---------------------------------------------------------------------------
# Release / shared 目录工具
# 用于 Capistrano 风格的 release 切换：
#   ${ROOT}/current      -> releases/<active>
#   ${ROOT}/previous     -> releases/<previous>
#   ${ROOT}/releases/<id>/   全量代码 + .venv + node_modules + .next
#   ${ROOT}/shared/web-env/.env.local
#   ${ROOT}/shared/worker-var/
#   ${ROOT}/shared/web-next-cache/
# ---------------------------------------------------------------------------

# release id：UTC 时间 + sha7。按字典序排序即时间序，便于 cleanup 保留最近 N 个。
lumen_release_id() {
    local sha="${1:-unknown}"
    # 截断到 7 位：与 git rev-parse --short 保持一致；不足 7 位时直接补字面量。
    local short
    short="$(printf '%s' "${sha}" | cut -c1-7)"
    [ -n "${short}" ] || short="unknown"
    printf '%sZ-%s' "$(date -u +%Y%m%dT%H%M%S)" "${short}"
}

# 读取 ${ROOT}/current 当前指向的 release 目录的绝对路径。
# 不是 symlink 时返回空串。
lumen_release_current_path() {
    local root="$1"
    local cur="${root}/current"
    [ -L "${cur}" ] || return 0
    if command -v readlink >/dev/null 2>&1; then
        # readlink -f 在 BSD 也可用（macOS 12+ 的 coreutils）；不行就回退到自己拼。
        local target
        target="$(readlink -f "${cur}" 2>/dev/null || true)"
        if [ -n "${target}" ]; then
            printf '%s' "${target}"
            return 0
        fi
        target="$(readlink "${cur}" 2>/dev/null || true)"
        case "${target}" in
            /*) printf '%s' "${target}" ;;
            '') ;;
            *) printf '%s/%s' "${root}" "${target}" ;;
        esac
    fi
}

# 读取 ${ROOT}/current 指向 release 的 id（即目录名），不是 symlink 返回空串。
lumen_release_current_id() {
    local root="$1"
    local target
    target="$(lumen_release_current_path "${root}" || true)"
    [ -n "${target}" ] || return 0
    basename "${target}"
}

# 检测 GNU mv 是否支持 -T 选项（用于真正原子的 symlink 替换）。
# 0 = 支持；1 = 不支持（macOS / BSD 默认）。
lumen_mv_has_T() {
    # `mv --version` GNU 才支持；BSD mv 会报 illegal option。
    mv --version >/dev/null 2>&1 || return 1
    # GNU mv 全部支持 -T（since coreutils 6.x）。
    return 0
}

# lumen_atomic_replace_symlink <link_target> <link_path>
# 跨平台原子替换 symlink。优先级：
#   1. GNU `mv -T`（rename(2) syscall，POSIX 保证原子）
#   2. python3 os.replace（也是 rename(2) 一次完成，BSD/macOS 上严格原子）
#   3. `ln -sfn`（unlink+symlink 两步，存在 µs 级窗口；最后兜底）
# link_target 是软链内容（通常相对路径如 "releases/<id>"）；link_path 是绝对路径。
lumen_atomic_replace_symlink() {
    local link_target="$1"
    local link_path="$2"
    local link_dir
    link_dir="$(dirname "${link_path}")"
    local link_name
    link_name="$(basename "${link_path}")"
    local tmp="${link_dir}/.${link_name}.tmp.$$"

    rm -f "${tmp}" 2>/dev/null || true
    if ! ln -s "${link_target}" "${tmp}"; then
        return 1
    fi

    if lumen_mv_has_T; then
        if mv -T "${tmp}" "${link_path}"; then
            return 0
        fi
        rm -f "${tmp}" 2>/dev/null || true
        return 1
    fi

    if command -v python3 >/dev/null 2>&1; then
        # os.replace 直接 rename(2)，跨平台原子；目标是 symlink 本身（不解引用）。
        if python3 -c "import os, sys; os.replace(sys.argv[1], sys.argv[2])" "${tmp}" "${link_path}" 2>/dev/null; then
            return 0
        fi
    fi

    # 最后兜底：ln -sfn。窗口极短，仅作 fallback。
    rm -f "${tmp}" 2>/dev/null || true
    ln -sfn "${link_target}" "${link_path}" 2>/dev/null || return 1
    return 0
}

# lumen_release_atomic_switch <root> <new_id>
# 原子地把 ${root}/current 切到 releases/<new_id>，并把旧 release 写入 ${root}/previous。
# 注意：current/previous 都是相对软链（指 "releases/<id>"），便于整体迁移到不同前缀。
lumen_release_atomic_switch() {
    local root="$1"
    local new_id="$2"
    local old_id=""
    old_id="$(lumen_release_current_id "${root}" || true)"

    if [ -z "${new_id}" ]; then
        log_error "lumen_release_atomic_switch：new_id 为空。"
        return 1
    fi
    if [ ! -d "${root}/releases/${new_id}" ]; then
        log_error "lumen_release_atomic_switch：不存在 releases/${new_id}。"
        return 1
    fi

    if ! lumen_atomic_replace_symlink "releases/${new_id}" "${root}/current"; then
        log_error "切换 ${root}/current → releases/${new_id} 失败。"
        return 1
    fi

    # 更新 previous 软链（指向旧 release）。失败不致命。
    if [ -n "${old_id}" ] && [ "${old_id}" != "${new_id}" ] \
        && [ -d "${root}/releases/${old_id}" ]; then
        lumen_atomic_replace_symlink "releases/${old_id}" "${root}/previous" 2>/dev/null || true
    fi
    return 0
}

# lumen_release_link_shared <release_dir> <shared_dir>
# 把 shared 目录下的几条已知路径软链到 release 内对应位置。
# 调用前 release 内的同名文件/目录如果存在会被备份到 .pre-link 后再删除（避免 ln 报错）。
lumen_release_link_shared() {
    local release_dir="$1"
    local shared_dir="$2"
    if [ ! -d "${release_dir}" ]; then
        log_error "lumen_release_link_shared：release 目录不存在：${release_dir}"
        return 1
    fi
    if [ ! -d "${shared_dir}" ]; then
        log_error "lumen_release_link_shared：shared 目录不存在：${shared_dir}"
        return 1
    fi

    # 四条软链。第二个字段为 shared 下的物理路径，第三个字段为 release 内目标路径。
    # 用换行分隔，避开复杂关联数组（兼容 bash 3.2 / macOS）。
    # .env 是 docker compose 启动 PostgreSQL / Redis 时读取的，release 是
    # git clone 出来的纯净树没有 .env，必须从 shared 链入；否则 containers
    # phase 会因为 "required variable DB_USER is missing a value" 失败。
    local mapping="
web-env/.env.local|apps/web/.env.local
worker-var|apps/worker/var
web-next-cache|apps/web/.next/cache
.env|.env
"
    local line src_rel dst_rel src dst dst_parent
    while IFS= read -r line; do
        [ -n "${line}" ] || continue
        src_rel="${line%%|*}"
        dst_rel="${line#*|}"
        src="${shared_dir}/${src_rel}"
        dst="${release_dir}/${dst_rel}"
        dst_parent="$(dirname "${dst}")"

        # shared 下的源不存在则跳过（例如 .env.local 在某些环境可能没有）。
        if [ ! -e "${src}" ] && [ ! -L "${src}" ]; then
            log_warn "shared 中缺少 ${src_rel}，跳过软链 ${dst_rel}。"
            continue
        fi

        mkdir -p "${dst_parent}" 2>/dev/null || true

        # 若 release 内已经有同名实体，先移走（不删除，备份成 .pre-link.<ts>）。
        if [ -e "${dst}" ] || [ -L "${dst}" ]; then
            local backup="${dst}.pre-link.$(date -u +%Y%m%d%H%M%S)"
            if ! mv "${dst}" "${backup}" 2>/dev/null; then
                rm -rf "${dst}" 2>/dev/null || true
            fi
        fi

        if ! ln -s "${src}" "${dst}"; then
            log_error "无法软链 ${dst} -> ${src}"
            return 1
        fi
    done <<EOF
${mapping}
EOF
    return 0
}

# lumen_release_cleanup_old <root> <keep>
# 保留按字典序最新的 <keep> 个 release，其余删除。
# 任何被 current/previous 指向的 release 都不会被删，即使它落在保留窗口外。
lumen_release_cleanup_old() {
    local root="$1"
    local keep="${2:-5}"
    local releases_dir="${root}/releases"
    [ -d "${releases_dir}" ] || return 0

    # 取出 current/previous 指向的 release id（仅 basename，避免跨平台
    # readlink/canonical 路径不一致——macOS /tmp -> /private/tmp 等情况）。
    local current_id previous_id
    current_id="$(lumen_release_current_id "${root}" || true)"
    previous_id=""
    if [ -L "${root}/previous" ]; then
        local prev_link
        prev_link="$(readlink "${root}/previous" 2>/dev/null || true)"
        if [ -n "${prev_link}" ]; then
            previous_id="$(basename "${prev_link}")"
        fi
    fi

    # 列出所有 release 子目录，按字典序倒排（最新的在前）。
    local -a all_ids=()
    local entry
    for entry in "${releases_dir}"/*; do
        [ -d "${entry}" ] || continue
        all_ids+=("$(basename "${entry}")")
    done
    if [ "${#all_ids[@]}" -le "${keep}" ]; then
        return 0
    fi

    # bash 3.2 没有 mapfile，用排序+逐行读。
    local -a sorted=()
    local id
    while IFS= read -r id; do
        sorted+=("${id}")
    done < <(printf '%s\n' "${all_ids[@]}" | sort -r)

    local kept=0
    local target removed=0
    for id in "${sorted[@]}"; do
        target="${releases_dir}/${id}"
        # 当前 current 或 previous 指向的，无条件保留。
        if [ -n "${current_id}" ] && [ "${id}" = "${current_id}" ]; then
            kept=$((kept+1))
            continue
        fi
        if [ -n "${previous_id}" ] && [ "${id}" = "${previous_id}" ]; then
            kept=$((kept+1))
            continue
        fi
        if [ "${kept}" -lt "${keep}" ]; then
            kept=$((kept+1))
        else
            # 删除。Linux 上 rm -rf 数百 MB 通常 < 1s。
            if rm -rf "${target}" 2>/dev/null; then
                removed=$((removed+1))
            fi
        fi
    done
    if [ "${removed}" -gt 0 ]; then
        log_info "release cleanup：删除 ${removed} 个旧 release，保留 ${keep} 个。"
    fi
    return 0
}
