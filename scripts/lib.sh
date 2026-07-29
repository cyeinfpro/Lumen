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

lumen_env_truthy() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

lumen_require_python_min_version() {
    local python_bin="${1:-python3}"
    local min_major="${2:-3}"
    local min_minor="${3:-8}"
    if ! command -v "${python_bin}" >/dev/null 2>&1; then
        log_error "未找到 Python：${python_bin}（需要 >= ${min_major}.${min_minor}）。"
        return 1
    fi
    if ! "${python_bin}" - "${min_major}" "${min_minor}" <<'PY' >/dev/null 2>&1
import sys

major = int(sys.argv[1])
minor = int(sys.argv[2])
raise SystemExit(0 if sys.version_info >= (major, minor) else 1)
PY
    then
        log_error "${python_bin} 版本过低：$("${python_bin}" --version 2>&1)，需要 >= ${min_major}.${min_minor}。"
        return 1
    fi
    return 0
}

# 默认运维路径与 Compose project name（§11.4 死规则：project name 必须固定）。
# 调用方可通过环境变量覆盖；fallback 全部走 /opt/lumendata 与 /opt/lumen 约定。
# LUMEN_DB_ROOT 只承载 postgres / redis，便于把数据库放在本机盘，
# 同时让 storage / backup 继续使用 LUMEN_DATA_ROOT（例如 CIFS/NAS）。
: "${LUMEN_DATA_ROOT:=/opt/lumendata}"
: "${LUMEN_DB_ROOT:=$LUMEN_DATA_ROOT}"
: "${LUMEN_BACKUP_ROOT:=$LUMEN_DATA_ROOT/backup}"
: "${LUMEN_POSTGRES_UID:=999}"
: "${LUMEN_POSTGRES_GID:=999}"
: "${LUMEN_REDIS_UID:=999}"
: "${LUMEN_REDIS_GID:=999}"
: "${LUMEN_APP_UID:=10001}"
: "${LUMEN_APP_GID:=10001}"
: "${LUMEN_APP_STORAGE_GID:=$LUMEN_APP_GID}"
: "${LUMEN_DEPLOY_ROOT:=/opt/lumen}"
: "${LUMEN_COMPOSE_PROJECT:=lumen}"
export LUMEN_DATA_ROOT LUMEN_DB_ROOT LUMEN_BACKUP_ROOT LUMEN_POSTGRES_UID LUMEN_POSTGRES_GID LUMEN_REDIS_UID LUMEN_REDIS_GID LUMEN_APP_UID LUMEN_APP_GID LUMEN_APP_STORAGE_GID LUMEN_DEPLOY_ROOT LUMEN_COMPOSE_PROJECT

# Step protocol and signal helpers are loaded from lib/step_protocol.sh.
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
        "/opt/lumen/shared/.env"; do
        [ -n "${candidate}" ] || continue
        if [ -f "${candidate}" ]; then
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
    if [ ! -f "${file}" ]; then
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

# Strict updater: only a release tag verified by its manifest or a 40-byte
# commit may reach raw GitHub. Mutable branches must use the bootstrap wrapper.
lumen_github_repo_slug() {
    local repo_url="${1:-${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}}" owner_repo=""
    case "${repo_url}" in
        https://github.com/*) owner_repo="${repo_url#https://github.com/}" ;;
        *) return 1 ;;
    esac
    owner_repo="${owner_repo%.git}"
    [[ "${owner_repo}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || return 1
    printf '%s' "${owner_repo}"
}

lumen_validate_self_update_file() {
    local relative="$1"
    local path="$2"
    local first_line=""
    if [ ! -f "${path}" ] || [ -L "${path}" ]; then
        return 1
    fi
    IFS= read -r first_line < "${path}" || true
    case "${relative}" in
        *.sh)
            case "${first_line}" in
                '#!'*bash*) ;;
                *) return 1 ;;
            esac
            bash -n "${path}" >/dev/null 2>&1
            ;;
        *.py)
            case "${first_line}" in
                '#!'*python3*) ;;
                *) return 1 ;;
            esac
            command -v python3 >/dev/null 2>&1 || return 1
            python3 - "${path}" <<'PY' >/dev/null 2>&1
from pathlib import Path
import sys

path = Path(sys.argv[1])
source = path.read_text(encoding="utf-8")
compile(source, str(path), "exec")
PY
            ;;
        *)
            return 1
            ;;
    esac
}

lumen_self_update_file_mode() {
    case "${1:-}" in
        backup.sh|restore.sh|update.sh|install.sh|uninstall.sh|lumenctl.sh|migrate_to_releases.sh)
            printf '0755'
            ;;
        *)
            printf '0644'
            ;;
    esac
}

