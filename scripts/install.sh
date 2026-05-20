#!/usr/bin/env bash
# Lumen 一键安装脚本（Docker Compose 全栈版）
# 用法：  bash scripts/install.sh                  # 打开运维菜单
#        bash scripts/install.sh --install         # 直接安装（docker compose 全栈）
#        bash scripts/install.sh --install --build # 用本地 Dockerfile 构建而不是 pull 远程镜像
#        bash scripts/install.sh --install --image-tag=vX.Y.Z   # 钉死镜像 tag
#        bash scripts/install.sh --install --data-root=/data    # 自定义 LUMEN_DATA_ROOT
#        bash scripts/install.sh --install --db-root=/var/lib/lumen-data # 自定义 PG/Redis 根
#
# 行为概述：
#   A. 检查 docker / docker compose v2 / openssl / curl
#   B. 准备数据目录（PG/Redis 可通过 LUMEN_DB_ROOT 与 storage/backup 分离）
#   C. 准备 release 布局（${LUMEN_DEPLOY_ROOT:-/opt/lumen}/{releases,shared,current}）
#   D. 生成或合并 shared/.env（强随机替换 placeholder；symlink release/.env -> shared/.env）
#   E. 探测 GHCR 镜像可用性，未发布 latest 时回退到 main
#   F. docker compose pull && 起 PG/Redis -> migrate -> 可选 bootstrap -> api/worker/web (+tgbot)
#   G. 切 current symlink
#   H. HTTP + compose 健康检查
#   I. 安装/刷新一键更新 systemd runner（Linux systemd）
#   J. systemd 旧服务残留提示（不自动 disable）
#   K. 打印汇总
#
# 重复执行安全（幂等）。失败时清理已起容器（不删数据卷），打印恢复命令。
# 兼容 LUMEN_NONINTERACTIVE=1：所有 read 跳过，从 LUMEN_ADMIN_EMAIL/LUMEN_ADMIN_PASSWORD env 读。

set -euo pipefail

# `curl | bash` 远程模式下 BASH_SOURCE 是空数组，set -u 会让访问 [0] 报
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
                # 把 scripts/ 覆盖到 current 指向的 release 内
                overlay_repo_into_existing "${repo_url}" "${branch}" "${install_dir}/current"
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
    if [ -r /dev/tty ]; then
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
  --image-tag=vX.Y.Z      钉死镜像 tag（默认探测 GHCR latest，找不到回退 main）
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
INSTALL_SWITCHED=0             # current symlink 是否已切到本次 RELEASE_DIR
INSTALL_TGBOT_STATUS=""        # started / failed / skipped；print_summary 汇报
INSTALL_PREV_CURRENT_TARGET="" # switch 前 current 指向的相对路径（失败回滚用）

on_error() {
    local line="$1"
    log_error "安装失败：第 ${line} 行返回非零状态（阶段=${INSTALL_PHASE:-unknown}）。"
}

# 失败清理：停止已启动的容器、回滚 current symlink、删除半完成的 release。
# 数据卷与 shared/.env 永远保留，让用户重跑 install 时复用。
cleanup_on_failure() {
    local rc=$?
    trap - EXIT INT TERM ERR
    if [ "${rc}" -ne 0 ]; then
        log_error "安装在阶段 [${INSTALL_PHASE:-unknown}] 失败，正在清理已启动的容器（数据卷与 shared/.env 保留）。"
        if [ "${#INSTALL_STARTED_SERVICES[@]}" -gt 0 ]; then
            local svc
            for svc in "${INSTALL_STARTED_SERVICES[@]}"; do
                log_warn "  最近 40 行 ${svc} 日志："
                _install_compose logs --tail=40 "${svc}" 2>/dev/null || log_warn "    （取日志失败，已忽略）"
            done
            log_warn "停止已启动的服务（数据卷保留）：${INSTALL_STARTED_SERVICES[*]}"
            if ! _install_compose stop "${INSTALL_STARTED_SERVICES[@]}" 2>/dev/null; then
                log_warn "  docker compose stop 返回非零（已忽略，请手动 docker compose ps 检查）"
            fi
        fi

        # 如果 current 已经被切到本次 RELEASE_DIR，但后续阶段失败，则切回 previous（如有）。
        # DEPLOY_ROOT 在主流程里赋值，可能在 lumen_acquire_lock 失败时还未定义；用 :- 防御。
        local _deploy_root="${DEPLOY_ROOT:-}"
        if [ -n "${_deploy_root}" ] \
                && [ "${INSTALL_SWITCHED}" = "1" ] \
                && [ -n "${INSTALL_PREV_CURRENT_TARGET:-}" ] \
                && [ -d "${_deploy_root}/${INSTALL_PREV_CURRENT_TARGET}" ]; then
            log_warn "回滚 current symlink → ${INSTALL_PREV_CURRENT_TARGET}（${INSTALL_PHASE} 之后失败）"
            if ! lumen_atomic_replace_symlink "${INSTALL_PREV_CURRENT_TARGET}" "${_deploy_root}/current" 2>/dev/null; then
                log_error "  current 回滚失败！请手动：ln -sfn ${INSTALL_PREV_CURRENT_TARGET} ${_deploy_root}/current"
            fi
        fi

        # 半完成的 release 目录：rsync 已落地但 current 从未切到它（或已切回 previous），删除。
        if [ -n "${RELEASE_DIR:-}" ] && [ -d "${RELEASE_DIR}" ]; then
            local cur_target=""
            if [ -n "${_deploy_root}" ] && [ -L "${_deploy_root}/current" ]; then
                cur_target="$(readlink "${_deploy_root}/current" 2>/dev/null || true)"
            fi
            if [ "${cur_target}" != "releases/${RELEASE_ID:-}" ]; then
                log_warn "清理半完成的 release：${RELEASE_DIR}"
                if ! lumen_safe_rm_rf "${RELEASE_DIR}" 2>/dev/null; then
                    if ! lumen_safe_rm_rf_as_root "${RELEASE_DIR}" 2>/dev/null; then
                        log_warn "  release 删除失败，请手动：sudo rm -rf '${RELEASE_DIR}'"
                    fi
                fi
            fi
        fi

        # 只在新流程触发的 step protocol 上下文里写 fail；emit_step 函数在 lib.sh
        if command -v lumen_emit_step >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
            lumen_emit_step "phase=${INSTALL_PHASE}" "status=fail" "rc=${rc}" "dur_ms=0" 2>/dev/null \
                || log_warn "lumen_emit_step 写入失败（已忽略）"
        fi
        log_error ""
        log_error "可恢复命令："
        log_error "  cd ${_deploy_root:-${ROOT}}/current 2>/dev/null || cd ${ROOT}"
        log_error "  COMPOSE_PROJECT_NAME=lumen docker compose ps"
        log_error "  COMPOSE_PROJECT_NAME=lumen docker compose logs --tail=200 api worker web"
        log_error "  bash ${SCRIPT_DIR}/install.sh --install   # 修复后重跑（幂等）"
    fi
    # lumen_release_lock 由 lumen_acquire_lock 安装的 EXIT trap 处理；这里手动也调一次幂等
    if command -v lumen_release_lock >/dev/null 2>&1; then
        lumen_release_lock 2>/dev/null || true
    fi
    return "${rc}"
}

on_signal() {
    local signal_name="$1"
    local rc="$2"
    log_error "安装被 ${signal_name} 中断（rc=${rc}），将走完整失败清理流程。"
    # exit 触发 EXIT trap (cleanup_on_failure)：清理已起容器、回滚 current
    # symlink、删半成品 release，最后释放锁。比裸 exit 更彻底。
    exit "${rc}"
}

# ---------------------------------------------------------------------------
# .env 写入辅助（保留旧行为：拒绝控制字符 / 单引号）
# ---------------------------------------------------------------------------
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

# 在 .env 文件里精确替换 KEY=value 行（避免全局 sed 误伤 §21.1）。
# 用法：env_file_set <file> <key> <value>
# 注意：value 不允许包含换行 / 单引号；用 dotenv_quote 校验。
# 在目标文件同 fs 下 mktemp，确保 mv 是 POSIX 原子 rename。默认 mktemp 在
# /tmp，与 /opt/lumen/shared/ 跨 fs 时退化为 copy+unlink，断电瞬间存在空文件窗口。
env_file_set() {
    local file="$1"
    local key="$2"
    local value="$3"
    validate_dotenv_value "${key}" "${value}" || return 1
    local tmp dir
    dir="$(dirname "${file}")"
    tmp="$(mktemp "${dir}/.lumen-env.XXXXXX" 2>/dev/null)" || tmp="$(mktemp)" || return 1
    # awk 行级精确替换：只动 ^KEY= 开头的行；其它原样保留。
    awk -v k="${key}" -v v="${value}" '
        BEGIN { replaced=0 }
        {
            if ($0 ~ "^" k "=") {
                printf "%s=%s\n", k, v
                replaced=1
            } else {
                print
            }
        }
        END {
            if (!replaced) {
                printf "%s=%s\n", k, v
            }
        }
    ' "${file}" > "${tmp}" && mv "${tmp}" "${file}"
}

# 读取 .env 中某 key 的当前值（沿用 lib.sh 实现）
env_file_get() {
    lumen_read_dotenv_value "$1" "$2"
}

generate_hex_secret() {
    local bytes="${1:-32}"
    openssl rand -hex "${bytes}"
}

