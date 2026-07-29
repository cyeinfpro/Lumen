#!/usr/bin/env bash
# Lumen 一键安装脚本（Docker Compose 全栈版）
# 用法：bash scripts/install.sh [--install] [--build] [--image-tag=vX.Y.Z]
#      [--data-root=/data] [--db-root=/var/lib/lumen-data]
# 入口负责 bootstrap、参数解析和流程编排；安装职责位于 scripts/install/*.sh。
# 重复执行安全。失败时清理已启动容器但不删除数据卷。
# LUMEN_NONINTERACTIVE=1 时从 LUMEN_ADMIN_EMAIL/LUMEN_ADMIN_PASSWORD 读取凭据。
set -euo pipefail

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
    # In `curl .../install.sh | bash`, bootstrap execs into the freshly cloned
    # local script before curl has always finished writing the rest of this
    # file. Drain the script pipe first so curl does not report rc=23.
    if [ "${RAW_INSTALL_FROM_STDIN:-0}" = "1" ] && [ ! -t 0 ]; then
        cat >/dev/null 2>/dev/null || true
    fi
}

# 检测 install_dir 当前状态，返回字符串：
#   empty   不存在或确实是空目录
#   git     已经是 git checkout（有 .git/）
#   release release 布局已就位（current 是 symlink，或 releases/ + shared/）
#   inplace 旧 in-place 部署 / rsync 部署（看到 scripts/lib.sh 或 apps/api 但无 .git）
#   mixed   既不像 Lumen 部署也不是空（杂乱目录，需要保留备份后重建）
detect_install_state() {
    local d="$1"
    if [ ! -e "${d}" ]; then
        printf 'empty'
        return 0
    fi
    if [ -d "${d}/.git" ]; then
        printf 'git'
        return 0
    fi
    if [ -L "${d}/current" ] || { [ -d "${d}/releases" ] && [ -d "${d}/shared" ]; }; then
        printf 'release'
        return 0
    fi
    if [ -f "${d}/scripts/lib.sh" ] || [ -d "${d}/apps/api" ] || [ -d "${d}/packages/core" ]; then
        printf 'inplace'
        return 0
    fi
    if [ ! -d "${d}" ]; then
        printf 'mixed'
        return 0
    fi
    # 真正的空目录也归 empty；无法读取目录时保守视为 mixed，避免 clone 报错或覆盖未知内容。
    if find "${d}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then
        printf 'mixed'
        return 0
    fi
    if [ -r "${d}" ] && [ -x "${d}" ]; then
        printf 'empty'
        return 0
    fi
    # 兜底分支：上面所有判断都没命中（罕见，例如 stat 失败 / 异常 ACL），
    # 视为 mixed 让调用方走"备份后重建"分支，绝不让函数无 stdout 让调用方拿空。
    printf 'mixed'
    return 0
}

# 把最新 main 的代码合并到已有部署目录，保留运行时数据（.env / shared / releases /
# current / var 等）。Docker 全栈版本下 .venv / node_modules / .next 都在镜像里，
# 但保留 exclude 是为了兼容残留的旧 in-place 部署目录。
overlay_repo_into_existing() {
    local repo_url="$1"
    local branch="$2"
    local install_dir="$3"
    local tmp_dir
    tmp_dir="$(mktemp -d)" || return 1
    # shellcheck disable=SC2064
    trap "rm -rf '${tmp_dir}'" RETURN
    printf '[INFO] 在临时目录 clone 最新 %s\n' "${branch}"
    if ! git clone --quiet --depth 1 --branch "${branch}" "${repo_url}" "${tmp_dir}/repo"; then
        printf '[ERROR] git clone 失败。\n' >&2
        return 1
    fi
    if ! raw_have_cmd rsync; then
        printf '[INFO] 缺少 rsync，尝试自动安装。\n'
        raw_install_packages rsync || true
    fi
    if ! raw_have_cmd rsync; then
        printf '[ERROR] 没有 rsync，无法非破坏性合并代码到 %s。\n' "${install_dir}" >&2
        return 1
    fi
    printf '[INFO] 把最新代码合并到 %s（保留 .env / shared / releases / var 等运行时数据）\n' "${install_dir}"
    rsync -a --delete-after \
        --exclude='/.git/' \
        --exclude='/.env' \
        --exclude='/.env.*' \
        --exclude='/shared/' \
        --exclude='/releases/' \
        --exclude='/current' \
        --exclude='/previous' \
        --exclude='/var/' \
        --exclude='/.venv/' \
        --exclude='/node_modules/' \
        --exclude='/apps/worker/var/' \
        --exclude='/apps/web/.next/' \
        --exclude='/apps/web/.env.local' \
        --exclude='/apps/web/node_modules/' \
        --exclude='/.lumen-script.lock/' \
        --exclude='/.update.log' \
        "${tmp_dir}/repo/" "${install_dir}/"
}

