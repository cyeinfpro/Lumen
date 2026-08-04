#!/usr/bin/env bash
# Lumen 一键安装脚本（Docker Compose 全栈版）
# 用法：bash scripts/install.sh [--install] [--build] [--image-tag=vX.Y.Z]
#      [--data-root=/data] [--db-root=/var/lib/lumen-data]
# 入口负责 bootstrap、参数解析和流程编排；安装职责位于 scripts/install/*.sh。
# 已有部署必须走 update；fresh install 失败时清理已启动容器但不删除数据卷。
# LUMEN_NONINTERACTIVE=1 时从 LUMEN_ADMIN_EMAIL/LUMEN_ADMIN_PASSWORD 读取凭据。
set -euo pipefail

_LUMEN_INSTALL_INPUT_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT-}"

# `curl | bash` 下 BASH_SOURCE 可为空，set -u 会让访问 [0] 报
# unbound variable 噪音；用 :- 兜底，dirname "" 返回 "." 落到 cwd。
RAW_INSTALL_FROM_STDIN=0
if [ -z "${BASH_SOURCE[0]:-}" ]; then
    RAW_INSTALL_FROM_STDIN=1
fi

if SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"; then
    :
else
    SCRIPT_DIR="$(pwd)"
fi
if [ "${BASH_SOURCE[0]:-}" = "${0}" ] && [ -f "${SCRIPT_DIR}/lib.sh" ]; then
    _LUMEN_ENTRY_LOCK_HELPER="${SCRIPT_DIR}/update/entry_lock.py"
    _LUMEN_ENTRY_LOCK_SCRIPTS_DIR="$(cd "${SCRIPT_DIR}" && pwd -P)"
    _LUMEN_ENTRY_LOCK_PATH="${_LUMEN_ENTRY_LOCK_SCRIPTS_DIR}.lumen-self-update.lock"
    if [ ! -f "${_LUMEN_ENTRY_LOCK_HELPER}" ] \
            || [ -L "${_LUMEN_ENTRY_LOCK_HELPER}" ]; then
        printf '[ERROR] installer 脚本单元缺少安全入口锁 helper。\n' >&2
        exit 78
    fi
    if ! python3 "${_LUMEN_ENTRY_LOCK_HELPER}" verify \
            "${LUMEN_SCRIPT_UNIT_LOCK_FD:-}" \
            "${_LUMEN_ENTRY_LOCK_PATH}" >/dev/null 2>&1; then
        exec python3 "${_LUMEN_ENTRY_LOCK_HELPER}" exec \
            "${_LUMEN_ENTRY_LOCK_PATH}" \
            "${LUMEN_SELF_UPDATE_LOCK_TIMEOUT:-60}" \
            -- bash "${BASH_SOURCE[0]}" "$@"
    fi
    unset _LUMEN_ENTRY_LOCK_HELPER _LUMEN_ENTRY_LOCK_SCRIPTS_DIR \
        _LUMEN_ENTRY_LOCK_PATH
fi

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
    elif raw_have_cmd brew; then
        brew install "$@"
    else
        return 1
    fi
}

raw_install_git() {
    printf '[INFO] 缺少 git，尝试自动安装。\n'
    raw_refresh_tool_paths
    case "$(uname -s 2>/dev/null || echo unknown)" in
        Darwin)
            if ! raw_have_cmd brew; then
                printf '[ERROR] macOS 缺少 git，且未发现 Homebrew。请先安装 Xcode Command Line Tools 或 Homebrew 后重跑。\n' >&2
                return 1
            fi
            if raw_have_cmd brew; then
                brew install git
            else
                printf '[ERROR] macOS 缺少 git，且未发现可用 brew。请先安装 Xcode Command Line Tools 或 Homebrew 后重跑。\n' >&2
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

raw_drain_bootstrap_stdin() {
    # Drain curl's script pipe before exec so curl does not report rc=23.
    if [ "${RAW_INSTALL_FROM_STDIN:-0}" = "1" ] && [ ! -t 0 ]; then
        cat >/dev/null 2>/dev/null || true
    fi
}

raw_github_repo_slug() {
    local repo_url="$1" owner="" repository=""
    case "${repo_url}" in
        https://github.com/*)
            repo_url="${repo_url#https://github.com/}"
            repo_url="${repo_url%.git}"
            ;;
        *)
            return 1
            ;;
    esac
    case "${repo_url}" in
        */*/*|/*|*/) return 1 ;;
    esac
    owner="${repo_url%%/*}"
    repository="${repo_url#*/}"
    case "${owner}:${repository}" in
        :*|*:|*[!A-Za-z0-9_.:-]*)
            return 1
            ;;
    esac
    printf '%s\n' "${repo_url}"
}