postgres_data_initialized() {
    local db_root="${LUMEN_DB_ROOT:-/opt/lumendata}"
    local postgres_dir="${db_root}/postgres"

    if [ -f "${postgres_dir}/PG_VERSION" ] || [ -f "${postgres_dir}/global/pg_control" ]; then
        return 0
    fi
    if command -v lumen_run_as_root >/dev/null 2>&1; then
        if lumen_run_as_root test -f "${postgres_dir}/PG_VERSION" 2>/dev/null \
                || lumen_run_as_root test -f "${postgres_dir}/global/pg_control" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

ensure_env_secret() {
    local file="$1"
    local key="$2"
    local bytes="${3:-32}"
    local value
    value="$(env_file_get "${key}" "${file}")"
    if [ -n "${value}" ]; then
        return 0
    fi
    if [ "${key}" = "BYOK_API_KEY_MASTER_SECRET" ] && [ "${LUMEN_ALLOW_BYOK_KEY_GEN:-0}" != "1" ]; then
        if postgres_data_initialized; then
            log_error "BYOK_API_KEY_MASTER_SECRET 缺失，且数据库可能已有 BYOK 密文。"
            log_error "  - 新部署：export LUMEN_ALLOW_BYOK_KEY_GEN=1 再重跑安装。"
            log_error "  - 升级：从备份恢复原始 BYOK_API_KEY_MASTER_SECRET，不要让脚本随机生成。"
            return 1
        fi
        log_warn "BYOK_API_KEY_MASTER_SECRET 缺失，但 Postgres 尚未初始化；按新部署/失败重跑自动生成。"
    fi
    value="$(generate_hex_secret "${bytes}")"
    if [ "${key}" = "REDIS_PASSWORD" ]; then
        validate_redis_password "${value}" || return 1
    else
        validate_dotenv_value "${key}" "${value}" || return 1
    fi
    env_file_set "${file}" "${key}" "${value}" || return 1
    return 2
}

ensure_required_env_secrets() {
    local file="$1"
    local generated=()
    local db_url redis_url redis_from_url

    db_url="$(env_file_get DATABASE_URL "${file}")"
    if [ -z "$(env_file_get DB_PASSWORD "${file}")" ] && [ -n "${db_url}" ]; then
        lumen_ensure_compose_db_env_vars "${file}" || return 1
    fi

    redis_url="$(env_file_get REDIS_URL "${file}")"
    if [ -z "$(env_file_get REDIS_PASSWORD "${file}")" ] && [ -n "${redis_url}" ]; then
        redis_from_url="$(lumen_redis_password_from_url "${redis_url}" 2>/dev/null || true)"
        if [ -n "${redis_from_url}" ]; then
            validate_redis_password "${redis_from_url}" || return 1
            env_file_set "${file}" REDIS_PASSWORD "${redis_from_url}" || return 1
        fi
    fi

    ensure_env_secret "${file}" DB_PASSWORD 32 || case "$?" in
        2) generated+=("DB_PASSWORD") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" REDIS_PASSWORD 32 || case "$?" in
        2) generated+=("REDIS_PASSWORD") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" SESSION_SECRET 64 || case "$?" in
        2) generated+=("SESSION_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" BYOK_API_KEY_MASTER_SECRET 48 || case "$?" in
        2) generated+=("BYOK_API_KEY_MASTER_SECRET") ;;
        *) return 1 ;;
    esac
    ensure_env_secret "${file}" TELEGRAM_BOT_SHARED_SECRET 32 || case "$?" in
        2) generated+=("TELEGRAM_BOT_SHARED_SECRET") ;;
        *) return 1 ;;
    esac

    local db_user db_name db_password redis_password
    db_user="$(env_file_get DB_USER "${file}")"
    db_name="$(env_file_get DB_NAME "${file}")"
    db_password="$(env_file_get DB_PASSWORD "${file}")"
    redis_password="$(env_file_get REDIS_PASSWORD "${file}")"
    db_user="${db_user:-lumen_app}"
    db_name="${db_name:-lumen_app}"
    if [ -z "$(env_file_get DATABASE_URL "${file}")" ]; then
        env_file_set "${file}" DATABASE_URL \
            "postgresql+asyncpg://${db_user}:${db_password}@postgres:5432/${db_name}" || return 1
    fi
    if [ -z "$(env_file_get REDIS_URL "${file}")" ]; then
        env_file_set "${file}" REDIS_URL \
            "redis://:${redis_password}@redis:6379/0" || return 1
    fi

    if [ "${#generated[@]}" -gt 0 ]; then
        log_info "已补齐随机密钥：${generated[*]}。"
    fi
}

# ---------------------------------------------------------------------------
# Compose 调用 wrapper
# 优先使用 lib.sh 提供的 lumen_compose；缺失时降级到 docker compose 直调。
# lumen_compose_in 的签名固定为 <dir> 后接 docker compose 参数，并自动设置
# COMPOSE_PROJECT_NAME=lumen。
# ---------------------------------------------------------------------------
_install_compose() {
    if command -v lumen_compose_in >/dev/null 2>&1 && [ -n "${RELEASE_DIR:-}" ]; then
        lumen_compose_in "${RELEASE_DIR}" "$@"
    elif command -v lumen_compose >/dev/null 2>&1; then
        lumen_compose "$@"
    else
        # Fallback：手动设置 COMPOSE_PROJECT_NAME=lumen，cd 到 RELEASE_DIR
        local cwd_dir="${RELEASE_DIR:-${ROOT}}"
        ( cd "${cwd_dir}" && COMPOSE_PROJECT_NAME=lumen docker compose "$@" )
    fi
}

# 按镜像分组拉取的薄 wrapper，调 lib.sh:lumen_compose_pull_per_image。
# 保留 _install_compose_pull_per_image 名字向后兼容（pull_or_build_images
# 已在用），实际工作由 lib.sh 同款函数处理；update.sh 也走 lib.sh 一份。
_install_compose_pull_per_image() {
    lumen_compose_pull_per_image "${RELEASE_DIR:-${ROOT}}"
}

# 健康检查 wrapper
_install_health_http() {
    local url="$1"
    local timeout_s="${2:-60}"
    local interval_s="${3:-2}"
    if command -v lumen_health_http >/dev/null 2>&1; then
        lumen_health_http "${url}" "${timeout_s}" "${interval_s}"
    else
        # Fallback：用 lib.sh 已有的 lumen_wait_for_http_ok（attempts=timeout_s）
        lumen_wait_for_http_ok "${url}" "${timeout_s}"
    fi
}

_install_health_compose() {
    if command -v lumen_health_compose >/dev/null 2>&1; then
        lumen_health_compose "$@"
        return $?
    fi
    # Fallback：自己 inspect Container.State.Health.Status
    local svc cid status
    for svc in "$@"; do
        cid="$(_install_compose ps -q "${svc}" 2>/dev/null | head -n1 || true)"
        if [ -z "${cid}" ]; then
            log_error "compose service ${svc} 未运行，无法做健康检查。"
            return 1
        fi
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${cid}" 2>/dev/null || true)"
        case "${status}" in
            healthy|running) ;;
            *)
                log_error "compose service ${svc} 状态异常：${status}"
                return 1
                ;;
        esac
    done
}

# 阶段记录 wrapper
# 记录每个 phase 的 wall-clock 起止时间，emit_step_done 时打印耗时摘要给
# 终端用户（lumen_emit_step 的 dur_ms 仅写入 SSE 协议，终端看不到）。
# 用单变量而非 declare -A 关联数组，兼容 macOS bash 3.2（CI smoke runner）。
INSTALL_PHASE_START_TS=""

_now_seconds() {
    # 高精度 wall-clock；macOS date 不支持 +%s.%N，用 perl 兜底，再不行用秒精度。
    if date +%s.%N >/dev/null 2>&1 && [ "$(date +%N)" != "N" ]; then
        date +%s.%N
    elif command -v perl >/dev/null 2>&1; then
        perl -MTime::HiRes=time -e 'printf "%.3f\n", time'
    else
        date +%s
    fi
}

emit_step_start() {
    INSTALL_PHASE="$1"
    INSTALL_PHASE_START_TS="$(_now_seconds)"
    log_step "[${INSTALL_PHASE}] $2"
    if command -v lumen_emit_step >/dev/null 2>&1; then
        lumen_emit_step "phase=${INSTALL_PHASE}" "status=start" || true
    fi
}

emit_step_done() {
    local dur=""
    if [ -n "${INSTALL_PHASE_START_TS}" ] && [ -n "${INSTALL_PHASE:-}" ]; then
        local end_ts
        end_ts="$(_now_seconds)"
        # awk 处理浮点；不依赖 bc。失败时 dur 留空，不打耗时。
        dur="$(awk -v s="${INSTALL_PHASE_START_TS}" -v e="${end_ts}" \
            'BEGIN { d = e - s; if (d < 0) d = 0; printf "%.1f", d }' 2>/dev/null || true)"
        if [ -n "${dur}" ]; then
            log_info "  ✓ ${INSTALL_PHASE} 完成（耗时 ${dur}s）"
        fi
    fi
    if command -v lumen_emit_step >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
        lumen_emit_step "phase=${INSTALL_PHASE}" "status=done" "rc=0" \
            ${dur:+dur_ms=$(awk -v d="${dur}" 'BEGIN { printf "%d", d * 1000 }')} || true
    fi
    INSTALL_PHASE=""
    INSTALL_PHASE_START_TS=""
}

emit_info() {
    if command -v lumen_emit_info >/dev/null 2>&1 && [ -n "${INSTALL_PHASE:-}" ]; then
        lumen_emit_info "phase=${INSTALL_PHASE}" "$@" || true
    fi
}

# 是否在 systemd 下运行（PID 1 是 systemd）。容器/WSL1/Alpine OpenRC/某些精简系统
# 会返回 1，调用者据此决定是否跳过 systemctl 调用。
_has_systemd() {
    raw_have_cmd systemctl || return 1
    [ -d /run/systemd/system ] || return 1
    return 0
}