overlay_release_scripts() {
    local repo_url="$1"
    local branch="$2"
    local release_dir="$3"
    local tmp_dir
    tmp_dir="$(mktemp -d)" || return 1
    # shellcheck disable=SC2064
    trap "rm -rf '${tmp_dir}'" RETURN
    printf '[INFO] 在临时目录 clone 最新 %s scripts/\n' "${branch}"
    if ! git clone --quiet --depth 1 --branch "${branch}" \
            "${repo_url}" "${tmp_dir}/repo"; then
        printf '[ERROR] git clone 失败。\n' >&2
        return 1
    fi
    if ! raw_have_cmd rsync; then
        printf '[INFO] 缺少 rsync，尝试自动安装。\n'
        raw_install_packages rsync || true
    fi
    if ! raw_have_cmd rsync; then
        printf '[ERROR] 没有 rsync，无法同步 release scripts/。\n' >&2
        return 1
    fi
    if [ ! -f "${tmp_dir}/repo/scripts/install.sh" ] \
            || [ ! -f "${tmp_dir}/repo/scripts/lib.sh" ]; then
        printf '[ERROR] 远端仓库缺少 scripts/install.sh 或 scripts/lib.sh。\n' >&2
        return 1
    fi
    mkdir -p "${release_dir}/scripts"
    printf '[INFO] 只同步 scripts/ 到 %s；release 其余源码保持不变。\n' \
        "${release_dir}"
    rsync -a "${tmp_dir}/repo/scripts/" "${release_dir}/scripts/"
}