raw_ensure_stable_resolver_tools() {
    local missing=()
    raw_have_cmd curl || missing+=(curl ca-certificates)
    raw_have_cmd python3 || missing+=(python3)
    if [ "${#missing[@]}" -gt 0 ]; then
        printf '[INFO] stable bootstrap 需要 curl/python3，尝试自动安装。\n'
        raw_install_packages "${missing[@]}" || true
    fi
    raw_have_cmd curl && raw_have_cmd python3
}

raw_resolve_stable_source() {
    local repo_url="$1"
    local requested_tag="$2"
    local tmp_dir="$3"
    local slug="" tag="${requested_tag}" manifest="" latest=""
    slug="$(raw_github_repo_slug "${repo_url}")" || {
        printf '[ERROR] stable bootstrap 仅支持 https://github.com/<owner>/<repo>.git。\n' >&2
        return 1
    }
    raw_ensure_stable_resolver_tools || {
        printf '[ERROR] 缺少 curl/python3，无法在 clone 前绑定 stable release。\n' >&2
        return 1
    }
    mkdir -p "${tmp_dir}"
    if [ -z "${tag}" ] || [ "${tag}" = "latest" ]; then
        latest="${tmp_dir}/latest.json"
        if ! curl -fsSL --proto '=https' --proto-redir '=https' \
                --connect-timeout 10 --max-time 60 \
                "https://api.github.com/repos/${slug}/releases/latest" \
                -o "${latest}"; then
            printf '[ERROR] 无法解析 GitHub latest Release。\n' >&2
            return 1
        fi
        tag="$(
            python3 - "${latest}" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
tag = payload.get("tag_name") if isinstance(payload, dict) else None
if not isinstance(tag, str) or not re.fullmatch(
    r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?",
    tag,
):
    raise SystemExit(1)
print(tag)
PY
        )" || {
            printf '[ERROR] latest Release tag 无效。\n' >&2
            return 1
        }
    fi
    case "${tag}" in
        v[0-9]*.[0-9]*.[0-9]*) ;;
        *)
            printf '[ERROR] stable bootstrap tag 无效：%s\n' "${tag}" >&2
            return 1
            ;;
    esac
    manifest="${tmp_dir}/release-manifest.json"
    if ! curl -fsSL --proto '=https' --proto-redir '=https' \
            --connect-timeout 10 --max-time 60 \
            "https://github.com/${slug}/releases/download/${tag}/release-manifest.json" \
            -o "${manifest}"; then
        printf '[ERROR] 无法下载 %s 的 release-manifest.json。\n' "${tag}" >&2
        return 1
    fi
    local commit=""
    commit="$(
        python3 - "${manifest}" "${tag}" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
commit = payload.get("commit_sha") if isinstance(payload, dict) else None
if (
    not isinstance(payload, dict)
    or not re.fullmatch(
        r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?",
        sys.argv[2],
    )
    or payload.get("schema_version") != 1
    or payload.get("version") != sys.argv[2]
    or not isinstance(payload.get("images"), dict)
    or not isinstance(commit, str)
    or not re.fullmatch(r"[0-9a-f]{40}", commit)
):
    raise SystemExit(1)
print(commit)
PY
    )" || {
        printf '[ERROR] %s release manifest 无效。\n' "${tag}" >&2
        return 1
    }
    printf '%s\t%s\n' "${tag}" "${commit}"
}