# 等 docker daemon 起来。systemctl enable --now 后 daemon 启动需要几秒；
# 当前用户可能没在 docker 组，先尝试直接 docker info，再用 sudo -n 探测。
_wait_docker_daemon_ready() {
    local timeout=60 i=0
    log_info "等待 Docker daemon 就绪（最多 ${timeout}s）..."
    while [ "${i}" -lt "${timeout}" ]; do
        if docker info >/dev/null 2>&1; then
            log_info "Docker daemon 已就绪。"
            return 0
        fi
        if raw_have_cmd sudo && sudo -n docker info >/dev/null 2>&1; then
            log_info "Docker daemon 已就绪（通过 sudo 访问）。"
            return 0
        fi
        sleep 2
        i=$((i + 2))
    done
    return 1
}

# Linux 的 apt 路径单独处理：apt-get update 失败仅 warn 不致命（cache 可能足以
# 装老版本包；某个 PPA 烂不应阻塞核心 install）。其他发行版直接走原 raw_install_packages。
_install_packages_linux_resilient() {
    local pkgs=("$@")
    [ "${#pkgs[@]}" -gt 0 ] || return 0
    if raw_have_cmd apt-get; then
        local update_ok=0 i
        for i in 1 2; do
            if raw_run_as_root env DEBIAN_FRONTEND=noninteractive apt-get update; then
                update_ok=1
                break
            fi
            if [ "${i}" -lt 2 ]; then
                log_warn "apt-get update 第 ${i} 次失败，5s 后重试。"
                sleep 5
            fi
        done
        if [ "${update_ok}" -eq 0 ]; then
            log_warn "apt-get update 反复失败；继续尝试 install（用现有 cache）。"
        fi
        raw_run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}" || return 1
        return 0
    fi
    raw_install_packages "${pkgs[@]}" || return 1
    return 0
}

# 检测 root 权限可用性：脚本要做 raw_run_as_root，如果当前不是 root 又没有 sudo
# 那么后续包安装一定 fail，提前 fail-fast 给清晰提示。
_ensure_root_or_sudo() {
    if [ "${EUID:-$(id -u 2>/dev/null || echo 1)}" -eq 0 ]; then
        return 0
    fi
    if raw_have_cmd sudo; then
        return 0
    fi
    log_error "当前用户不是 root，且未发现 sudo；无法安装系统包。请用 root 用户重跑或先装 sudo。"
    return 1
}

# 自动安装 openssl / curl 这类轻量基础包。Linux 走 _install_packages_linux_resilient
# （apt update 容错 + apt/dnf/yum/pacman/zypper/apk fallback），macOS 走 brew。
# 任一步失败返回 1，由调用者打印手动安装提示并退出。
_auto_install_basics() {
    local pkgs=("$@")
    [ "${#pkgs[@]}" -gt 0 ] || return 0
    log_info "尝试自动安装基础依赖：${pkgs[*]}"
    case "${OS}" in
        linux)
            _ensure_root_or_sudo || return 1
            _install_packages_linux_resilient ca-certificates "${pkgs[@]}" || return 1
            ;;
        macos)
            if ! raw_have_cmd brew; then
                log_warn "macOS 未发现 Homebrew，无法自动安装 ${pkgs[*]}。请先按 https://brew.sh 装 brew 再重跑。"
                return 1
            fi
            brew install "${pkgs[@]}" || return 1
            ;;
        *)
            return 1
            ;;
    esac
    raw_refresh_tool_paths
    return 0
}

# 自动安装 Docker：Linux 走官方 https://get.docker.com 一键脚本。
# 鲁棒性增强：先下载到 tmp 文件再执行（避免 curl|sh 半途断流）、加 timeout/retry、
# 把脚本输出 tee 到日志文件供排错、装完显式等 daemon ready。
# macOS 上不支持自动装 Docker Desktop（GUI），需用户手动安装。
_auto_install_docker() {
    case "${OS}" in
        linux)
            _ensure_root_or_sudo || return 1
            if ! raw_have_cmd curl; then
                log_error "缺 curl，无法下载 Docker 安装脚本；请先安装 curl 后重跑。"
                return 1
            fi

            local installer log_file rc
            installer="$(mktemp -t lumen-get-docker.XXXXXX 2>/dev/null || mktemp /tmp/lumen-get-docker.XXXXXX)"
            log_file="/tmp/lumen-docker-install.log"
            : > "${log_file}" 2>/dev/null || true
            # 关闭 ERR trap & errexit 临时，避免 trap 噪音；本函数自己管 rc。
            log_info "下载 Docker 安装脚本（https://get.docker.com → ${installer}）"
            if ! curl -fsSL \
                    --connect-timeout 30 \
                    --max-time 300 \
                    --retry 3 \
                    --retry-delay 5 \
                    --retry-connrefused \
                    -o "${installer}" \
                    "https://get.docker.com"; then
                log_error "下载 https://get.docker.com 失败（网络/代理/防火墙）；请检查后重跑或手动安装。"
                rm -f "${installer}" 2>/dev/null || true
                return 1
            fi
            if [ ! -s "${installer}" ]; then
                log_error "下载到的 Docker 安装脚本为空；请重试或手动安装。"
                rm -f "${installer}" 2>/dev/null || true
                return 1
            fi

            log_info "执行 Docker 安装脚本（详细输出 → ${log_file}，可能需要 1~3 分钟）"
            # tee 到日志文件 + 标准输出。PIPESTATUS[0] 是 sh 的 rc。
            set +e
            if [ "${EUID:-$(id -u)}" -eq 0 ]; then
                sh "${installer}" 2>&1 | tee "${log_file}"
                rc="${PIPESTATUS[0]}"
            else
                sudo sh "${installer}" 2>&1 | tee "${log_file}"
                rc="${PIPESTATUS[0]}"
            fi
            set -e
            rm -f "${installer}" 2>/dev/null || true

            if [ "${rc}" -ne 0 ]; then
                log_error "Docker 安装脚本失败（rc=${rc}）；详细日志：${log_file}"
                log_error "  常见原因：网络超时、apt/dnf 仓库被封、内核太旧、SELinux/AppArmor 拦截。"
                log_error "  排错：tail -n 80 ${log_file}"
                return 1
            fi
            raw_refresh_tool_paths

            # 启动 docker daemon：systemd 走 systemctl；OpenRC/其他走 service；都没有就靠 dockerd 自启。
            if _has_systemd; then
                if ! raw_run_as_root systemctl enable --now docker; then
                    log_warn "systemctl enable --now docker 失败；尝试 systemctl start docker。"
                    raw_run_as_root systemctl start docker || \
                        log_warn "systemctl start docker 也失败；下面的 daemon 等待会兜底报错。"
                fi
            elif raw_have_cmd service; then
                raw_run_as_root service docker start || \
                    log_warn "service docker start 失败；下面的 daemon 等待会兜底报错。"
            else
                log_warn "未检测到 systemd 或 service 命令（容器/WSL1/Alpine？）；如 daemon 未起请手动 dockerd & 或重启 shell。"
            fi

            # 把当前用户加入 docker 组（root 直接跳过）。
            local target_user
            target_user="${SUDO_USER:-${USER:-}}"
            [ -n "${target_user}" ] || target_user="$(id -un 2>/dev/null || echo)"
            if [ -n "${target_user}" ] && [ "${target_user}" != "root" ]; then
                if ! getent group docker >/dev/null 2>&1; then
                    log_warn "docker 组未发现（dockerd 安装异常？）；docker 命令将通过 sudo 调用。"
                elif id -nG "${target_user}" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
                    log_info "${target_user} 已在 docker 组中。"
                elif raw_run_as_root usermod -aG docker "${target_user}"; then
                    log_warn "已把 ${target_user} 加入 docker 组；当前 shell 仍未拿到组权限，docker 命令由 sudo 兜底，重登录后免 sudo。"
                else
                    log_warn "把 ${target_user} 加入 docker 组失败；docker 命令将通过 sudo 调用。"
                fi
            fi

            # 等 daemon 起来；起不来就 fail-fast。
            if ! _wait_docker_daemon_ready; then
                log_error "Docker 装好但 daemon 60s 内未就绪；请检查 systemctl status docker / journalctl -u docker。"
                return 1
            fi
            return 0
            ;;
        macos)
            log_error "macOS 上 Docker 需手动安装 Docker Desktop（https://www.docker.com/products/docker-desktop）；脚本无法自动安装 GUI 应用。"
            return 1
            ;;
        *)
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# A. 前置检查
# 必装：docker / docker compose v2 / openssl / curl
# 可选：python3（仅 backup 脚本用），systemd（仅 update-runner 路径用）
# 磁盘：/opt 至少 10GB
# ---------------------------------------------------------------------------
check_prerequisites() {
    emit_step_start prepare "前置检查（docker / compose v2 / openssl / curl）"
    case "${OS}" in
        macos|linux) ;;
        *)
            log_error "暂不支持当前操作系统（uname -s = $(uname -s)）。仅支持 macOS 与 Linux（含 WSL2）。"
            exit 1
            ;;
    esac

    # 1) openssl / curl / rsync 缺则自动装（轻量、安全副作用低）。
    # rsync 在 prepare_release_layout 阶段必需；提前装避免后面才 fail-fast。
    local basics_missing=()
    command -v openssl >/dev/null 2>&1 || basics_missing+=("openssl")
    command -v curl    >/dev/null 2>&1 || basics_missing+=("curl")
    command -v rsync   >/dev/null 2>&1 || basics_missing+=("rsync")
    if [ "${#basics_missing[@]}" -gt 0 ]; then
        if ! _auto_install_basics "${basics_missing[@]}"; then
            log_error "缺少必备命令：${basics_missing[*]}（自动安装失败）"
            log_error "  请通过系统包管理器手动安装（apt/dnf/brew）后重跑。"
            exit 1
        fi
        # 装完 re-check（PATH 可能没刷新）
        for cmd in "${basics_missing[@]}"; do
            if ! command -v "${cmd}" >/dev/null 2>&1; then
                log_error "${cmd} 自动安装后仍未在 PATH 中，请重新登录或手动安装后重跑。"
                exit 1
            fi
        done
    fi

    # 2) docker 缺则走官方一键脚本（Linux）；macOS 仍 fail-fast 让用户装 Desktop。
    if ! command -v docker >/dev/null 2>&1; then
        case "${OS}" in
            linux)
                # 非交互模式直接装（脚本本来就是无人值守目标）；交互模式问一次，
                # 默认 No 防生产机上误触发系统级改动。
                local do_install=0
                if [ "${LUMEN_NONINTERACTIVE:-}" = "1" ]; then
                    do_install=1
                elif confirm "未检测到 Docker，是否调用官方一键脚本（https://get.docker.com）自动安装？"; then
                    do_install=1
                fi
                if [ "${do_install}" -eq 1 ]; then
                    if ! _auto_install_docker; then
                        exit 1
                    fi
                else
                    log_error "缺少 Docker；请按 https://docs.docker.com/engine/install/ 手动安装后重跑。"
                    exit 1
                fi
                ;;
            macos)
                log_error "缺少 Docker；macOS 请安装 Docker Desktop（https://www.docker.com/products/docker-desktop）后重跑。"
                exit 1
                ;;
        esac
        if ! command -v docker >/dev/null 2>&1; then
            log_error "Docker 安装后仍未检测到 docker 命令；请检查 PATH 或重新登录后重跑。"
            exit 1
        fi
    fi

    # docker compose v2 子命令检测
    if ! docker compose version >/dev/null 2>&1; then
        log_error "未检测到 docker compose v2 子命令。请安装 docker-compose-plugin（Linux）"
        log_error "或升级 Docker Desktop（macOS）。"
        exit 1
    fi

    # docker daemon 可达 + 是否需要 sudo
    if command -v lumen_require_docker_access >/dev/null 2>&1; then
        lumen_require_docker_access
    elif ! docker info >/dev/null 2>&1; then
        log_error "Docker daemon 未运行，或当前用户无权访问 Docker。"
        log_error "  Linux：sudo systemctl start docker；将用户加入 docker 组后重新登录"
        log_error "  macOS：启动 Docker Desktop 等待初始化"
        exit 1
    fi

    # 可选：python3（备份脚本辅助）
    if ! command -v python3 >/dev/null 2>&1; then
        log_warn "未检测到 python3；备份/恢复脚本会有部分辅助功能不可用，但安装可继续。"
    fi

    # 磁盘空间：分别探测 LUMEN_DATA_ROOT (storage/backup) 与 LUMEN_DB_ROOT
    # (postgres/redis)。--data-root=/data 时 /opt 容量充足无意义，必须探数据卷。
    # 没存在的路径退化到上级 / 根分区。
    local _disk_check_paths
    _disk_check_paths="$(printf '%s\n%s\n' \
        "${LUMEN_DATA_ROOT:-/opt/lumendata}" "${LUMEN_DB_ROOT:-/opt/lumendata}" | sort -u)"
    while IFS= read -r _path; do
        [ -z "${_path}" ] && continue
        local probe="${_path}"
        # 找最近存在的祖先目录
        while [ -n "${probe}" ] && [ "${probe}" != "/" ] && [ ! -d "${probe}" ]; do
            probe="$(dirname "${probe}")"
        done
        [ -d "${probe}" ] || probe="/"
        if command -v df >/dev/null 2>&1; then
            local free_kb
            free_kb="$(df -Pk "${probe}" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
            if [ -n "${free_kb}" ] && [ "${free_kb}" -lt $((10 * 1024 * 1024)) ] 2>/dev/null; then
                log_warn "${probe} 空闲约 $((free_kb / 1024)) MB（< 10 GB 建议值；目标路径 ${_path}）"
                if [ "${LUMEN_NONINTERACTIVE:-}" != "1" ] && ! confirm "仍要继续？"; then
                    exit 0
                fi
            fi
        fi
    done <<< "${_disk_check_paths}"

    log_info "前置检查通过：docker $(docker --version 2>&1 | awk '{print $3}' | tr -d ',') / compose v2 / openssl / curl"
    emit_step_done
}