bootstrap_from_raw_script() {
    local repo_url="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    local branch="${LUMEN_BRANCH:-main}"
    # root 用户走 /opt/lumen（系统级部署，update.sh / lumen-storage-* systemd unit
    # 都期望 LUMEN_DEPLOY_ROOT=/opt/lumen）；非 root 才回 $HOME/Lumen 个人目录。
    local default_dir
    if [ "${EUID:-$(id -u)}" = "0" ]; then
        default_dir="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
    else
        default_dir="${HOME:-$PWD}/Lumen"
    fi
    local install_dir="${LUMEN_INSTALL_DIR:-${default_dir}}"

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

    local state
    state="$(detect_install_state "${install_dir}")"
    printf '[INFO] 检测到目标目录状态：%s\n' "${state}"

    case "${state}" in
        git)
            # 标准 git checkout：fetch + reset，确保 worktree 干净指向 origin/branch。
            printf '[INFO] 已是 git checkout，拉取最新 %s 并 reset。\n' "${branch}"
            git -C "${install_dir}" fetch --quiet origin "${branch}"
            git -C "${install_dir}" checkout --quiet "${branch}"
            git -C "${install_dir}" reset --hard "origin/${branch}"
            export LUMEN_BOOTSTRAP_MODE="auto"
            ;;
        release)
            # release 布局：current 软链 + shared/releases。代码升级走 update.sh，
            # 这里只把 update.sh / lib.sh 等 scripts 同步到最新，让 update.sh 有新逻辑。
            printf '[INFO] 已是 release 布局，先同步 scripts/ 到最新再交给 update.sh。\n'
            local current_release="${install_dir}/current"
            if [ -L "${current_release}" ]; then
                if ! overlay_release_scripts \
                        "${repo_url}" "${branch}" "${current_release}"; then
                    printf '[ERROR] release scripts/ 同步失败。\n' >&2
                    exit 1
                fi
            else
                printf '[WARN] %s 不是 symlink，跳过 scripts 同步，直接交给 update.sh。\n' "${current_release}" >&2
            fi
            export LUMEN_BOOTSTRAP_MODE="update"
            ;;
        inplace)
            # 老 in-place 部署 / rsync 落地。把代码合并进去（保护运行时数据）。
            # 之后让 update.sh 的 auto-migrate 把 in-place 切到 release 布局。
            printf '[INFO] 检测到旧 in-place 部署，合并最新代码并交给 update.sh 自动迁移。\n'
            if ! overlay_repo_into_existing "${repo_url}" "${branch}" "${install_dir}"; then
                printf '[ERROR] 合并代码失败。\n' >&2
                exit 1
            fi
            export LUMEN_BOOTSTRAP_MODE="update"
            ;;
        mixed)
            # 杂乱目录：备份后重新 clone，避免误删用户数据。
            local backup
            backup="${install_dir}.bak.$(date -u +%Y%m%d%H%M%S 2>/dev/null || date +%s)"
            printf '[WARN] %s 已存在但不像 Lumen 部署，备份到 %s 后重新 clone。\n' "${install_dir}" "${backup}"
            mv "${install_dir}" "${backup}"
            git clone --branch "${branch}" "${repo_url}" "${install_dir}"
            export LUMEN_BOOTSTRAP_MODE="install"
            ;;
        empty|*)
            git clone --branch "${branch}" "${repo_url}" "${install_dir}"
            export LUMEN_BOOTSTRAP_MODE="install"
            ;;
    esac

    # 决定 exec 时传给 install.sh 的参数。调用方没传时保留菜单入口；
    # 需要无人值守更新时显式传 --auto 或 --update，避免脚本一运行就跳过菜单。
    local args=("$@")
    if [ "${#args[@]}" -eq 0 ]; then
        args=("menu")
    fi

    # 选 install.sh 路径：release 布局下 scripts/ 在 current 内（${ROOT}/current/scripts），
    # 而不是 ${ROOT}/scripts；其它布局都在 ${ROOT}/scripts。fallback 到 inplace 路径
    # 兼容奇怪情况（current symlink 失效）。
    local script_path=""
    if [ "${state}" = "release" ] && [ -L "${install_dir}/current" ] \
            && [ -f "${install_dir}/current/scripts/install.sh" ]; then
        script_path="${install_dir}/current/scripts/install.sh"
    elif [ -f "${install_dir}/scripts/install.sh" ]; then
        script_path="${install_dir}/scripts/install.sh"
    elif [ -f "${install_dir}/current/scripts/install.sh" ]; then
        script_path="${install_dir}/current/scripts/install.sh"
    else
        printf '[ERROR] 找不到 install.sh：既不在 %s/scripts/ 也不在 %s/current/scripts/\n' \
            "${install_dir}" "${install_dir}" >&2
        exit 1
    fi

    # 优先用 /dev/tty 接管 stdin（让交互菜单能读键），没 tty 就直接 exec。
    # --auto / --update 都是非交互的，没 tty 也能跑通。
    raw_drain_bootstrap_stdin
    if [ -r /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
        exec bash "${script_path}" "${args[@]}" </dev/tty
    fi
    exec bash "${script_path}" "${args[@]}"
}

if [ ! -f "${SCRIPT_DIR}/lib.sh" ]; then
    bootstrap_from_raw_script "$@"
fi

# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
OS="$(detect_os)"

# Full-repository install modules. Raw curl|bash bootstrap above must stay
# self-contained because these files do not exist until the repository lands.
INSTALL_MODULE_DIR="${SCRIPT_DIR}/install"
for install_module in \
        state.sh environment.sh runtime.sh prerequisites.sh layout.sh services.sh operations.sh; do
    if [ ! -f "${INSTALL_MODULE_DIR}/${install_module}" ]; then
        log_error "缺少安装模块：${INSTALL_MODULE_DIR}/${install_module}"
        exit 1
    fi
    # shellcheck source=/dev/null
    . "${INSTALL_MODULE_DIR}/${install_module}"
done
unset install_module

