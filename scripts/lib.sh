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

lumen_effective_proxy_url() {
    local env_file="${1:-}"
    local key value
    for key in LUMEN_UPDATE_PROXY_URL LUMEN_HTTP_PROXY HTTPS_PROXY HTTP_PROXY ALL_PROXY https_proxy http_proxy all_proxy; do
        value="${!key:-}"
        if [ -z "${value}" ] \
                && command -v lumen_env_value >/dev/null 2>&1 \
                && [ -n "${env_file}" ] \
                && [ -f "${env_file}" ]; then
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

lumen_bootstrap_self_update_module() {
    local module_path="$1"
    local branch="${LUMEN_SELF_UPDATE_BRANCH:-main}"
    local owner_repo="" commit_sha="${LUMEN_SELF_UPDATE_COMMIT:-}"
    local tmp_path="" proxy_url=""
    owner_repo="$(lumen_github_repo_slug)" || return 1
    if [[ ! "${commit_sha}" =~ ^[0-9a-f]{40}$ ]]; then
        commit_sha="$(lumen_resolve_github_branch_commit "${branch}")" || return 1
    fi
    mkdir -p "$(dirname "${module_path}")" || return 1
    tmp_path="$(mktemp "${module_path}.bootstrap.XXXXXX")" || return 1
    local curl_cmd=(curl -fsSL --connect-timeout 10 --max-time 60)
    proxy_url="$(lumen_effective_proxy_url "${SHARED_ENV:-}" 2>/dev/null || true)"
    [ -z "${proxy_url}" ] || curl_cmd+=(--proxy "${proxy_url}")
    if ! "${curl_cmd[@]}" \
            "https://raw.githubusercontent.com/${owner_repo}/${commit_sha}/scripts/lib/self_update.sh" \
            -o "${tmp_path}" \
            || ! bash -n "${tmp_path}"; then
        rm -f "${tmp_path}"
        return 1
    fi
    chmod 0644 "${tmp_path}" || { rm -f "${tmp_path}"; return 1; }
    mv -f "${tmp_path}" "${module_path}"
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
_LUMEN_LIB_SELF_UPDATE_MODULE="${_LUMEN_LIB_SCRIPTS_DIR}/lib/self_update.sh"
if [ ! -f "${_LUMEN_LIB_SELF_UPDATE_MODULE}" ]; then
    log_warn "lib.sh 缺少 self-update 模块，尝试从固定 branch commit 引导。"
    lumen_bootstrap_self_update_module "${_LUMEN_LIB_SELF_UPDATE_MODULE}" || true
fi
if [ -f "${_LUMEN_LIB_SELF_UPDATE_MODULE}" ]; then
    # shellcheck source=/dev/null
    . "${_LUMEN_LIB_SELF_UPDATE_MODULE}"
fi
_LUMEN_LIB_MODULES=(
    lib/system.sh
    lib/environment.sh
    lib/step_protocol.sh
    lib/runtime.sh
    lib/locking.sh
    lib/container_release.sh
    lib/release_layout.sh
    lib/self_update.sh
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
    if command -v lumen_self_update_scripts_from_github_branch >/dev/null 2>&1; then
        lumen_self_update_scripts_from_github_branch \
            "${_LUMEN_LIB_SCRIPTS_DIR}" \
            "${LUMEN_SELF_UPDATE_BRANCH:-main}" \
            0 \
            lib.sh \
            ${_LUMEN_LIB_MODULES[@]+"${_LUMEN_LIB_MODULES[@]}"}
    fi
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
unset _LUMEN_LIB_SELF_UPDATE_MODULE
unset _LUMEN_LIB_MODULES
unset _LUMEN_LIB_MISSING
unset _LUMEN_LIB_MODULE
unset _LUMEN_LIB_FORCE_WAS_SET
unset _LUMEN_LIB_FORCE_PREVIOUS