# Install every changed script plus the update markers as one rollback unit.
# The subshell owns signal traps, so caller traps are left untouched.
lumen_self_update_install_transaction() (
    scripts_dir="$1"
    download_dir="$2"
    backup_ts="$3"
    commit_sha="$4"
    all_files_list="$5"
    changed_files_list="$6"
    transaction_dir=""
    committed=0
    replacement_started=0
    rollback_failed=0
    f=""
    target=""
    target_dir=""
    backup=""
    stage=""
    mode=""
    index=0
    marker=""
    marker_stage=""
    marker_backup=""
    changed_files=()
    targets=()
    target_states=()
    target_backups=()
    target_stages=()
    marker_paths=()
    marker_states=()
    marker_backups=()
    marker_stages=()
    visible_backups=()

    while IFS= read -r f; do
        [ -n "${f}" ] && changed_files+=("${f}")
    done < "${changed_files_list}"

    umask 077
    transaction_dir="$(
        mktemp -d "${scripts_dir}/.lumen-self-update.txn.XXXXXXXXXX" 2>/dev/null
    )" || exit 1
    if ! chmod 0700 "${transaction_dir}"; then
        rm -rf "${transaction_dir}" 2>/dev/null || true
        exit 1
    fi
    export LUMEN_SELF_UPDATE_TRANSACTION_PID="${BASHPID:-$$}"

    # shellcheck disable=SC2329  # Invoked from rollback and EXIT traps.
    _lumen_self_update_restore_path() {
        local restore_path="$1"
        local restore_state="$2"
        local restore_backup="$3"
        local restore_index="$4"
        local restore_stage="${transaction_dir}/restore.${restore_index}"
        case "${restore_state}" in
            present)
                if ! cp -a "${restore_backup}" "${restore_stage}" \
                        || ! mv -f "${restore_stage}" "${restore_path}"; then
                    rm -f "${restore_stage}" 2>/dev/null || true
                    return 1
                fi
                ;;
            absent)
                rm -f "${restore_path}" || return 1
                ;;
            *)
                return 1
                ;;
        esac
    }

    # shellcheck disable=SC2329  # Invoked from the EXIT trap.
    _lumen_self_update_rollback() {
        local i
        rollback_failed=0
        for ((i = ${#marker_paths[@]} - 1; i >= 0; i--)); do
            if ! _lumen_self_update_restore_path \
                    "${marker_paths[$i]}" \
                    "${marker_states[$i]}" \
                    "${marker_backups[$i]}" \
                    "marker.${i}"; then
                rollback_failed=1
                log_warn "[self_update] marker 回滚失败：${marker_paths[$i]}"
            fi
        done
        for ((i = ${#targets[@]} - 1; i >= 0; i--)); do
            if ! _lumen_self_update_restore_path \
                    "${targets[$i]}" \
                    "${target_states[$i]}" \
                    "${target_backups[$i]}" \
                    "target.${i}"; then
                rollback_failed=1
                log_warn "[self_update] 脚本回滚失败：${targets[$i]}"
            fi
        done
        return "${rollback_failed}"
    }

    # shellcheck disable=SC2329  # Installed as the EXIT trap below.
    _lumen_self_update_finish() {
        local rc=$?
        local created_backup
        trap - EXIT INT TERM HUP
        if [ "${committed}" -ne 1 ]; then
            if [ "${replacement_started}" -eq 1 ]; then
                log_warn "[self_update] 安装事务失败，正在恢复全部 scripts 文件。"
                if ! _lumen_self_update_rollback; then
                    log_warn "[self_update] scripts 事务回滚不完整，拒绝继续运行。"
                    rc=70
                fi
            else
                for created_backup in \
                        ${visible_backups[@]+"${visible_backups[@]}"}; do
                    rm -f "${created_backup}" 2>/dev/null || true
                done
            fi
        fi
        rm -rf "${transaction_dir}" 2>/dev/null || true
        exit "${rc}"
    }

    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap '_lumen_self_update_finish' EXIT

    mkdir -p "${transaction_dir}/staged" "${transaction_dir}/markers" \
        || exit 1

    for f in ${changed_files[@]+"${changed_files[@]}"}; do
        target="${scripts_dir}/${f}"
        target_dir="$(dirname "${target}")"
        if ! mkdir -p "${target_dir}"; then
            log_warn "[self_update] 无法创建目标目录：${target_dir}。"
            exit 1
        fi
        if [ -L "${target}" ] || { [ -e "${target}" ] && [ ! -f "${target}" ]; }; then
            log_warn "[self_update] 目标不是普通文件：${target}。"
            exit 1
        fi

        index=$((index + 1))
        backup=""
        if [ -f "${target}" ]; then
            backup="${target}.bak.${backup_ts}"
            if [ -e "${backup}" ] || [ -L "${backup}" ]; then
                log_warn "[self_update] 备份路径已存在，拒绝覆盖：${backup}。"
                exit 1
            fi
            if ! cp -a "${target}" "${backup}" \
                    || ! cmp -s "${target}" "${backup}"; then
                log_warn "[self_update] 备份失败，未开始替换：${target}。"
                exit 1
            fi
            target_states+=("present")
            target_backups+=("${backup}")
            visible_backups+=("${backup}")
        else
            target_states+=("absent")
            target_backups+=("")
        fi

        stage="${transaction_dir}/staged/$(printf '%04d' "${index}")"
        mode="$(lumen_self_update_file_mode "${f}")"
        if ! cp -a "${download_dir}/${f}" "${stage}" \
                || ! chmod "${mode}" "${stage}" \
                || ! lumen_validate_self_update_file "${f}" "${stage}"; then
            log_warn "[self_update] staging/权限校验失败：${f}。"
            exit 1
        fi
        targets+=("${target}")
        target_stages+=("${stage}")
    done

    marker_paths=(
        "${scripts_dir}/.lumen-self-update.files"
        "${scripts_dir}/.lumen-self-update.source"
        "${scripts_dir}/.lumen-self-update.last"
    )
    index=0
    for marker in "${marker_paths[@]}"; do
        index=$((index + 1))
        if [ -L "${marker}" ] || { [ -e "${marker}" ] && [ ! -f "${marker}" ]; }; then
            log_warn "[self_update] marker 不是普通文件：${marker}。"
            exit 1
        fi
        marker_backup="${transaction_dir}/markers/original.${index}"
        if [ -f "${marker}" ]; then
            if ! cp -a "${marker}" "${marker_backup}"; then
                log_warn "[self_update] marker 备份失败：${marker}。"
                exit 1
            fi
            marker_states+=("present")
            marker_backups+=("${marker_backup}")
        else
            marker_states+=("absent")
            marker_backups+=("")
        fi
        marker_stage="${transaction_dir}/markers/staged.${index}"
        case "${index}" in
            1) sort -u "${all_files_list}" > "${marker_stage}" || exit 1 ;;
            2) printf '%s\n' "${commit_sha}" > "${marker_stage}" || exit 1 ;;
            3) date -u +%s > "${marker_stage}" || exit 1 ;;
        esac
        if ! chmod 0600 "${marker_stage}"; then
            log_warn "[self_update] marker 权限设置失败：${marker}。"
            exit 1
        fi
        marker_stages+=("${marker_stage}")
    done

    replacement_started=1
    for ((index = 0; index < ${#targets[@]}; index++)); do
        if ! mv -f "${target_stages[$index]}" "${targets[$index]}"; then
            log_warn "[self_update] 替换失败：${targets[$index]}。"
            exit 1
        fi
    done
    for ((index = 0; index < ${#marker_paths[@]}; index++)); do
        if ! mv -f "${marker_stages[$index]}" "${marker_paths[$index]}"; then
            log_warn "[self_update] marker 提交失败：${marker_paths[$index]}。"
            exit 1
        fi
    done

    committed=1
    exit 0
)

lumen_self_update_scripts() {
    LUMEN_SELF_UPDATE_RESULT=skipped
    LUMEN_SELF_UPDATE_CHANGED=""
    LUMEN_SELF_UPDATE_BACKUP_TS=""
    LUMEN_SELF_UPDATE_SOURCE=""
    LUMEN_SELF_UPDATE_SOURCE_TAG=""
    LUMEN_SELF_UPDATE_SOURCE_COMMIT=""

    local scripts_dir="${1:-}"
    local source_ref="${2:-${LUMEN_SELF_UPDATE_REF:-}}"
    local ttl_sec="${3:-${LUMEN_SELF_UPDATE_TTL:-600}}"
    if [ "$#" -gt 3 ]; then
        shift 3
    else
        shift "$#"
    fi
    local files=("$@")
    local module_files=(
        lib/environment.sh
        lib/step_protocol.sh
        lib/runtime.sh
        lib/locking.sh
        lib/container_release.sh
        lib/release_layout.sh
    )
    local python_helper_files=(release_manifest_guard.py update_runner.py restore_runner.py)
    if [ "${#files[@]}" -eq 0 ]; then
        files=(
            lib.sh
            lib/environment.sh
            lib/step_protocol.sh
            lib/runtime.sh
            lib/locking.sh
            lib/container_release.sh
            lib/release_layout.sh
            release_manifest_guard.py
            update_runner.py
            restore_runner.py
            backup.sh
            restore.sh
            update.sh
            lumenctl.sh
        )
    else
        # Facade/modules/runners are one version unit, including legacy callers.
        local requested include_modules=0 include_python_helpers=0 module helper present
        for requested in "${files[@]}"; do
            if [ "${requested}" = "lib.sh" ]; then
                include_modules=1
            fi
            case "${requested}" in
                lib.sh|update.sh|lumenctl.sh)
                    include_python_helpers=1
                    ;;
            esac
        done
        if [ "${include_modules}" -eq 1 ]; then
            for module in "${module_files[@]}"; do
                present=0
                for requested in "${files[@]}"; do
                    if [ "${requested}" = "${module}" ]; then
                        present=1
                        break
                    fi
                done
                if [ "${present}" -eq 0 ]; then
                    files+=("${module}")
                fi
            done
        fi
        if [ "${include_python_helpers}" -eq 1 ]; then
            for helper in "${python_helper_files[@]}"; do
                present=0
                for requested in "${files[@]}"; do
                    if [ "${requested}" = "${helper}" ]; then
                        present=1
                        break
                    fi
                done
                if [ "${present}" -eq 0 ]; then
                    files+=("${helper}")
                fi
            done
        fi
    fi

    # Install dependencies before facade/update entrypoints.
    local ordered_files=()
    for module in "${module_files[@]}"; do
        for requested in "${files[@]}"; do
            if [ "${requested}" = "${module}" ]; then
                ordered_files+=("${requested}")
                break
            fi
        done
    done
    for helper in "${python_helper_files[@]}"; do
        for requested in "${files[@]}"; do
            if [ "${requested}" = "${helper}" ]; then
                ordered_files+=("${requested}")
                break
            fi
        done
    done
    for requested in "${files[@]}"; do
        present=0
        for module in "${module_files[@]}"; do
            if [ "${requested}" = "${module}" ]; then
                present=1
                break
            fi
        done
        if [ "${present}" -eq 0 ]; then
            for helper in "${python_helper_files[@]}"; do
                if [ "${requested}" = "${helper}" ]; then
                    present=1
                    break
                fi
            done
        fi
        if [ "${present}" -eq 0 ]; then
            ordered_files+=("${requested}")
        fi
    done
    files=("${ordered_files[@]}")

    if [ "${LUMEN_SELF_UPDATE:-1}" = "0" ]; then
        LUMEN_SELF_UPDATE_RESULT=disabled
        return 0
    fi
    if [ -z "${scripts_dir}" ] || [ ! -d "${scripts_dir}" ]; then
        LUMEN_SELF_UPDATE_RESULT=skipped
        return 0
    fi

    local release_tag="" commit_sha="${LUMEN_SELF_UPDATE_COMMIT:-}"
    if [[ "${source_ref}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
        release_tag="${source_ref}"
        local manifest_file="${LUMEN_SELF_UPDATE_MANIFEST_FILE:-}"
        local manifest_tmp=""
        if [ -z "${manifest_file}" ]; then
            manifest_tmp="$(mktemp 2>/dev/null)" || {
                LUMEN_SELF_UPDATE_RESULT=failed
                return 0
            }
            if ! command -v lumen_fetch_release_manifest >/dev/null 2>&1 \
                    || ! lumen_fetch_release_manifest "${release_tag}" "${manifest_tmp}"; then
                rm -f "${manifest_tmp}" 2>/dev/null || true
                log_warn "[self_update] 无法获取 ${release_tag} 的 release manifest，拒绝覆盖脚本。"
                LUMEN_SELF_UPDATE_RESULT=failed
                return 0
            fi
            manifest_file="${manifest_tmp}"
        fi
        local manifest_commit=""
        manifest_commit="$(python3 - "${manifest_file}" "${release_tag}" <<'PY' 2>/dev/null || true
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
commit = payload.get("commit_sha")
if payload.get("version") != sys.argv[2] or not isinstance(commit, str):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit(1)
print(commit)
PY
)"
        [ -n "${manifest_tmp}" ] && rm -f "${manifest_tmp}" 2>/dev/null || true
        if [ -z "${manifest_commit}" ] \
                || { [ -n "${commit_sha}" ] && [ "${commit_sha}" != "${manifest_commit}" ]; }; then
            log_warn "[self_update] ${release_tag} 的 release commit 无效或与预期不一致，拒绝覆盖脚本。"
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        commit_sha="${manifest_commit}"
    elif [[ "${source_ref}" =~ ^[0-9a-f]{40}$ ]]; then
        if [ -n "${commit_sha}" ] && [ "${commit_sha}" != "${source_ref}" ]; then
            log_warn "[self_update] commit 与 LUMEN_SELF_UPDATE_COMMIT 不一致，拒绝覆盖脚本。"
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        commit_sha="${source_ref}"
    elif [ -z "${source_ref}" ] && [[ "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        :
    else
        log_warn "[self_update] source=${source_ref:-<empty>} 不是具体 release tag/commit；拒绝从可变 branch 覆盖脚本。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    if [[ ! "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "[self_update] 未解析到有效的 40 位 release commit，拒绝覆盖脚本。"
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE_TAG="${release_tag}"
    LUMEN_SELF_UPDATE_SOURCE_COMMIT="${commit_sha}"

    local marker="${scripts_dir}/.lumen-self-update.last"
    local coverage_marker="${scripts_dir}/.lumen-self-update.files"
    local source_marker="${scripts_dir}/.lumen-self-update.source"
    local coverage_complete=1
    if [ ! -f "${coverage_marker}" ]; then
        coverage_complete=0
    else
        for requested in "${files[@]}"; do
            if ! grep -Fxq "${requested}" "${coverage_marker}" 2>/dev/null; then
                coverage_complete=0
                break
            fi
        done
    fi
    if [ ! -f "${source_marker}" ] \
            || [ "$(cat "${source_marker}" 2>/dev/null || true)" != "${commit_sha}" ]; then
        coverage_complete=0
    fi
    if [ "${LUMEN_SELF_UPDATE_FORCE:-0}" != "1" ] \
            && [ "${coverage_complete}" -eq 1 ] \
            && [ -f "${marker}" ]; then
        local last_ts now_ts age
        last_ts="$(cat "${marker}" 2>/dev/null || echo 0)"
        case "${last_ts}" in
            ''|*[!0-9]*) last_ts=0 ;;
        esac
        now_ts="$(date -u +%s)"
        age=$((now_ts - last_ts))
        if [ "${ttl_sec}" -gt 0 ] && [ "${age}" -lt "${ttl_sec}" ] && [ "${age}" -ge 0 ]; then
            LUMEN_SELF_UPDATE_RESULT=skipped
            return 0
        fi
    fi

    local repo_url="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    local owner_repo="" raw_base=""
    owner_repo="$(lumen_github_repo_slug "${repo_url}")" || true
    if [ -z "${owner_repo}" ]; then
        if command -v log_warn >/dev/null 2>&1; then
            log_warn "[self_update] LUMEN_REPO_URL 不是 https://github.com/<owner>/<repo>(.git)：${repo_url}，跳过。"
        fi
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    raw_base="https://raw.githubusercontent.com/${owner_repo}/${commit_sha}/scripts"
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE="${raw_base}"

    local proxy_url=""
    proxy_url="$(lumen_effective_proxy_url "${SHARED_ENV:-}" 2>/dev/null || true)"
    local curl_cmd=(curl -fsSL --connect-timeout 10 --max-time 60)
    if [ -n "${proxy_url}" ]; then
        curl_cmd+=(--proxy "${proxy_url}")
    fi

    local tmp_dir
    tmp_dir="$(mktemp -d 2>/dev/null)" || { LUMEN_SELF_UPDATE_RESULT=failed; return 0; }

    local f
    for f in "${files[@]}"; do
        case "${f}" in
            ''|.|..|/*|../*|*/../*|*/..|*[!A-Za-z0-9_./-]*)
                if command -v log_warn >/dev/null 2>&1; then
                    log_warn "[self_update] 非法脚本相对路径：${f:-<empty>}，跳过 self-update。"
                fi
                rm -rf "${tmp_dir}" 2>/dev/null || true
                LUMEN_SELF_UPDATE_RESULT=failed
                return 0
                ;;
        esac
        if ! mkdir -p "$(dirname "${tmp_dir}/${f}")"; then
            if command -v log_warn >/dev/null 2>&1; then
                log_warn "[self_update] 无法创建临时模块目录：$(dirname "${tmp_dir}/${f}")。"
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        if ! "${curl_cmd[@]}" "${raw_base}/${f}" -o "${tmp_dir}/${f}" 2>/dev/null; then
            if command -v log_warn >/dev/null 2>&1; then
                log_warn "[self_update] 下载 ${f} 失败（GitHub 不可达？），跳过 self-update（继续用本地脚本）。"
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
        if ! lumen_validate_self_update_file "${f}" "${tmp_dir}/${f}"; then
            if command -v log_warn >/dev/null 2>&1; then
                case "${f}" in
                    *.sh)
                        log_warn "[self_update] ${f} 不是有效 bash 脚本，跳过 self-update。"
                        ;;
                    *.py)
                        log_warn "[self_update] ${f} 不是有效 Python 3 helper，跳过 self-update。"
                        ;;
                    *)
                        log_warn "[self_update] ${f} 文件类型不在 self-update 允许列表，跳过。"
                        ;;
                esac
            fi
            rm -rf "${tmp_dir}" 2>/dev/null || true
            LUMEN_SELF_UPDATE_RESULT=failed
            return 0
        fi
    done

    LUMEN_SELF_UPDATE_BACKUP_TS="$(date -u +%Y%m%d-%H%M%S)"
    local changed=""
    local library_changed=0
    local update_requested=0
    local changed_files=()
    local all_files_list="${tmp_dir}/.all-files"
    local changed_files_list="${tmp_dir}/.changed-files"
    for f in "${files[@]}"; do
        if [ "${f}" = "update.sh" ]; then
            update_requested=1
            break
        fi
    done
    for f in "${files[@]}"; do
        if [ -f "${scripts_dir}/${f}" ] \
                && cmp -s "${tmp_dir}/${f}" "${scripts_dir}/${f}"; then
            continue
        fi
        changed_files+=("${f}")
        changed="${changed}${f} "
        case "${f}" in
            lib.sh|lib/*.sh) library_changed=1 ;;
        esac
    done

    if ! printf '%s\n' "${files[@]}" > "${all_files_list}" \
            || ! printf '%s\n' \
                ${changed_files[@]+"${changed_files[@]}"} > "${changed_files_list}"; then
        rm -rf "${tmp_dir}" 2>/dev/null || true
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi

    local transaction_rc=0
    if lumen_self_update_install_transaction \
            "${scripts_dir}" \
            "${tmp_dir}" \
            "${LUMEN_SELF_UPDATE_BACKUP_TS}" \
            "${commit_sha}" \
            "${all_files_list}" \
            "${changed_files_list}"; then
        :
    else
        transaction_rc=$?
        rm -rf "${tmp_dir}" 2>/dev/null || true
        LUMEN_SELF_UPDATE_RESULT=failed
        case "${transaction_rc}" in
            70|129|130|143)
                return "${transaction_rc}"
                ;;
        esac
        return 0
    fi

    # Preserve facade/update changed tokens used by caller re-exec contracts.
    if [ "${library_changed}" -eq 1 ]; then
        case " ${changed} " in
            *" lib.sh "*) ;;
            *) changed="${changed}lib.sh " ;;
        esac
        if [ "${update_requested}" -eq 1 ]; then
            case " ${changed} " in
                *" update.sh "*) ;;
                *) changed="${changed}update.sh " ;;
            esac
        fi
    fi

    # 每个文件保留最近 N 份备份；find 无匹配时仍成功，兼容 set -e/pipefail。
    local max_keep="${LUMEN_SELF_UPDATE_BAK_KEEP:-5}"
    if [ "${max_keep}" -gt 0 ] 2>/dev/null; then
        local prune_f prune_dir prune_name total del_n
        for prune_f in "${files[@]}"; do
            prune_dir="${scripts_dir}/$(dirname "${prune_f}")"
            prune_name="$(basename "${prune_f}")"
            total="$(find "${prune_dir}" -maxdepth 1 -name "${prune_name}.bak.*" -type f 2>/dev/null | wc -l | tr -d '[:space:]')"
            if [ -n "${total}" ] && [ "${total}" -gt "${max_keep}" ] 2>/dev/null; then
                del_n=$((total - max_keep))
                find "${prune_dir}" -maxdepth 1 -name "${prune_name}.bak.*" -type f 2>/dev/null \
                    | sort \
                    | head -n "${del_n}" \
                    | while IFS= read -r bak_path; do
                        [ -n "${bak_path}" ] && rm -f "${bak_path}" 2>/dev/null || true
                    done
            fi
        done
    fi

    rm -rf "${tmp_dir}" 2>/dev/null || true

    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_CHANGED="${changed}"
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_RESULT=ok
    if command -v log_info >/dev/null 2>&1; then
        if [ -z "${changed}" ]; then
            log_info "[self_update] 远端 ${raw_base} 与本地一致，无需替换。"
        else
            log_info "[self_update] 已从 ${raw_base} 同步：${changed}（旧版备份 *.bak.${LUMEN_SELF_UPDATE_BACKUP_TS}）。"
        fi
    fi
    return 0
}

# Bootstrap-only branch boundary: GitHub API branch -> immutable commit.
lumen_resolve_github_branch_commit() {
    local branch="${1:-}" owner_repo="" body="" commit_sha="" proxy_url=""
    [ "${#branch}" -le 128 ] || branch=""
    case "${branch}" in
        ''|.|..|/*|*/|.*|*.|*'..'*|*//*|*'@{'*|*[!A-Za-z0-9._/-]*)
            log_warn "[self_update] 非法或不安全的 GitHub branch：${branch:-<empty>}。"
            return 1
            ;;
    esac
    owner_repo="$(lumen_github_repo_slug)" || { log_warn "[self_update] LUMEN_REPO_URL 不是受支持的 GitHub 仓库 URL。"; return 1; }
    if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
        log_warn "[self_update] branch bootstrap 需要 curl 和 python3。"
        return 1
    fi
    local curl_cmd=(curl -fsSL --connect-timeout 10 --max-time 30 -H
        'Accept: application/vnd.github+json' --get --data-urlencode "sha=${branch}"
        --data-urlencode "per_page=1")
    proxy_url="$(lumen_effective_proxy_url "${SHARED_ENV:-}" 2>/dev/null || true)"
    [ -z "${proxy_url}" ] || curl_cmd+=(--proxy "${proxy_url}")
    body="$("${curl_cmd[@]}" "https://api.github.com/repos/${owner_repo}/commits" 2>/dev/null)" \
        || { log_warn "[self_update] 无法通过 GitHub API 解析 branch=${branch}。"; return 1; }
    commit_sha="$(python3 -c 'import json,re,sys; p=json.load(sys.stdin); s=p[0].get("sha","") if isinstance(p,list) and p else ""; print(s if isinstance(s,str) and re.fullmatch(r"[0-9a-f]{40}",s) else "")' \
        <<<"${body}" 2>/dev/null || true)"
    [[ "${commit_sha}" =~ ^[0-9a-f]{40}$ ]] \
        || { log_warn "[self_update] GitHub API 未返回 branch=${branch} 的有效 40 位 commit。"; return 1; }
    printf '%s' "${commit_sha}"
}

lumen_self_update_scripts_from_github_branch() {
    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_RESULT=skipped LUMEN_SELF_UPDATE_CHANGED=""
    # shellcheck disable=SC2034  # Public results consumed by sourcing callers.
    LUMEN_SELF_UPDATE_BACKUP_TS="" LUMEN_SELF_UPDATE_SOURCE=""
    # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
    LUMEN_SELF_UPDATE_SOURCE_COMMIT=""
    local scripts_dir="${1:-}" branch="${2:-${LUMEN_SELF_UPDATE_BRANCH:-main}}"
    local ttl_sec="${3:-${LUMEN_SELF_UPDATE_TTL:-600}}" commit_sha="${LUMEN_SELF_UPDATE_COMMIT:-}"
    shift "$(( $# < 3 ? $# : 3 ))"
    [ "${LUMEN_SELF_UPDATE:-1}" != "0" ] \
        || { LUMEN_SELF_UPDATE_RESULT=disabled; return 0; }
    [ -n "${scripts_dir}" ] && [ -d "${scripts_dir}" ] || return 0
    if [ -n "${commit_sha}" ] && [[ ! "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        log_warn "[self_update] LUMEN_SELF_UPDATE_COMMIT 不是有效的 40 位 commit。"
        # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    fi
    commit_sha="${commit_sha:-$(lumen_resolve_github_branch_commit "${branch}")}" || {
        # shellcheck disable=SC2034  # Public result consumed by sourcing callers.
        LUMEN_SELF_UPDATE_RESULT=failed
        return 0
    }
    log_info "[self_update] branch=${branch} 已固定到 commit=${commit_sha}。"
    lumen_self_update_scripts "${scripts_dir}" "${commit_sha}" "${ttl_sec}" "$@"
}

lumen_effective_proxy_url() {
    local env_file="${1:-}"
    local key value
    for key in LUMEN_UPDATE_PROXY_URL LUMEN_HTTP_PROXY HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy; do
        value="${!key:-}"
        if [ -z "${value}" ] && [ -n "${env_file}" ] && [ -f "${env_file}" ]; then
            value="$(lumen_env_value "${key}" "${env_file}")"
        fi
        if [ -n "${value}" ]; then
            printf '%s' "${value}"
            return 0
        fi
    done
    return 1
}

lumen_configure_proxy_env() {
    local env_file="${1:-}"
    local proxy_url no_proxy_value
    proxy_url="$(lumen_effective_proxy_url "${env_file}" 2>/dev/null || true)"
    if [ -z "${proxy_url}" ]; then
        return 1
    fi
    export LUMEN_UPDATE_PROXY_URL="${proxy_url}"
    export LUMEN_HTTP_PROXY="${proxy_url}"
    export HTTP_PROXY="${proxy_url}"
    export HTTPS_PROXY="${proxy_url}"
    export ALL_PROXY="${proxy_url}"
    export http_proxy="${proxy_url}"
    export https_proxy="${proxy_url}"
    export all_proxy="${proxy_url}"

    no_proxy_value="${NO_PROXY:-${no_proxy:-}}"
    if [ -z "${no_proxy_value}" ] && [ -n "${env_file}" ] && [ -f "${env_file}" ]; then
        no_proxy_value="$(lumen_env_value NO_PROXY "${env_file}")"
        [ -n "${no_proxy_value}" ] || no_proxy_value="$(lumen_env_value no_proxy "${env_file}")"
    fi
    no_proxy_value="${no_proxy_value:-127.0.0.1,localhost,::1}"
    export NO_PROXY="${no_proxy_value}"
    export no_proxy="${no_proxy_value}"

    printf '%s' "${proxy_url}"
}

# Host runtime, systemd, storage ownership, and local process helpers are loaded
# from lib/runtime.sh at the end of this facade.

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

# Docker Compose, image verification, release manifest, and image tag helpers
# are loaded from lib/container_release.sh at the end of this facade.

# ---------------------------------------------------------------------------
# 路径安全 & 重试 & release 维护（install/update/uninstall 复用）
# ---------------------------------------------------------------------------

# lumen_path_safe_for_rm <path>
# 校验 <path> 适合作为 rm -rf 的目标。返回 0=safe，1=unsafe（已 log_error）。
# 拒绝：空 / 非绝对 / 长度 < 5 / 含 .. / 等于以下"系统目录"之一：
#   /  /bin /boot /dev /etc /home /lib /lib32 /lib64 /opt /proc /root /run
#   /sbin /srv /sys /tmp /usr /var /Applications /Library /System /Users /private
# 注意：仅拦截"等于"系统目录；/opt/lumen, /opt/lumendata 等子路径不受影响。
lumen_path_safe_for_rm() {
    local p="$1"
    local home_dir="${HOME:-}"
    if [ -z "${p}" ]; then
        log_error "lumen_path_safe_for_rm: 路径为空，拒绝删除。"
        return 1
    fi
    case "${p}" in
        /*) ;;
        *)
            log_error "lumen_path_safe_for_rm: '${p}' 不是绝对路径，拒绝删除。"
            return 1
            ;;
    esac
    if [ "${#p}" -lt 5 ]; then
        log_error "lumen_path_safe_for_rm: '${p}' 路径过短，拒绝删除。"
        return 1
    fi
    case "${p}" in
        *..*)
            log_error "lumen_path_safe_for_rm: '${p}' 包含 '..'，拒绝删除。"
            return 1
            ;;
    esac
    case "${p}" in
        /|/bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/Applications|/Library|/System|/Users|/private)
            log_error "lumen_path_safe_for_rm: '${p}' 是系统目录，拒绝删除。"
            return 1
            ;;
    esac
    if [ -n "${home_dir}" ] && [ "${p%/}" = "${home_dir%/}" ]; then
        log_error "lumen_path_safe_for_rm: '${p}' 是当前用户 HOME，拒绝删除。"
        return 1
    fi
    # 移除多余的尾部斜杠后再次校验（避免 "/opt/" 通过）
    local trimmed="${p}"
    while [[ "${trimmed}" == */ && "${trimmed}" != "/" ]]; do
        trimmed="${trimmed%/}"
    done
    case "${trimmed}" in
        /|/bin|/boot|/dev|/etc|/home|/lib|/lib32|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/Applications|/Library|/System|/Users|/private)
            log_error "lumen_path_safe_for_rm: 规范化后 '${trimmed}' 仍是系统目录，拒绝删除。"
            return 1
            ;;
    esac
    if [ -n "${home_dir}" ] && [ "${trimmed}" = "${home_dir%/}" ]; then
        log_error "lumen_path_safe_for_rm: 规范化后 '${trimmed}' 是当前用户 HOME，拒绝删除。"
        return 1
    fi
    return 0
}

# lumen_safe_rm_rf <path>
# 在 rm -rf 之前用 lumen_path_safe_for_rm 把关。返回 rm 的退出码（或校验失败时 1）。
lumen_safe_rm_rf() {
    local target="$1"
    if ! lumen_path_safe_for_rm "${target}"; then
        return 1
    fi
    rm -rf -- "${target}"
}

# lumen_safe_rm_rf_as_root <path>
# 同 lumen_safe_rm_rf，但通过 lumen_run_as_root 执行（处理需要 root 权限的目录）。
lumen_safe_rm_rf_as_root() {
    local target="$1"
    if ! lumen_path_safe_for_rm "${target}"; then
        return 1
    fi
    lumen_run_as_root rm -rf -- "${target}"
}

# lumen_release_remove_unused <root> <release_id>
# 删除一个 release 目录，但拒绝删除 current/previous 当前指向的 release。
# 失败/被拒绝时 log_warn 并返回非零；不抛异常（让调用方决定如何处理）。
lumen_release_remove_unused() {
    local root="$1"
    local release_id="$2"
    if [ -z "${release_id}" ]; then
        log_warn "lumen_release_remove_unused: release_id 为空，跳过。"
        return 1
    fi
    local target="${root}/releases/${release_id}"
    if [ ! -d "${target}" ]; then
        return 0
    fi
    local cur_id prev_id=""
    cur_id="$(lumen_release_current_id "${root}" || true)"
    if [ -L "${root}/previous" ]; then
        local prev_link
        prev_link="$(readlink "${root}/previous" 2>/dev/null || true)"
        [ -n "${prev_link}" ] && prev_id="$(basename "${prev_link}")"
    fi
    if [ "${release_id}" = "${cur_id}" ]; then
        log_warn "lumen_release_remove_unused: ${release_id} 是当前 current，拒绝删除。"
        return 1
    fi
    if [ "${release_id}" = "${prev_id}" ]; then
        log_warn "lumen_release_remove_unused: ${release_id} 是 previous，拒绝删除。"
        return 1
    fi
    if ! lumen_path_safe_for_rm "${target}"; then
        return 1
    fi
    if rm -rf -- "${target}" 2>/dev/null; then
        log_info "已删除未使用的 release：${target}"
        return 0
    fi
    if lumen_run_as_root rm -rf -- "${target}" 2>/dev/null; then
        log_info "已删除未使用的 release（root 权限）：${target}"
        return 0
    fi
    log_warn "无法删除 release 目录：${target}"
    return 1
}

# lumen_retry <max_attempts> <initial_delay_seconds> <label> <cmd...>
# 指数退避重试。每次失败后 sleep delay，下次 delay 翻倍（最大 30s）。
# label 仅用于日志（如 "docker compose pull"）。返回最后一次的退出码。
lumen_retry() {
    local max_attempts="$1"
    local delay="$2"
    local label="$3"
    shift 3
    local attempt=1
    local rc=0
    while :; do
        rc=0
        "$@" || rc=$?
        if [ "${rc}" -eq 0 ]; then
            return 0
        fi
        # 用户中断（SIGINT=130 / SIGTERM=143）立即 break，不要白白退避 5/10/20s
        # 浪费用户时间。下游也能更快进入 EXIT trap 的清理流程。
        if [ "${rc}" -eq 130 ] || [ "${rc}" -eq 143 ]; then
            log_warn "${label}：被信号中断（rc=${rc}），不再重试。"
            return "${rc}"
        fi
        if [ "${attempt}" -ge "${max_attempts}" ]; then
            log_error "${label}：连续 ${attempt} 次失败（rc=${rc}），不再重试。"
            return "${rc}"
        fi
        log_warn "${label}：第 ${attempt} 次失败（rc=${rc}），${delay}s 后重试。"
        sleep "${delay}"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
        if [ "${delay}" -gt 30 ]; then
            delay=30
        fi
    done
}

# ---------------------------------------------------------------------------
# Structured module facade
# ---------------------------------------------------------------------------

# Resolve modules from lib.sh itself, never from caller-owned SCRIPT_DIR. This
# keeps sourcing reliable through release symlinks and paths containing spaces.
_LUMEN_LIB_SOURCE="${BASH_SOURCE[0]:-}"
if [ -z "${_LUMEN_LIB_SOURCE}" ]; then
    log_error "无法解析 scripts/lib.sh 路径，不能加载 shell 模块。"
    if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
        exit 1
    fi
    return 1
fi
_LUMEN_LIB_SCRIPTS_DIR="$(cd "$(dirname "${_LUMEN_LIB_SOURCE}")" && pwd -P)"
_LUMEN_LIB_MODULES=(
    lib/environment.sh
    lib/step_protocol.sh
    lib/runtime.sh
    lib/locking.sh
    lib/container_release.sh
    lib/release_layout.sh
)
_LUMEN_LIB_MISSING=()
for _LUMEN_LIB_MODULE in "${_LUMEN_LIB_MODULES[@]}"; do
    if [ ! -f "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}" ]; then
        _LUMEN_LIB_MISSING+=("${_LUMEN_LIB_MODULE}")
    fi
done

# Older updaters fetched only lib.sh; bootstrap one commit-wide facade unit.
if [ "${#_LUMEN_LIB_MISSING[@]}" -gt 0 ]; then
    log_warn "lib.sh 缺少模块，尝试从固定 branch commit 补齐：${_LUMEN_LIB_MISSING[*]}"
    _LUMEN_LIB_FORCE_WAS_SET=0
    _LUMEN_LIB_FORCE_PREVIOUS=""
    if [ "${LUMEN_SELF_UPDATE_FORCE+x}" = "x" ]; then
        _LUMEN_LIB_FORCE_WAS_SET=1
        _LUMEN_LIB_FORCE_PREVIOUS="${LUMEN_SELF_UPDATE_FORCE}"
    fi
    LUMEN_SELF_UPDATE_FORCE=1
    lumen_self_update_scripts_from_github_branch \
        "${_LUMEN_LIB_SCRIPTS_DIR}" \
        "${LUMEN_SELF_UPDATE_BRANCH:-main}" \
        0 \
        lib.sh \
        ${_LUMEN_LIB_MODULES[@]+"${_LUMEN_LIB_MODULES[@]}"}
    if [ "${_LUMEN_LIB_FORCE_WAS_SET}" -eq 1 ]; then
        LUMEN_SELF_UPDATE_FORCE="${_LUMEN_LIB_FORCE_PREVIOUS}"
    else
        unset LUMEN_SELF_UPDATE_FORCE
    fi
fi

for _LUMEN_LIB_MODULE in "${_LUMEN_LIB_MODULES[@]}"; do
    if [ ! -f "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}" ]; then
        log_error "缺少 shell 模块：${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}"
        if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
            exit 1
        fi
        return 1
    fi
    # shellcheck source=/dev/null
    . "${_LUMEN_LIB_SCRIPTS_DIR}/${_LUMEN_LIB_MODULE}"
done

unset _LUMEN_LIB_SOURCE
unset _LUMEN_LIB_SCRIPTS_DIR
unset _LUMEN_LIB_MODULES
unset _LUMEN_LIB_MISSING
unset _LUMEN_LIB_MODULE
unset _LUMEN_LIB_FORCE_WAS_SET
unset _LUMEN_LIB_FORCE_PREVIOUS