# ---------------------------------------------------------------------------
# B. 准备数据目录与权限（§15.2 + §17.0）
# LUMEN_DB_ROOT 承载 postgres/redis；LUMEN_DATA_ROOT 承载 storage/backup。
# 未显式设置 LUMEN_DB_ROOT 时保持旧行为：两者使用同一个根。
# ---------------------------------------------------------------------------
prepare_data_dirs() {
    emit_step_start prepare "准备数据目录与权限（data=${LUMEN_DATA_ROOT}, db=${LUMEN_DB_ROOT}）"
    local data_root="${LUMEN_DATA_ROOT}"
    local db_root="${LUMEN_DB_ROOT}"
    local app_uid="${LUMEN_APP_UID:-10001}"
    local app_storage_gid="${LUMEN_APP_STORAGE_GID:-${LUMEN_APP_GID:-10001}}"
    local postgres_uid="${LUMEN_POSTGRES_UID:-999}"
    local postgres_gid="${LUMEN_POSTGRES_GID:-999}"
    local redis_uid="${LUMEN_REDIS_UID:-999}"
    local redis_gid="${LUMEN_REDIS_GID:-999}"

    if [ -e "${data_root}" ] && [ ! -d "${data_root}" ]; then
        log_error "${data_root} 已存在但不是目录，请先移走或删除后重试。"
        exit 1
    fi
    if [ -e "${db_root}" ] && [ ! -d "${db_root}" ]; then
        log_error "${db_root} 已存在但不是目录，请先移走或删除后重试。"
        exit 1
    fi

    lumen_run_as_root mkdir -p "${db_root}" \
        "${db_root}/postgres" \
        "${db_root}/redis" \
        "${data_root}" \
        "${data_root}/storage" \
        "${data_root}/backup" \
        "${data_root}/backup/pg" \
        "${data_root}/backup/redis" || {
        log_error "无法创建数据目录。请确认当前用户有 sudo 权限。"
        exit 1
    }

    # 顶层 root:root 755（不递归）；CIFS/NAS 场景可能不支持，允许继续。
    lumen_run_as_root chown root:root "${data_root}" "${db_root}" \
        || log_warn "chown root:root 数据根失败（已忽略，子目录单独 chown）"
    lumen_run_as_root chmod 755 "${data_root}" "${db_root}" \
        || log_warn "chmod 755 数据根失败（已忽略）"

    # 按服务分别 chown（禁止整体 chown 给所有目录 —— §15.2）
    lumen_run_as_root chown -R "${postgres_uid}:${postgres_gid}" "${db_root}/postgres" || {
        log_error "chown postgres 数据目录失败。"
        exit 1
    }
    lumen_run_as_root chown -R "${redis_uid}:${redis_gid}" "${db_root}/redis" || {
        log_error "chown redis 数据目录失败。"
        exit 1
    }
    lumen_run_as_root chown -R "${app_uid}:${app_storage_gid}" "${data_root}/storage" "${data_root}/backup" || {
        log_error "chown storage/backup 数据目录失败。"
        exit 1
    }

    lumen_run_as_root chmod 700 "${db_root}/postgres" "${db_root}/redis" \
        || log_warn "chmod 700 postgres/redis 失败（已忽略，但容器可能因权限问题起不来）"
    lumen_run_as_root chmod 750 "${data_root}/storage" "${data_root}/backup" \
        || log_warn "chmod 750 storage/backup 失败（已忽略，但 api/worker 可能写不进去）"

    log_info "数据目录权限设置完成（postgres=${postgres_uid}:${postgres_gid}, redis=${redis_uid}:${redis_gid}；storage/backup 在 ${data_root}）。"
    emit_info "key=data_root" "value=${data_root}"
    emit_info "key=db_root" "value=${db_root}"
    emit_step_done
}

_sed_replacement_escape() {
    printf '%s' "$1" | sed 's/[\/&#]/\\&/g'
}

_render_systemd_unit_template() {
    local src="$1"
    local dst="$2"
    local data_root="$3"
    local backup_root="$4"
    local deploy_root="$5"
    local data_root_esc backup_root_esc deploy_root_esc
    data_root_esc="$(_sed_replacement_escape "${data_root}")"
    backup_root_esc="$(_sed_replacement_escape "${backup_root}")"
    deploy_root_esc="$(_sed_replacement_escape "${deploy_root}")"

    sed \
        -e 's#/opt/lumendata/backup#__LUMEN_BACKUP_ROOT__#g' \
        -e 's#/opt/lumendata#__LUMEN_DATA_ROOT__#g' \
        -e 's#/opt/lumen#__LUMEN_DEPLOY_ROOT__#g' \
        "${src}" \
        | sed \
            -e "s#__LUMEN_BACKUP_ROOT__#${backup_root_esc}#g" \
            -e "s#__LUMEN_DATA_ROOT__#${data_root_esc}#g" \
            -e "s#__LUMEN_DEPLOY_ROOT__#${deploy_root_esc}#g" \
        > "${dst}"
}