raw_select_bootstrap_source() {
    local repo_url="$1"
    shift
    local channel="${LUMEN_INSTALL_CHANNEL:-${LUMEN_UPDATE_CHANNEL:-stable}}"
    local image_tag="${LUMEN_IMAGE_TAG:-}"
    local arg="" resolver_tmp="" resolved=""
    for arg in "$@"; do
        case "${arg}" in
            --image-tag=*) image_tag="${arg#*=}" ;;
        esac
    done
    if [ -z "${LUMEN_INSTALL_CHANNEL+x}" ] \
            && [ -z "${LUMEN_UPDATE_CHANNEL+x}" ] \
            && [ -z "${image_tag}" ] \
            && [ -n "${LUMEN_BRANCH+x}" ]; then
        channel="main"
    fi

    RAW_BOOTSTRAP_SOURCE_MODE="stable"
    RAW_BOOTSTRAP_SOURCE_REF=""
    RAW_BOOTSTRAP_SOURCE_TAG=""
    RAW_BOOTSTRAP_SOURCE_COMMIT=""
    case "${image_tag}" in
        v[0-9]*.[0-9]*.[0-9]*) channel="stable" ;;
    esac
    if [ "${channel}" = "main" ] || [ "${image_tag}" = "main" ]; then
        RAW_BOOTSTRAP_SOURCE_MODE="rolling"
        RAW_BOOTSTRAP_SOURCE_REF="${LUMEN_BRANCH:-main}"
        export LUMEN_IMAGE_TAG="main"
        return 0
    fi
    if [ "${channel}" != "stable" ]; then
        printf '[ERROR] raw install 只接受 stable 或显式 main channel：%s\n' \
            "${channel}" >&2
        return 1
    fi
    if [ -n "${image_tag}" ] && [ "${image_tag}" != "latest" ]; then
        case "${image_tag}" in
            v[0-9]*.[0-9]*.[0-9]*) ;;
            *)
                printf '[ERROR] stable raw install 的 image tag 必须是 latest 或 vX.Y.Z。\n' >&2
                return 1
                ;;
        esac
    fi
    resolver_tmp="$(mktemp -d)" || return 1
    resolved="$(
        raw_resolve_stable_source \
            "${repo_url}" "${image_tag:-latest}" "${resolver_tmp}"
    )" || {
        rm -rf "${resolver_tmp}" 2>/dev/null || true
        return 1
    }
    rm -rf "${resolver_tmp}" 2>/dev/null || true
    IFS=$'\t' read -r RAW_BOOTSTRAP_SOURCE_TAG \
        RAW_BOOTSTRAP_SOURCE_COMMIT <<< "${resolved}"
    RAW_BOOTSTRAP_SOURCE_REF="${RAW_BOOTSTRAP_SOURCE_TAG}"
    export LUMEN_INSTALL_RESOLVED_TAG="${RAW_BOOTSTRAP_SOURCE_TAG}"
    export LUMEN_INSTALL_RESOLVED_COMMIT="${RAW_BOOTSTRAP_SOURCE_COMMIT}"
}

raw_load_bootstrap_helper() {
    local repo_url="$1" helper="${SCRIPT_DIR}/install/raw_bootstrap.sh"
    local slug="" ref="" tmp=""
    if [ -f "${helper}" ] && [ ! -L "${helper}" ]; then
        bash -n "${helper}" || return 1
        # shellcheck source=/dev/null
        . "${helper}"
        return 0
    fi
    raw_have_cmd curl || raw_install_packages curl ca-certificates || return 1
    slug="$(raw_github_repo_slug "${repo_url}")" || return 1
    ref="${RAW_BOOTSTRAP_SOURCE_COMMIT:-${RAW_BOOTSTRAP_SOURCE_REF}}"
    tmp="$(mktemp)" || return 1
    if ! curl -fsSL --proto '=https' --proto-redir '=https' \
            --connect-timeout 10 --max-time 60 \
            "https://raw.githubusercontent.com/${slug}/${ref}/scripts/install/raw_bootstrap.sh" \
            -o "${tmp}" \
            || ! bash -n "${tmp}"; then
        rm -f "${tmp}" 2>/dev/null || true
        return 1
    fi
    # shellcheck source=/dev/null
    . "${tmp}"
    rm -f "${tmp}" 2>/dev/null || true
}

raw_bootstrap_entry() {
    local repo_url="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    local args=("$@")
    [ "${#args[@]}" -gt 0 ] || args=("menu")
    if ! raw_have_cmd git \
            && { ! raw_install_git || ! raw_have_cmd git; }; then
        printf '[ERROR] 缺少 git，且自动安装失败。\n' >&2
        return 1
    fi
    raw_select_bootstrap_source "${repo_url}" "${args[@]}" || return 1
    raw_load_bootstrap_helper "${repo_url}" || {
        printf '[ERROR] 无法加载已绑定 source 的 raw bootstrap helper。\n' >&2
        return 1
    }
    bootstrap_from_raw_script "${repo_url}" "${args[@]}"
}

if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    # Preserve the menu default，避免脚本一运行就跳过菜单。
    raw_bootstrap_entry "$@"
fi

# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
OS="$(detect_os)"