# ---------------------------------------------------------------------------
# 入口：菜单 / auto / install / update / uninstall 分发
# 这一段保持向后兼容，逻辑没变。docker 化只影响 install 主流程。
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
Lumen 安装入口（Docker Compose 全栈版）

用法：
  bash scripts/install.sh                    打开运维菜单
  bash scripts/install.sh --auto             自动：有部署走 update，新机器走 install
  bash scripts/install.sh --install [opts]   直接安装 Lumen（docker compose）
  bash scripts/install.sh --update           更新 Lumen
  bash scripts/install.sh --uninstall        卸载 Lumen

--install 可选参数：
  --image-tag=vX.Y.Z      钉死镜像 tag（默认探测 GHCR latest，不自动回退 main）
  --data-root=/path       LUMEN_DATA_ROOT 文件/备份根目录（默认 /opt/lumendata）
  --db-root=/path         LUMEN_DB_ROOT 数据库根目录（默认跟随 LUMEN_DATA_ROOT）
  --build                 用本地 Dockerfile 构建而不是 pull GHCR（等价 LUMEN_INSTALL_BUILD=1）

环境变量：
  LUMEN_DEPLOY_ROOT       部署根目录（默认 /opt/lumen 或脚本所在父目录）
  LUMEN_NONINTERACTIVE=1  非交互模式：从 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD 读管理员
  LUMEN_IMAGE_REGISTRY    镜像 registry 前缀（默认 ghcr.io/cyeinfpro）
  LUMEN_INSTALL_BUILD=1   等价 --build

EOF
}

# --auto：根据当前机器状态自动选 update / install。
#   release 布局或 in-place 部署或已有 systemd active → update（无人值守）
#   否则                                              → fresh install（如有 tty 进交互菜单）
dispatch_auto() {
    local has_release=0 has_inplace=0 has_systemd=0
    [ -L "${ROOT}/current" ] && has_release=1
    [ -d "${ROOT}/apps/api" ] && has_inplace=1
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet lumen-api.service 2>/dev/null \
           || systemctl is-active --quiet lumen-worker.service 2>/dev/null \
           || systemctl is-active --quiet lumen-web.service 2>/dev/null; then
            has_systemd=1
        fi
    fi
    if [ "${has_release}" = "1" ] || [ "${has_inplace}" = "1" ] || [ "${has_systemd}" = "1" ]; then
        log_info "[auto] 检测到已有 Lumen 部署 (release=${has_release} inplace=${has_inplace} systemd=${has_systemd})，转入 update 流程。"
        exec bash "${SCRIPT_DIR}/update.sh"
    fi
    log_info "[auto] 未检测到已有部署，进入全新安装流程。"
    if [ ! -r /dev/tty ] && [ -t 0 ]; then
        : # 有交互输入
    elif [ ! -r /dev/tty ] && [ "${LUMEN_NONINTERACTIVE:-}" != "1" ]; then
        log_warn "[auto] 当前没有 tty，全新安装会卡在交互输入。"
        log_warn "[auto] 请改用：LUMEN_NONINTERACTIVE=1 bash ${SCRIPT_DIR}/install.sh --install   或在 SSH 终端里重跑。"
        exit 2
    fi
    # fall through 到 install path
}

# 解析 --image-tag / --data-root / --db-root / --build；其它参数报错。
# 调用方：dispatch_entrypoint 在收到 install/--install 后调用本函数。
INSTALL_IMAGE_TAG_OVERRIDE=""
INSTALL_DATA_ROOT_OVERRIDE=""
INSTALL_DB_ROOT_OVERRIDE=""
INSTALL_BUILD_FLAG="${LUMEN_INSTALL_BUILD:-0}"

parse_install_args() {
    local arg
    for arg in "$@"; do
        case "${arg}" in
            --image-tag=*) INSTALL_IMAGE_TAG_OVERRIDE="${arg#*=}" ;;
            --data-root=*) INSTALL_DATA_ROOT_OVERRIDE="${arg#*=}" ;;
            --db-root=*)   INSTALL_DB_ROOT_OVERRIDE="${arg#*=}" ;;
            --build)       INSTALL_BUILD_FLAG=1 ;;
            *)
                usage
                log_error "未知 install 参数：${arg}"
                exit 1
                ;;
        esac
    done
}