_render_update_runner_units() {
    local src_path="$1"
    local src_runner="$2"
    local out_dir="$3"
    local data_root="$4"
    local backup_root="$5"
    local deploy_root="$6"

    _render_systemd_unit_template \
        "${src_path}" \
        "${out_dir}/lumen-update.path" \
        "${data_root}" \
        "${backup_root}" \
        "${deploy_root}"
    _render_systemd_unit_template \
        "${src_runner}" \
        "${out_dir}/lumen-update-runner.service" \
        "${data_root}" \
        "${backup_root}" \
        "${deploy_root}"
    local src_dir src_warm_path src_warm_service
    src_dir="$(dirname "${src_path}")"
    src_warm_path="${src_dir}/lumen-update-warm.path"
    src_warm_service="${src_dir}/lumen-update-warm.service"
    if [ -f "${src_warm_path}" ]; then
        _render_systemd_unit_template \
            "${src_warm_path}" \
            "${out_dir}/lumen-update-warm.path" \
            "${data_root}" \
            "${backup_root}" \
            "${deploy_root}"
    fi
    if [ -f "${src_warm_service}" ]; then
        _render_systemd_unit_template \
            "${src_warm_service}" \
            "${out_dir}/lumen-update-warm.service" \
            "${data_root}" \
            "${backup_root}" \
            "${deploy_root}"
    fi
    local backup_unit
    for backup_unit in lumen-backup.service lumen-backup.timer lumen-backup.path; do
        if [ -f "${src_dir}/${backup_unit}" ]; then
            _render_systemd_unit_template \
                "${src_dir}/${backup_unit}" \
                "${out_dir}/${backup_unit}" \
                "${data_root}" \
                "${backup_root}" \
                "${deploy_root}"
        fi
    done
}