# Full-repository install modules. Raw curl|bash bootstrap above must stay
# self-contained because these files do not exist until the repository lands.
INSTALL_MODULE_DIR="${SCRIPT_DIR}/install"
for install_module in \
        state.sh environment.sh runtime.sh prerequisites.sh layout.sh \
        services.sh operations.sh entrypoint.sh; do
    if [ ! -f "${INSTALL_MODULE_DIR}/${install_module}" ]; then
        log_error "缺少安装模块：${INSTALL_MODULE_DIR}/${install_module}"
        exit 1
    fi
    # shellcheck source=/dev/null
    . "${INSTALL_MODULE_DIR}/${install_module}"
done
unset install_module
dispatch_entrypoint "$@"

# ---------------------------------------------------------------------------
# 失败处理 / 锁
# 锁机制：先解析受信任的目标 DEPLOY_ROOT，再使用目标根的维护锁与
# update.sh / backup.sh / restore.sh / uninstall.sh 互斥。
# ---------------------------------------------------------------------------
INSTALL_PHASE=""               # 当前阶段名（用于错误时报告 + step protocol）
INSTALL_STARTED_SERVICES=()    # 已启动的 compose service 列表（失败时 stop）
INSTALL_TGBOT_STATUS=""        # started / failed / skipped；print_summary 汇报
INSTALL_STATE_SNAPSHOT_READY=0
INSTALL_ENV_SNAPSHOT=""
INSTALL_ORIGINAL_CURRENT_PRESENT=0
INSTALL_ORIGINAL_CURRENT_TARGET=""
INSTALL_ORIGINAL_PREVIOUS_PRESENT=0
INSTALL_ORIGINAL_PREVIOUS_TARGET=""
INSTALL_ORIGINAL_RUNNING_SERVICES=""
INSTALL_HOST_ARTIFACT_SNAPSHOT=""
INSTALL_GHCR_PROBE_FILE=""
INSTALL_SOURCE_COMMIT=""
INSTALL_SOURCE_COMMIT_PROOF=""
INSTALL_PHASE_START_TS=""
INSTALL_JOURNAL_DIR=""
INSTALL_TRANSACTION_COMMITTED=0
INSTALL_RECOVERED_COMPLETE=0

# ---------------------------------------------------------------------------
# 主流程
#
# trap 顺序很关键，避免被 lumen_acquire_lock 内部的 trap 覆盖：
#   1) 先装 INT/TERM/ERR：让 lumen_acquire_lock 之前的代码（参数解析、
#      DEPLOY_ROOT 推导）按 Ctrl-C 也能走 cleanup（rc != 0 时不会清半成品 —
#      因为还没建任何东西，但至少日志说明清晰）。
#   2) lumen_acquire_lock 自身会装 `trap lumen_release_lock EXIT`；不能在它
#      之前装其他 EXIT trap，否则会被覆盖。
#   3) lumen_acquire_lock 之后用 cleanup_on_failure 覆盖 EXIT trap：cleanup
#      末尾会幂等调 lumen_release_lock（line ~533），保证锁仍释放。
# ---------------------------------------------------------------------------
trap 'on_error ${LINENO}' ERR
trap 'on_signal SIGINT 130' INT
trap 'on_signal SIGTERM 143' TERM
trap 'on_signal SIGHUP 129' HUP

# 解析并准备最终部署根。显式 LUMEN_DEPLOY_ROOT 必须是绝对、无 traversal、
# 无 symlink 歧义的路径；未设置时，release 内运行解析回部署根，开发 checkout
# 保持使用自己的物理根目录。只允许在该目标根拿锁后接受 bootstrap transaction。
if ! DEPLOY_ROOT="$(
        lumen_resolve_install_deploy_root \
            "${SCRIPT_DIR}" "${_LUMEN_INSTALL_INPUT_DEPLOY_ROOT}"
)"; then
    log_error "拒绝不安全或有歧义的安装部署根目录。"
    exit 78
fi
LUMEN_DEPLOY_ROOT="${DEPLOY_ROOT}"
export LUMEN_DEPLOY_ROOT
unset _LUMEN_INSTALL_INPUT_DEPLOY_ROOT

# 全局维护锁：所有部署操作共用目标根的 .lumen-maintenance.lock。
lumen_acquire_lock "${DEPLOY_ROOT}" "install.sh"

# 锁拿到后再装 cleanup_on_failure，覆盖 lumen_acquire_lock 内层的 release_lock
# trap。cleanup 内 chain 调 release_lock 幂等。
trap cleanup_on_failure EXIT