dispatch_entrypoint() {
    local command="${1:-menu}"
    case "${command}" in
        menu|--menu)
            exec bash "${SCRIPT_DIR}/lumenctl.sh" menu
            ;;
        auto|--auto)
            shift || true
            dispatch_auto
            # dispatch_auto 没退出说明要走 install path
            ;;
        install|--install)
            shift || true
            parse_install_args "$@"
            ;;
        update|--update)
            exec bash "${SCRIPT_DIR}/update.sh"
            ;;
        uninstall|--uninstall)
            exec bash "${SCRIPT_DIR}/uninstall.sh"
            ;;
        repair|--repair|repair-compose-project|--repair-compose-project)
            # self-heal: 把跑在非 lumen project 的 lumen-* 容器迁回 project=lumen
            # idempotent — 没冲突就秒退。详细文档见 scripts/lib.sh 的
            # lumen_compose_project_unify 注释。
            if ! command -v lumen_compose_project_unify >/dev/null 2>&1; then
                log_error "lib.sh 未提供 lumen_compose_project_unify；请确认 install.sh 与 lib.sh 同版本。"
                exit 1
            fi
            log_step "[repair] 检查并修复 lumen-* 容器 compose project 名漂移"
            lumen_compose_project_unify
            local _root="${LUMEN_DEPLOY_ROOT:-/opt/lumen}/current"
            if [ ! -f "${_root}/docker-compose.yml" ]; then
                log_error "未找到 ${_root}/docker-compose.yml；无法重新启动 stack。"
                exit 1
            fi
            log_step "[repair] 重新启动 stack 到 project=${LUMEN_COMPOSE_PROJECT:-lumen}"
            if ! lumen_compose_in "${_root}" up --pull missing -d --wait --force-recreate; then
                log_error "[repair] docker compose up 失败；请检查 docker / compose 状态。"
                exit 1
            fi
            log_info "[repair] 完成。当前 stack:"
            lumen_compose_in "${_root}" ps
            exit 0
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

# ---------------------------------------------------------------------------
# 失败处理 / 锁
# 锁机制：使用 lib.sh 的 lumen_acquire_lock（${ROOT}/.lumen-maintenance.lock），
# 与 update.sh / uninstall.sh 互斥。lumen_release_lock 由 EXIT trap 自动调用。
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
INSTALL_PHASE_START_TS=""

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

# 全局维护锁：与 update.sh / uninstall.sh 互斥（共用 ${ROOT}/.lumen-maintenance.lock）。
lumen_acquire_lock "${ROOT}" "install.sh"

# 锁拿到后再装 cleanup_on_failure，覆盖 lumen_acquire_lock 内层的 release_lock
# trap。cleanup 内 chain 调 release_lock 幂等。
trap cleanup_on_failure EXIT

# 解析最终的部署目录与数据目录（命令行 / 环境变量 / 默认值优先级）
DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
# 当前脚本若是从 /opt/lumen/* 内部运行，优先尊重它的根目录
case "${ROOT}" in
    "${DEPLOY_ROOT}"|"${DEPLOY_ROOT}"/*)
        # ROOT 在 deploy_root 下：保留 deploy_root 不变
        ;;
    *)
        # 否则：如果用户没显式设置 LUMEN_DEPLOY_ROOT，回退到 ROOT（开发模式 / 本地仓库）
        if [ -z "${LUMEN_DEPLOY_ROOT:-}" ]; then
            DEPLOY_ROOT="${ROOT}"
        fi
        ;;
esac

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

check_prerequisites
prepare_data_dirs
prepare_release_layout
if ! snapshot_install_state; then
    log_error "无法创建安装事务快照，拒绝继续。"
    exit 1
fi
prepare_env_file
probe_ghcr_image_tag
pull_or_build_images
start_infrastructure
run_migration
run_bootstrap_admin
start_application_services
switch_current_symlink
install_update_runner_units
install_storage_control_plane
run_health_checks
warn_about_legacy_systemd
print_summary

trap - ERR EXIT
discard_install_state_snapshot
lumen_release_lock 2>/dev/null || true
exit 0