# ---------------------------------------------------------------------------
# C. 准备 release 布局
#   ${LUMEN_DEPLOY_ROOT}/
#     releases/<id>/      <- 当前 release，rsync 整个仓库进来
#     shared/.env         <- 跨 release 持久化的密钥与配置
#     current -> releases/<id>
# ---------------------------------------------------------------------------
prepare_release_layout() {
    emit_step_start prepare "准备 release 布局（${DEPLOY_ROOT}）"

    # 决定 release id：UTC 时间戳 + 可选 git short sha
    local release_id sha=""
    if [ -d "${ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
        sha="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
    fi
    if command -v lumen_release_id >/dev/null 2>&1; then
        release_id="$(lumen_release_id "${sha:-unknown}")"
    else
        release_id="$(date -u +%Y%m%dT%H%M%SZ)-${sha:-unknown}"
    fi

    RELEASE_ID="${release_id}"
    RELEASE_DIR="${DEPLOY_ROOT}/releases/${release_id}"
    SHARED_DIR="${DEPLOY_ROOT}/shared"

    # 创建顶层 + releases + shared
    lumen_run_as_root mkdir -p "${DEPLOY_ROOT}/releases" "${SHARED_DIR}" || {
        log_error "无法创建部署目录 ${DEPLOY_ROOT}。请确认 sudo 权限。"
        exit 1
    }
    # DEPLOY_ROOT 写权限给当前用户（compose 要从 RELEASE_DIR 读 docker-compose.yml）
    if [ ! -w "${DEPLOY_ROOT}" ]; then
        lumen_run_as_root chown "$(id -un):$(id -gn)" "${DEPLOY_ROOT}" "${DEPLOY_ROOT}/releases" "${SHARED_DIR}" 2>/dev/null \
            || log_warn "chown ${DEPLOY_ROOT} 失败（已忽略，rsync 可能因权限失败）"
    fi

    if [ -e "${RELEASE_DIR}" ] && [ "$(ls -A "${RELEASE_DIR}" 2>/dev/null | head -1)" ]; then
        # 加了 PID 后缀后同秒冲突理论上不会发生；非空 = 上次失败留下的半成品
        # 或两个 shell 同时跑（lumen_acquire_lock 应已挡住，但兜底）。fail-fast
        # 比 rsync 不带 --delete 留半新半旧文件、再产生诡异问题更好。
        # 紧急绕过：LUMEN_INSTALL_OVERWRITE_RELEASE=1（手动确认要覆盖）。
        if [ "${LUMEN_INSTALL_OVERWRITE_RELEASE:-0}" != "1" ]; then
            log_error "release 目录已存在且非空：${RELEASE_DIR}"
            log_error "  说明：上次 install 中途失败留下半成品，或两个 install 并发。"
            log_error "  排查：ls -la ${RELEASE_DIR}"
            log_error "  清理：sudo rm -rf '${RELEASE_DIR}' 然后重跑 install"
            log_error "  或显式覆盖：LUMEN_INSTALL_OVERWRITE_RELEASE=1 bash scripts/install.sh --install"
            exit 1
        fi
        log_warn "release 目录已存在且非空：${RELEASE_DIR}（OVERWRITE_RELEASE=1，覆盖式继续）"
    fi
    mkdir -p "${RELEASE_DIR}" 2>/dev/null || lumen_run_as_root mkdir -p "${RELEASE_DIR}"
    if [ ! -w "${RELEASE_DIR}" ]; then
        lumen_run_as_root chown -R "$(id -un):$(id -gn)" "${RELEASE_DIR}" 2>/dev/null \
            || log_warn "chown ${RELEASE_DIR} 失败（已忽略，rsync 可能因权限失败）"
    fi

    # 把当前仓库内容 rsync 到 release 目录（保留 release 布局，§11.1）
    # check_prerequisites 已经会自动装 rsync；这里保留兜底，便于直接调用本函数
    # （或老版本 install.sh 跳过 prepare 时）也能自愈。
    if ! command -v rsync >/dev/null 2>&1; then
        log_warn "缺少 rsync，尝试自动安装。"
        if ! _auto_install_basics rsync; then
            log_error "缺少 rsync 且自动安装失败；无法把仓库内容复制到 release 目录。"
            log_error "  Debian/Ubuntu：sudo apt install rsync"
            log_error "  RHEL/Alma：sudo dnf install rsync"
            log_error "  macOS：brew install rsync"
            exit 1
        fi
    fi
    log_info "rsync 仓库 → ${RELEASE_DIR}"
    rsync -a \
        --exclude='/.git/' \
        --exclude='/.env' \
        --exclude='/.env.local' \
        --exclude='/shared/' \
        --exclude='/releases/' \
        --exclude='/current' \
        --exclude='/previous' \
        --exclude='/var/' \
        --exclude='/.venv/' \
        --exclude='/node_modules/' \
        --exclude='/apps/worker/var/' \
        --exclude='/apps/web/.next/' \
        --exclude='/apps/web/node_modules/' \
        --exclude='/.lumen-script.lock/' \
        --exclude='/.update.log' \
        --exclude='/.install-logs/' \
        "${ROOT}/" "${RELEASE_DIR}/"

    emit_info "key=release_id" "value=${release_id}"
    emit_info "key=release_dir" "value=${RELEASE_DIR}"
    emit_step_done
}

# ---------------------------------------------------------------------------
# D. 生成或合并 shared/.env
#   - 不存在：从 release 内的 .env.example 拷贝，然后 awk 替换 placeholder
#   - 存在：原样保留
#   - 写入 LUMEN_IMAGE_REGISTRY / LUMEN_IMAGE_TAG / LUMEN_VERSION / LUMEN_DATA_ROOT / LUMEN_DB_ROOT
#   - 在 release dir 创建 .env -> shared/.env 的相对 symlink，让 docker compose 自动读
# ---------------------------------------------------------------------------
prepare_env_file() {
    emit_step_start prepare "生成或合并 shared/.env"
    local shared_env="${SHARED_DIR}/.env"
    local example="${RELEASE_DIR}/.env.example"

    if [ ! -f "${example}" ]; then
        log_error "找不到 ${example}（仓库 .env.example 缺失？）"
        exit 1
    fi

    if [ ! -f "${shared_env}" ]; then
        log_info "shared/.env 不存在，从 .env.example 拷贝并生成强随机密钥。"
        cp "${example}" "${shared_env}"
        chmod 600 "${shared_env}"
        ensure_required_env_secrets "${shared_env}" || exit 1
        log_info "已写入随机密钥（DB_PASSWORD/REDIS_PASSWORD/SESSION_SECRET/BYOK_API_KEY_MASTER_SECRET/TELEGRAM_BOT_SHARED_SECRET）。"
    else
        log_info "shared/.env 已存在，跳过密钥生成。"
        ensure_required_env_secrets "${shared_env}" || exit 1
        # 兜底：补齐 docker compose 必需的 DB_USER/DB_PASSWORD/DB_NAME
        lumen_ensure_compose_db_env_vars "${shared_env}" || exit 1
        case "${LUMEN_ENV_MIGRATE_CONTAINER_URLS:-dry-run}" in
            0|false|FALSE|False|no|NO|No|off|OFF|Off)
                log_info "跳过旧 .env 容器内 URL 检查（LUMEN_ENV_MIGRATE_CONTAINER_URLS=0）。"
                ;;
            apply|--apply)
                log_info "检查并迁移旧 .env 容器内 URL（白名单 + backup）。"
                lumen_migrate_container_urls "${shared_env}" --dry-run || exit 1
                lumen_migrate_container_urls "${shared_env}" --apply || exit 1
                ;;
            *)
                log_info "检查旧 .env 容器内 URL（白名单 dry-run，不落盘）。"
                local dry_run_output
                dry_run_output="$(lumen_migrate_container_urls "${shared_env}" --dry-run)" || {
                    printf '%s\n' "${dry_run_output:-}" >&2
                    exit 1
                }
                printf '%s\n' "${dry_run_output}"
                case "${dry_run_output}" in
                    *"dry-run only;"*)
                        log_error "检测到旧 .env 仍需要容器地址迁移；默认 dry-run 不落盘，安装已停止。"
                        log_error "请确认上方 diff 后执行："
                        log_error "  bash ${RELEASE_DIR}/scripts/lumenctl.sh migrate-env-apply ${shared_env}"
                        log_error "或显式：LUMEN_ENV_MIGRATE_CONTAINER_URLS=apply bash ${SCRIPT_DIR}/install.sh --install"
                        exit 1
                        ;;
                esac
                log_warn "如上方显示 DATABASE_URL/REDIS_URL 等变更，请确认后执行："
                log_warn "  bash ${RELEASE_DIR}/scripts/lumenctl.sh migrate-env-apply ${shared_env}"
                ;;
        esac
    fi

    # 写入/覆盖镜像与版本变量（每次安装都更新，便于 update.sh 读到一致 tag）
    local image_registry image_tag lumen_version
    image_registry="${LUMEN_IMAGE_REGISTRY:-ghcr.io/cyeinfpro}"
    image_tag="${INSTALL_IMAGE_TAG_OVERRIDE:-${LUMEN_IMAGE_TAG:-latest}}"
    if [ -f "${RELEASE_DIR}/VERSION" ]; then
        lumen_version="$(head -n1 "${RELEASE_DIR}/VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
    fi
    if [ -z "${lumen_version:-}" ] && [ -d "${ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
        lumen_version="$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
    fi
    lumen_version="${lumen_version:-unknown}"

    env_file_set "${shared_env}" LUMEN_IMAGE_REGISTRY "${image_registry}"
    env_file_set "${shared_env}" LUMEN_IMAGE_TAG      "${image_tag}"
    env_file_set "${shared_env}" LUMEN_VERSION        "${lumen_version}"
    env_file_set "${shared_env}" LUMEN_DATA_ROOT      "${LUMEN_DATA_ROOT}"
    env_file_set "${shared_env}" LUMEN_DB_ROOT        "${LUMEN_DB_ROOT}"
    env_file_set "${shared_env}" LUMEN_POSTGRES_UID   "${LUMEN_POSTGRES_UID}"
    env_file_set "${shared_env}" LUMEN_POSTGRES_GID   "${LUMEN_POSTGRES_GID}"
    env_file_set "${shared_env}" LUMEN_REDIS_UID      "${LUMEN_REDIS_UID}"
    env_file_set "${shared_env}" LUMEN_REDIS_GID      "${LUMEN_REDIS_GID}"
    env_file_set "${shared_env}" LUMEN_APP_UID        "${LUMEN_APP_UID}"
    env_file_set "${shared_env}" LUMEN_APP_GID        "${LUMEN_APP_GID}"
    env_file_set "${shared_env}" LUMEN_APP_STORAGE_GID "${LUMEN_APP_STORAGE_GID}"
    if [ -n "${LUMEN_WEB_BIND_HOST:-}" ]; then
        env_file_set "${shared_env}" WEB_BIND_HOST "${LUMEN_WEB_BIND_HOST}"
    elif [ -z "$(env_file_get WEB_BIND_HOST "${shared_env}")" ]; then
        env_file_set "${shared_env}" WEB_BIND_HOST "127.0.0.1"
    fi

    # 创建 release/.env -> ../../shared/.env 的相对 symlink
    # docker compose 默认从 -f 所在目录加载 .env；让它读到 shared/.env。
    # 用 lumen_atomic_replace_symlink 替代 rm -f + ln -s 的两步操作，避免
    # 中间窗口（compose 在此瞬间读 .env 会拿到 ENOENT）。
    if command -v lumen_atomic_replace_symlink >/dev/null 2>&1; then
        lumen_atomic_replace_symlink "../../shared/.env" "${RELEASE_DIR}/.env"
    else
        if [ -e "${RELEASE_DIR}/.env" ] || [ -L "${RELEASE_DIR}/.env" ]; then
            rm -f "${RELEASE_DIR}/.env"
        fi
        ln -s "../../shared/.env" "${RELEASE_DIR}/.env"
    fi
    log_info "已 symlink ${RELEASE_DIR}/.env -> ../../shared/.env"

    # 友善提示：PUBLIC_BASE_URL / CORS_ALLOW_ORIGINS / NEXT_PUBLIC_API_BASE 保留默认
    local pub_url cors_url
    pub_url="$(env_file_get PUBLIC_BASE_URL "${shared_env}")"
    cors_url="$(env_file_get CORS_ALLOW_ORIGINS "${shared_env}")"
    if [[ "${pub_url}" == http://localhost* ]] || [[ "${cors_url}" == http://localhost* ]]; then
        log_warn "PUBLIC_BASE_URL / CORS_ALLOW_ORIGINS 仍是 localhost 默认值。"
        log_warn "  生产部署后请编辑 ${shared_env}，改成你的公网域名（例如 https://lumen.example.com）。"
        log_warn "  并在 nginx 配置正确的 server_name + 反代到 127.0.0.1:3000。"
    fi
    if lumen_configure_proxy_env "${shared_env}" >/dev/null 2>&1; then
        log_info "已配置更新/拉镜像代理（LUMEN_UPDATE_PROXY_URL / LUMEN_HTTP_PROXY / HTTP_PROXY）。"
        emit_info "key=proxy" "value=configured"
    fi

    emit_info "key=shared_env" "value=${shared_env}"
    emit_info "key=image_registry" "value=${image_registry}"
    emit_info "key=image_tag" "value=${image_tag}"
    emit_step_done
}

# ---------------------------------------------------------------------------
# E. 探测 GHCR 镜像可用性
# ---------------------------------------------------------------------------
probe_ghcr_image_tag() {
    emit_step_start prepare "探测 GHCR 镜像 tag 可用性"
    local shared_env="${SHARED_DIR}/.env"
    local registry tag api_url
    registry="$(env_file_get LUMEN_IMAGE_REGISTRY "${shared_env}")"
    tag="$(env_file_get LUMEN_IMAGE_TAG "${shared_env}")"

    # 只在默认 ghcr.io/cyeinfpro 路径下做探测；自定义 registry 直接信任用户配置
    if [[ "${registry}" != ghcr.io/cyeinfpro* ]]; then
        log_info "自定义镜像 registry=${registry}，跳过 GHCR tag 探测。"
        emit_step_done
        return 0
    fi

    # 用户显式 --image-tag 覆盖时不做 fallback（信任用户）
    if [ -n "${INSTALL_IMAGE_TAG_OVERRIDE}" ]; then
        log_info "已用 --image-tag=${INSTALL_IMAGE_TAG_OVERRIDE}，跳过 GHCR 探测。"
        emit_step_done
        return 0
    fi

    # --build 模式不需要远程镜像
    if [ "${INSTALL_BUILD_FLAG}" = "1" ]; then
        log_info "--build 模式，跳过 GHCR 探测（将本地构建镜像）。"
        emit_step_done
        return 0
    fi

    # GHCR public packages tags API（对未 token 也返回 200/404）
    api_url="https://ghcr.io/v2/cyeinfpro/lumen-api/tags/list"
    log_info "探测 ${api_url}（tag=${tag}）..."
    local resp http_code
    http_code="$(curl -fsS -o /tmp/lumen-ghcr-probe.$$ -w '%{http_code}' --max-time 10 "${api_url}" 2>/dev/null || echo "000")"
    resp="$(cat /tmp/lumen-ghcr-probe.$$ 2>/dev/null || true)"
    rm -f /tmp/lumen-ghcr-probe.$$

    if [ "${http_code}" = "200" ] && printf '%s' "${resp}" | grep -q "\"${tag}\""; then
        log_info "GHCR 上存在 tag=${tag}，使用配置值。"
    elif [ "${http_code}" = "200" ]; then
        # 探测到 tags 列表但缺 ${tag}：尝试 fallback 到 main
        if printf '%s' "${resp}" | grep -q '"main"'; then
            log_warn "GHCR 上未找到 tag=${tag}，回退到 main。v1.0.0 发布后请改回 latest。"
            env_file_set "${shared_env}" LUMEN_IMAGE_TAG "main"
            # 在 .env 顶部追加一行注释（如果还没加过）
            if ! grep -q '^# install.sh: fallback to main' "${shared_env}"; then
                printf '\n# install.sh: fallback to main; v1.0.0 发布后改回 latest\n' >> "${shared_env}"
            fi
        else
            log_warn "GHCR 上既无 ${tag} 也无 main。保留配置，pull 时可能失败。"
        fi
    else
        # API 探测失败但 .env 已有 tag → 不动
        log_warn "GHCR API 探测失败（HTTP ${http_code}），保留 .env 配置 LUMEN_IMAGE_TAG=${tag}。"
    fi
    emit_step_done
}

# ---------------------------------------------------------------------------
# F. 拉镜像 / 构建 -> 起 PG/Redis -> migrate -> bootstrap -> api/worker/web (+tgbot)
# ---------------------------------------------------------------------------
pull_or_build_images() {
    if [ "${INSTALL_BUILD_FLAG}" = "1" ]; then
        emit_step_start containers "本地构建镜像（lumen_compose build）"
        # build 失败通常是 Dockerfile / 资源问题，重试 2 次（每次都是 from-scratch 的网络拉基础镜像）。
        if ! lumen_retry 2 5 "docker compose build" _install_compose build; then
            log_error "本地 docker compose build 失败。"
            exit 1
        fi
    else
        emit_step_start containers "拉取镜像（lumen_compose pull）"
        # 网络抖动是 pull 失败最常见的原因；先重试 3 次（指数退避 5/10/20），仍失败再走 fallback。
        if ! lumen_retry 3 5 "docker compose pull" _install_compose_pull_per_image; then
            local shared_env="${SHARED_DIR}/.env"
            local registry current_tag
            registry="$(env_file_get LUMEN_IMAGE_REGISTRY "${shared_env}")"
            current_tag="$(env_file_get LUMEN_IMAGE_TAG "${shared_env}")"
            if [ -z "${INSTALL_IMAGE_TAG_OVERRIDE}" ] \
                && [[ "${registry}" == ghcr.io/cyeinfpro* ]] \
                && [ "${current_tag}" != "main" ]; then
                log_warn "docker compose pull 失败，疑似默认镜像 tag=${current_tag} 尚未发布；回退到 main 后重试一次。"
                env_file_set "${shared_env}" LUMEN_IMAGE_TAG "main"
                if ! grep -q '^# install.sh: fallback to main after pull failure' "${shared_env}"; then
                    printf '\n# install.sh: fallback to main after pull failure; publish stable/latest then switch back\n' >> "${shared_env}"
                fi
                if lumen_retry 2 5 "docker compose pull (main fallback)" _install_compose_pull_per_image; then
                    log_info "已使用 LUMEN_IMAGE_TAG=main 拉取镜像。"
                else
                    log_error "docker compose pull 失败（fallback main 后仍失败）。"
                    log_error "  常见原因：1) 国内网络访问 ghcr 受阻 → 设置 LUMEN_HTTP_PROXY 或自托管 registry"
                    log_error "            2) main 镜像也未发布 → 使用 --build 本地构建"
                    exit 1
                fi
            else
                log_error "docker compose pull 失败。"
                log_error "  常见原因：1) 国内网络访问 ghcr 受阻 → 设置 LUMEN_HTTP_PROXY 或自托管 registry"
                log_error "            2) 镜像 tag 不存在 → 用 --image-tag=vX.Y.Z 钉死 tag 或 --build 本地构建"
                exit 1
            fi
        fi
    fi
    emit_step_done
}

start_infrastructure() {
    emit_step_start containers "启动 PostgreSQL / Redis 并等待健康"
    if ! _install_compose up --pull missing -d --wait postgres redis; then
        log_error "postgres / redis 启动或健康检查失败。"
        exit 1
    fi
    INSTALL_STARTED_SERVICES+=("postgres" "redis")
    log_info "PG / Redis 已健康。"
    emit_step_done
}

run_migration() {
    emit_step_start migrate_db "执行数据库迁移（migrate profile，alembic upgrade head）"
    if ! _install_compose --profile migrate run --rm migrate; then
        log_error "alembic 迁移失败。检查 PG 容器健康状态与 DATABASE_URL。"
        exit 1
    fi
    log_info "数据库迁移完成。"
    emit_step_done
}

run_bootstrap_admin() {
    local shared_env="${SHARED_DIR}/.env"
    # 已 bootstrapped 过则跳过
    if grep -q '^LUMEN_BOOTSTRAPPED=1' "${shared_env}" 2>/dev/null; then
        log_info "shared/.env 中已记录 LUMEN_BOOTSTRAPPED=1，跳过管理员创建。"
        return 0
    fi

    emit_step_start migrate_db "创建首个管理员账号（bootstrap profile）"

    local admin_email admin_pwd
    if [ "${LUMEN_NONINTERACTIVE:-}" = "1" ]; then
        admin_email="${LUMEN_ADMIN_EMAIL:-}"
        admin_pwd="${LUMEN_ADMIN_PASSWORD:-}"
        if [ -z "${admin_email}" ] || [ -z "${admin_pwd}" ]; then
            log_error "LUMEN_NONINTERACTIVE=1 但未提供 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD。"
            exit 1
        fi
        if [ "${#admin_pwd}" -lt 12 ]; then
            log_error "LUMEN_ADMIN_PASSWORD 长度不能少于 12 位。"
            exit 1
        fi
    else
        admin_email="$(read_or_default '管理员邮箱' 'admin@example.com')"
        admin_pwd=""
        while [ -z "${admin_pwd}" ]; do
            admin_pwd="$(read_secret '管理员密码（≥12 chars）')"
            if [ -z "${admin_pwd}" ]; then
                log_warn "密码不能为空。"
            elif [ "${#admin_pwd}" -lt 12 ]; then
                log_warn "密码长度不能少于 12 位。"
                admin_pwd=""
            fi
        done
    fi

    # bootstrap 容器读 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD env（compose 已声明）
    # 不写入 .env（§10.3：不要把管理员密码写入 .env）
    # 注意：不再把 --password 作为 CLI 位置参数传，避免密码出现在 host
    # `ps -ef` / docker inspect Args / journalctl logs 里。bootstrap.py 已支持
    # 读 LUMEN_ADMIN_PASSWORD env 兜底。
    # 捕获 bootstrap 输出，区分"已存在"（无害，幂等重跑常见）vs "真错误"（DB
    # 连接 / migration 漂移 / 校验失败），让用户能立即定位是不是真问题。
    local _boot_log
    _boot_log="$(mktemp)" || _boot_log=""
    local _boot_rc=0
    if [ -n "${_boot_log}" ]; then
        LUMEN_ADMIN_EMAIL="${admin_email}" LUMEN_ADMIN_PASSWORD="${admin_pwd}" \
            _install_compose --profile bootstrap run --rm \
            -e "LUMEN_ADMIN_EMAIL=${admin_email}" \
            -e "LUMEN_ADMIN_PASSWORD=${admin_pwd}" \
            bootstrap python -m app.scripts.bootstrap "${admin_email}" --role admin \
            >"${_boot_log}" 2>&1 || _boot_rc=$?
        if [ "${_boot_rc}" -eq 0 ]; then
            cat "${_boot_log}" || true
        elif grep -qiE 'already (exists|created)|duplicate key|user_already_exists|already_admin' "${_boot_log}"; then
            log_info "管理员账号 ${admin_email} 已存在（bootstrap 幂等跳过）。"
        else
            log_warn "bootstrap 返回非零（rc=${_boot_rc}），可能是 DB 连接 / migration 漂移 / 校验失败。"
            log_warn "  最近输出："
            tail -n 15 "${_boot_log}" | sed 's/^/    /' >&2
            log_warn "  如确认账号已存在仅是 race，可登录后到管理面板验证；否则查 logs：docker compose logs --tail=120 migrate api"
        fi
        rm -f "${_boot_log}"
    else
        # mktemp 失败：退化为旧行为（不区分错误来源）
        if ! LUMEN_ADMIN_EMAIL="${admin_email}" LUMEN_ADMIN_PASSWORD="${admin_pwd}" \
                _install_compose --profile bootstrap run --rm \
                -e "LUMEN_ADMIN_EMAIL=${admin_email}" \
                -e "LUMEN_ADMIN_PASSWORD=${admin_pwd}" \
                bootstrap python -m app.scripts.bootstrap "${admin_email}" --role admin; then
            log_warn "bootstrap 返回非零。常见原因：管理员账号已存在；继续后续步骤。"
        fi
    fi

    # 标记已 bootstrapped，避免重复运行
    if ! grep -q '^LUMEN_BOOTSTRAPPED=1' "${shared_env}"; then
        printf 'LUMEN_BOOTSTRAPPED=1\n' >> "${shared_env}"
    fi

    INSTALL_ADMIN_EMAIL="${admin_email}"
    log_info "管理员账号：${admin_email}"
    emit_info "key=admin_email" "value=${admin_email}"
    emit_step_done
}

start_application_services() {
    emit_step_start containers "启动 API / Worker / Web（compose --wait）"
    if ! _install_compose up --pull missing -d --wait api worker web; then
        log_error "api / worker / web 启动或健康检查失败。"
        exit 1
    fi
    INSTALL_STARTED_SERVICES+=("api" "worker" "web")

    # tgbot 仅在 .env 提供了非空 TELEGRAM_BOT_TOKEN 时启动
    local shared_env="${SHARED_DIR}/.env"
    local bot_token
    bot_token="$(env_file_get TELEGRAM_BOT_TOKEN "${shared_env}")"
    if [ -n "${bot_token}" ]; then
        log_info "检测到 TELEGRAM_BOT_TOKEN 非空，启动 tgbot service。"
        if ! _install_compose --profile tgbot up --pull missing -d tgbot; then
            log_warn "tgbot 启动失败（可能是 token 无效或网络问题）。主栈不受影响。"
            INSTALL_TGBOT_STATUS="failed"
        else
            INSTALL_STARTED_SERVICES+=("tgbot")
            INSTALL_TGBOT_STATUS="started"
        fi
    else
        log_info "未配置 TELEGRAM_BOT_TOKEN，跳过 tgbot。"
        INSTALL_TGBOT_STATUS="skipped"
    fi
    emit_step_done
}

# ---------------------------------------------------------------------------
# G. 切换 current symlink
# ---------------------------------------------------------------------------
switch_current_symlink() {
    emit_step_start switch "切换 current symlink → releases/${RELEASE_ID}"
    local cur="${DEPLOY_ROOT}/current"
    # 保存切换前的 target，cleanup_on_failure 在后续 health 失败时切回。
    INSTALL_PREV_CURRENT_TARGET=""
    if [ -L "${cur}" ]; then
        local prev_target
        prev_target="$(readlink "${cur}" 2>/dev/null || true)"
        if [ -n "${prev_target}" ] && [ "${prev_target}" != "releases/${RELEASE_ID}" ]; then
            INSTALL_PREV_CURRENT_TARGET="${prev_target}"
            if ! lumen_atomic_replace_symlink "${prev_target}" "${DEPLOY_ROOT}/previous" 2>/dev/null; then
                log_warn "无法更新 previous symlink → ${prev_target}（已忽略，不阻断 switch）"
            fi
        fi
    fi
    if ! lumen_atomic_replace_symlink "releases/${RELEASE_ID}" "${cur}"; then
        log_error "切换 current → releases/${RELEASE_ID} 失败。"
        exit 1
    fi
    INSTALL_SWITCHED=1
    log_info "${cur} → releases/${RELEASE_ID}"
    emit_step_done
}

# ---------------------------------------------------------------------------
# H. 安装/刷新一键更新 systemd runner
# ---------------------------------------------------------------------------
install_update_runner_units() {
    emit_step_start prepare "安装一键更新 runner（systemd path）"
    if [ "${OS}" != "linux" ] || ! command -v systemctl >/dev/null 2>&1 || [ ! -d /run/systemd/system ]; then
        log_warn "未检测到 Linux systemd，跳过一键更新 runner 安装；命令行 update-lumen 不受影响。"
        emit_step_done
        return 0
    fi

    local src_dir="${RELEASE_DIR}/deploy/systemd"
    local src_path="${src_dir}/lumen-update.path"
    local src_runner="${src_dir}/lumen-update-runner.service"
    if [ ! -f "${src_path}" ] || [ ! -f "${src_runner}" ]; then
        log_warn "找不到 update runner unit 模板（${src_dir}），跳过一键更新 runner 安装。"
        emit_step_done
        return 0
    fi

    local data_root deploy_root backup_root tmp_dir
    data_root="${LUMEN_DATA_ROOT%/}"
    deploy_root="${DEPLOY_ROOT%/}"
    backup_root="${LUMEN_BACKUP_ROOT:-${data_root}/backup}"
    backup_root="${backup_root%/}"
    tmp_dir="$(mktemp -d)"

    _render_update_runner_units \
        "${src_path}" \
        "${src_runner}" \
        "${tmp_dir}" \
        "${data_root}" \
        "${backup_root}" \
        "${deploy_root}"

    lumen_ensure_backup_service_user "${backup_root}"

    if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update.path" /etc/systemd/system/lumen-update.path; then
        log_warn "安装 lumen-update.path 失败，面板一键更新将不可用。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if ! lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-runner.service" /etc/systemd/system/lumen-update-runner.service; then
        log_warn "安装 lumen-update-runner.service 失败，面板一键更新将不可用。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if [ -f "${tmp_dir}/lumen-update-warm.path" ] && [ -f "${tmp_dir}/lumen-update-warm.service" ]; then
        lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-warm.path" /etc/systemd/system/lumen-update-warm.path \
            || log_warn "安装 lumen-update-warm.path 失败，镜像预热将不可用。"
        lumen_run_as_root install -m 0644 "${tmp_dir}/lumen-update-warm.service" /etc/systemd/system/lumen-update-warm.service \
            || log_warn "安装 lumen-update-warm.service 失败，镜像预热将不可用。"
    fi
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.service "安装 lumen-backup.service 失败，自动/手动触发备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.timer "安装 lumen-backup.timer 失败，自动备份将不可用。"
    lumen_install_optional_systemd_unit "${tmp_dir}" lumen-backup.path "安装 lumen-backup.path 失败，管理后台立即备份将无法触发宿主机备份。"
    if ! lumen_run_as_root systemctl daemon-reload; then
        log_warn "systemctl daemon-reload 失败，面板一键更新可能不可用。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if ! lumen_run_as_root systemctl enable --now lumen-update.path; then
        log_warn "启用 lumen-update.path 失败，面板一键更新将不可用；可稍后手动执行 systemctl enable --now lumen-update.path。"
        rm -rf "${tmp_dir}"
        emit_step_done
        return 0
    fi
    if [ -f "${tmp_dir}/lumen-update-warm.path" ]; then
        lumen_run_as_root systemctl enable --now lumen-update-warm.path \
            || log_warn "启用 lumen-update-warm.path 失败，镜像预热将不可用；可稍后手动执行 systemctl enable --now lumen-update-warm.path。"
    fi
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-backup.timer "启用 lumen-backup.timer 失败，自动备份将不可用；可稍后手动执行 systemctl enable --now lumen-backup.timer。"
    lumen_enable_optional_systemd_unit "${tmp_dir}" lumen-backup.path "启用 lumen-backup.path 失败，管理后台立即备份将不可用；可稍后手动执行 systemctl enable --now lumen-backup.path。"
    rm -rf "${tmp_dir}"

    log_info "一键更新 runner 已启用：监听 ${backup_root}/.update.trigger"
    emit_info "key=update_trigger" "value=${backup_root}/.update.trigger"
    emit_info "key=warm_trigger" "value=${backup_root}/.warm.trigger"
    emit_info "key=backup_trigger" "value=${backup_root}/.backup.trigger"
    emit_step_done
}

# ---------------------------------------------------------------------------
# I. 健康检查（HTTP + Compose 状态）
# ---------------------------------------------------------------------------
run_health_checks() {
    emit_step_start health_post "健康检查（HTTP + compose service 状态）"

    if ! _install_health_http "http://127.0.0.1:8000/healthz" 60 2; then
        log_error "API 健康检查失败：http://127.0.0.1:8000/healthz 在 60s 内未返回 2xx/3xx。"
        log_error "  排查：${COMPOSE_LABEL} logs --tail=200 api"
        exit 1
    fi
    log_info "API /healthz 通过。"

    if ! _install_health_http "http://127.0.0.1:3000/" 60 2; then
        log_error "Web 健康检查失败：http://127.0.0.1:3000/ 在 60s 内未返回 2xx/3xx。"
        log_error "  排查：${COMPOSE_LABEL} logs --tail=200 web"
        exit 1
    fi
    log_info "Web 首页通过。"

    local health_services=("api" "worker" "web")
    local shared_env="${SHARED_DIR}/.env"
    if [ -n "$(env_file_get TELEGRAM_BOT_TOKEN "${shared_env}")" ]; then
        # tgbot 没有 healthcheck（compose 里没声明），降级到 service started
        :
    fi
    if ! _install_health_compose "${health_services[@]}"; then
        log_error "compose service 健康状态异常。"
        exit 1
    fi
    log_info "所有 compose service 健康。"
    emit_step_done
}

# ---------------------------------------------------------------------------
# J. systemd 处理（不自动 disable，仅提示）
# ---------------------------------------------------------------------------
warn_about_legacy_systemd() {
    if ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    local has_active=0 unit
    for unit in lumen-api.service lumen-worker.service lumen-web.service lumen-tgbot.service; do
        if systemctl is-active --quiet "${unit}" 2>/dev/null; then
            has_active=1
            break
        fi
    done
    if [ "${has_active}" -eq 1 ]; then
        log_warn ""
        log_warn "检测到旧版本的 systemd 服务仍在运行（可能与 docker 容器抢端口）："
        log_warn "  Docker 栈已启动并健康。建议手动禁用旧 systemd 服务以避免冲突："
        log_warn "    sudo systemctl disable --now lumen-api lumen-worker lumen-web lumen-tgbot"
        log_warn "  确认后再访问 Web，避免请求被旧 systemd 进程截获。"
    fi
}

# ---------------------------------------------------------------------------
# K. 输出汇总
# ---------------------------------------------------------------------------
print_summary() {
    emit_step_start cleanup "安装完成汇总"
    local shared_env="${SHARED_DIR}/.env"
    local image_tag
    image_tag="$(env_file_get LUMEN_IMAGE_TAG "${shared_env}")"
    cat <<EOF

  ${LUMEN_C_BOLD}Lumen 安装完成（Docker Compose 全栈）${LUMEN_C_RESET}

  Web 地址 ......... http://127.0.0.1:3000/
                     （默认仅本机监听；生产请通过 nginx/Caddy/Traefik 反代 HTTPS）
  API 健康检查 ..... http://127.0.0.1:8000/healthz
  管理员邮箱 ....... ${INSTALL_ADMIN_EMAIL:-（已存在或非交互模式未设置）}
  Provider 配置 .... 登录后 → 右上角「管理 → 上游 Provider」
                     默认 PROVIDERS=[]，需添加 1 条才能调图像 API

  部署目录 ......... ${DEPLOY_ROOT}/current → releases/${RELEASE_ID}
  数据目录 ......... storage/backup=${LUMEN_DATA_ROOT}，postgres/redis=${LUMEN_DB_ROOT}
  共享 .env ........ ${SHARED_DIR}/.env
  镜像 tag ......... ${image_tag}
  tgbot ............ ${INSTALL_TGBOT_STATUS:-unknown}（started=正常 / failed=token 或网络问题 / skipped=未配置）

  ${LUMEN_C_BOLD}日常运维${LUMEN_C_RESET}

    状态：    cd ${DEPLOY_ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose ps
    日志：    cd ${DEPLOY_ROOT}/current && COMPOSE_PROJECT_NAME=lumen docker compose logs -f api
    更新：    bash ${DEPLOY_ROOT}/current/scripts/lumenctl.sh update-lumen
    备份：    bash ${DEPLOY_ROOT}/current/scripts/backup.sh   （输出到 ${LUMEN_DATA_ROOT}/backup）
    卸载：    bash ${DEPLOY_ROOT}/current/scripts/uninstall.sh

EOF
    emit_step_done
}

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
prepare_env_file
probe_ghcr_image_tag
pull_or_build_images
start_infrastructure
run_migration
run_bootstrap_admin
start_application_services
switch_current_symlink
install_update_runner_units
run_health_checks
warn_about_legacy_systemd
print_summary

trap - ERR EXIT
lumen_release_lock 2>/dev/null || true
exit 0