if [ -n "${LUMEN_RAW_BOOTSTRAP_TRANSACTION:-}" ]; then
    if [ ! -f "${SCRIPT_DIR}/install/bootstrap_transaction.py" ] \
            || [ -L "${SCRIPT_DIR}/install/bootstrap_transaction.py" ]; then
        log_error "bootstrap transaction helper 缺失，拒绝接受新脚本单元。"
        exit 70
    fi
    python3 "${SCRIPT_DIR}/install/bootstrap_transaction.py" accept \
        "${LUMEN_RAW_BOOTSTRAP_TRANSACTION}"
    unset LUMEN_RAW_BOOTSTRAP_TRANSACTION
fi

LUMEN_DATA_ROOT="${INSTALL_DATA_ROOT_OVERRIDE:-${LUMEN_DATA_ROOT:-/opt/lumendata}}"
LUMEN_DB_ROOT="${INSTALL_DB_ROOT_OVERRIDE:-${LUMEN_DB_ROOT:-${LUMEN_DATA_ROOT}}}"
LUMEN_POSTGRES_UID="${LUMEN_POSTGRES_UID:-999}"
LUMEN_POSTGRES_GID="${LUMEN_POSTGRES_GID:-999}"
LUMEN_REDIS_UID="${LUMEN_REDIS_UID:-999}"
LUMEN_REDIS_GID="${LUMEN_REDIS_GID:-999}"
LUMEN_APP_UID="${LUMEN_APP_UID:-10001}"
LUMEN_APP_GID="${LUMEN_APP_GID:-10001}"
LUMEN_APP_STORAGE_GID="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID}}"
RELEASE_DIR=""
RELEASE_ID=""
SHARED_DIR=""
INSTALL_ADMIN_EMAIL=""
COMPOSE_LABEL="COMPOSE_PROJECT_NAME=lumen docker compose"

log_step "Lumen Docker Compose 全栈安装（OS=${OS}, deploy=${DEPLOY_ROOT}, data=${LUMEN_DATA_ROOT}, db=${LUMEN_DB_ROOT}）"

if ! recover_stale_install_transaction; then
    log_error "无法恢复 stale fresh-install journal，拒绝覆盖现场。"
    exit 70
fi
if [ "${INSTALL_RECOVERED_COMPLETE}" -eq 1 ]; then
    trap - ERR EXIT INT TERM HUP
    lumen_release_lock 2>/dev/null || true
    exit 0
fi

guard_install_target_is_fresh
check_prerequisites
prepare_data_dirs
prepare_release_layout
install_transaction_set_phase snapshot
install_transaction_failpoint snapshot
if ! snapshot_install_state; then
    log_error "无法创建安装事务快照，拒绝继续。"
    exit 1
fi
install_transaction_set_phase prepare_env
install_transaction_failpoint prepare_env
prepare_env_file
install_transaction_set_phase probe_images
install_transaction_failpoint probe_images
probe_ghcr_image_tag
install_transaction_set_phase pull_images
install_transaction_failpoint pull_images
pull_or_build_images
install_transaction_set_phase start_infrastructure
install_transaction_failpoint start_infrastructure
start_infrastructure
install_transaction_set_phase migrate_db
install_transaction_failpoint migrate_db
run_migration
install_transaction_set_phase bootstrap_admin
install_transaction_failpoint bootstrap_admin
run_bootstrap_admin
install_transaction_set_phase metadata
install_transaction_failpoint metadata
write_install_release_metadata
install_transaction_set_phase ownership
harden_install_release_ownership
install_transaction_failpoint ownership
install_transaction_set_phase start_services
install_transaction_failpoint start_services
if ! lumen_verify_backup_service_layout_binding; then
    log_error "backup/maintenance root binding 在应用服务激活前失效。"
    exit 70
fi
start_application_services
install_transaction_set_phase switch
install_transaction_failpoint switch
switch_current_symlink
install_transaction_set_phase host_operations
install_transaction_failpoint host_operations
install_update_runner_units
install_storage_control_plane
install_transaction_set_phase health
install_transaction_failpoint health
run_health_checks
warn_about_legacy_systemd
install_transaction_set_phase summary
print_summary

install_transaction_mark_complete
trap - ERR EXIT INT TERM HUP
install_transaction_failpoint complete
if ! install_transaction_cleanup; then
    log_error "安装已完成，但 durable journal 清理失败；下次重跑只会 finalize cleanup。"
    lumen_release_lock 2>/dev/null || true
    exit 1
fi
discard_install_state_snapshot
lumen_release_lock 2>/dev/null || true
exit 0
