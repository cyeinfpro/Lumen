#!/usr/bin/env bash
# Lumen 统一运维入口（Docker compose 全栈版）。
# 用法：
#   bash scripts/lumenctl.sh                  # 交互菜单
#   bash scripts/lumenctl.sh install-lumen    # 安装（透传给 install.sh）
#   bash scripts/lumenctl.sh update-lumen     # 更新（透传给 update.sh）
#   bash scripts/lumenctl.sh status           # docker compose ps + healthz
#   bash scripts/lumenctl.sh logs api         # 跟随 api 日志
#   bash scripts/lumenctl.sh nginx-optimize   # nginx 反代向导

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"
NGINX_FILES=()
LUMEN_USE_SUDO="${LUMEN_USE_SUDO:-0}"
LUMEN_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
LUMEN_COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-lumen}"
export COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT}"

_LUMENCTL_MODULE_FILES=(
    lumenctl/validation.sh
    lumenctl/compose.sh
    lumenctl/systemd_image_job.sh
    lumenctl/nginx.sh
)
_LUMENCTL_MODULES_LOADED=0

trap 'log_error "lumenctl 失败：第 ${LINENO} 行返回非零状态。请查看上方输出修正后重试。"' ERR

usage() {
    cat <<EOF
Lumen 一键运维菜单

用法：
  bash scripts/lumenctl.sh [command] [args...]

Lifecycle commands:
  menu                 打开交互菜单（默认）
  install-lumen        安装 Lumen（调用 scripts/install.sh）
  update-lumen         更新 Lumen（调用 scripts/update.sh）
  uninstall-lumen      卸载 Lumen（调用 scripts/uninstall.sh）
  rollback             回滚到 previous release（pull 旧 tag + compose up）
  version              输出 VERSION + 镜像 tag + git sha
  bootstrap-scripts    应急：解析 GitHub branch commit 后强制热替换运维脚本
                       （平时入口处自动 self-update，TTL=600s；本命令突破 TTL）

Docker compose runtime:
  status               docker compose ps + 健康检查
  logs [service]       跟随 service 日志（默认 api，等价 docker compose logs -f）
  start                up -d --wait api worker web
  stop                 stop api worker web tgbot
  restart              up -d --force-recreate api worker web
  migrate              compose --profile migrate run --rm migrate
  bootstrap            创建初始 admin（需 LUMEN_ADMIN_EMAIL / LUMEN_ADMIN_PASSWORD）
  migrate-env          dry-run 检查旧 .env 的容器内 URL
  migrate-env-apply    按白名单迁移旧 .env 的容器内 URL，并写 .bak
  backup               调用 scripts/backup.sh
  restore <ts>         调用 scripts/restore.sh <timestamp>

Auxiliary:
  install-storage-units 安装存储后端组件（lumen-storage-mount + 4 个 systemd unit）
                       对应管理后台「存储后端」页（local / smb 切换）
  install-image-job    安装 image-job sidecar、systemd 服务
  uninstall-image-job  卸载 image-job sidecar
  nginx-scan           扫描 nginx 配置
  nginx-optimize       nginx 反代优化向导（Lumen / sub2api / image-job）
  nginx-lumen          生成/更新 Lumen 反代配置
  nginx-sub2api        生成/更新 sub2api 单机公网反代配置
  nginx-sub2api-inner  生成/更新 sub2api 内层反代配置
  nginx-sub2api-outer  生成/更新 sub2api 外层公网反代配置
  nginx-image-job      给已有站点注入 image-job 路由
  help                 显示帮助

EOF
}

lumenctl_resolve_script() {
    local script_name="$1"
    if [ -f "${SCRIPT_DIR}/${script_name}" ]; then
        printf '%s' "${SCRIPT_DIR}/${script_name}"
        return 0
    fi
    if [ -f "${ROOT}/current/scripts/${script_name}" ]; then
        printf '%s' "${ROOT}/current/scripts/${script_name}"
        return 0
    fi
    if [ -f "${LUMEN_DEPLOY_ROOT}/current/scripts/${script_name}" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}/current/scripts/${script_name}"
        return 0
    fi
    if [ -f "${ROOT}/scripts/${script_name}" ]; then
        printf '%s' "${ROOT}/scripts/${script_name}"
        return 0
    fi
    return 1
}

lumenctl_raw_script_url() {
    local script_name="$1"
    local branch="${LUMEN_BRANCH:-main}"
    local raw_base="${LUMEN_RAW_BASE:-https://raw.githubusercontent.com/cyeinfpro/Lumen/${branch}}"
    printf '%s/scripts/%s' "${raw_base%/}" "${script_name}"
}

lumenctl_bootstrap_install_from_github() {
    local raw_url tmp_script install_dir
    raw_url="$(lumenctl_raw_script_url install.sh)"
    install_dir="${LUMEN_INSTALL_DIR:-${ROOT}}"

    ensure_cmd curl "当前目录不是完整 Lumen 仓库，且缺少 curl，无法从 GitHub 拉取 install.sh"
    tmp_script="$(mktemp)" || {
        log_error "无法创建临时文件，不能从 GitHub bootstrap 安装。"
        exit 1
    }

    log_warn "当前目录缺少 scripts/install.sh，将从 GitHub bootstrap 完整仓库。"
    log_info "GitHub raw：${raw_url}"
    log_info "目标目录：${install_dir}"
    if ! curl -fsSL "${raw_url}" -o "${tmp_script}"; then
        rm -f "${tmp_script}"
        log_error "无法从 GitHub 下载 install.sh：${raw_url}"
        log_error "可手动执行：git clone ${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git} ${install_dir}"
        exit 1
    fi

    export LUMEN_INSTALL_DIR="${install_dir}"
    export LUMEN_REPO_URL="${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git}"
    export LUMEN_BRANCH="${LUMEN_BRANCH:-main}"

    local rc=0
    bash "${tmp_script}" --install "$@" || rc=$?
    rm -f "${tmp_script}"
    return "${rc}"
}

run_lumen_script() {
    local script_name="$1"
    shift || true
    local script_path=""
    # CLI/menu updates must obtain commit-proven release files on non-git hosts.
    # The "-" expansion defaults only an unset variable and preserves 0/1/empty.
    if [ "${script_name}" = "update.sh" ]; then
        local LUMEN_UPDATE_GIT_PULL="${LUMEN_UPDATE_GIT_PULL-1}"
        export LUMEN_UPDATE_GIT_PULL
    fi
    log_step "执行 ${script_name}"
    if ! script_path="$(lumenctl_resolve_script "${script_name}")"; then
        if [ "${script_name}" = "install.sh" ]; then
            lumenctl_bootstrap_install_from_github "$@"
            return $?
        fi
        log_error "找不到脚本：${ROOT}/current/scripts/${script_name} 或 ${ROOT}/scripts/${script_name}"
        log_error "如果这是新机器，请先从 GitHub 拉完整仓库：git clone ${LUMEN_REPO_URL:-https://github.com/cyeinfpro/Lumen.git} ${ROOT}"
        exit 1
    fi
    # 全栈 docker 化后 install.sh / update.sh / uninstall.sh 都接受透传 flag。
    # 不再强制塞 --install；让上游传什么就传什么。
    case "${script_name}" in
        install.sh|update.sh|uninstall.sh|backup.sh|restore.sh)
            if [ "$(detect_os)" = "linux" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
                ensure_cmd sudo "请安装 sudo，或切换到 root 后重试"
                # sudo 默认 env_reset 会把 LUMEN_UPDATE_GIT_PULL 等 inline env vars 全部 strip。
                # 用 env KEY=val 显式重建，确保 update.sh / install.sh 能读到调用方的 LUMEN_*。
                local env_args=()
                local _v
                while IFS= read -r _v; do
                    [ -n "${_v}" ] || continue
                    env_args+=("${_v}=${!_v}")
                done < <(compgen -e 2>/dev/null | grep '^LUMEN_' || true)
                if [ "${#env_args[@]}" -gt 0 ]; then
                    lumen_sudo env "${env_args[@]}" bash "${script_path}" "$@"
                else
                    lumen_sudo bash "${script_path}" "$@"
                fi
            else
                bash "${script_path}" "$@"
            fi
            ;;
        *)
            bash "${script_path}" "$@"
            ;;
    esac
}

run_lumen_install_script() {
    case "${1:-}" in
        install|--install)
            run_lumen_script install.sh "$@"
            ;;
        *)
            run_lumen_script install.sh --install "$@"
            ;;
    esac
}

# Resolve all modules from one release tree so mixed-version functions cannot
# be loaded across current, deploy-root, and repository fallbacks.
lumenctl_find_module_root() {
    local candidate relative complete
    local candidates=(
        "${SCRIPT_DIR}"
        "${ROOT}/current/scripts"
        "${LUMEN_DEPLOY_ROOT}/current/scripts"
        "${ROOT}/scripts"
    )
    for candidate in "${candidates[@]}"; do
        [ -n "${candidate}" ] || continue
        complete=1
        for relative in "${_LUMENCTL_MODULE_FILES[@]}"; do
            if [ ! -f "${candidate}/${relative}" ]; then
                complete=0
                break
            fi
        done
        if [ "${complete}" = "1" ]; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

lumenctl_load_modules() {
    if [ "${_LUMENCTL_MODULES_LOADED:-0}" = "1" ]; then
        return 0
    fi

    local module_root relative
    module_root="$(lumenctl_find_module_root)" || return 1
    for relative in "${_LUMENCTL_MODULE_FILES[@]}"; do
        # shellcheck disable=SC1090
        . "${module_root}/${relative}"
    done
    _LUMENCTL_MODULES_LOADED=1
}

lumenctl_sync_script_unit() {
    local ttl_sec="$1"
    lumen_self_update_scripts_from_github_branch "${SCRIPT_DIR}" \
        "${LUMEN_SELF_UPDATE_BRANCH:-main}" \
        "${ttl_sec}" \
        lib.sh \
        backup.sh \
        restore.sh \
        update.sh \
        lumenctl.sh \
        "${_LUMENCTL_MODULE_FILES[@]}"
}

lumenctl_repair_missing_modules() {
    if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
        return 1
    fi
    if [ "${LUMEN_LUMENCTL_SELF_UPDATE:-1}" = "0" ] \
            || [ "${LUMEN_SELF_UPDATE:-1}" = "0" ]; then
        return 1
    fi

    log_warn "[lumenctl] 当前部署缺少拆分模块，强制同步完整 lumenctl 脚本单元。"
    local previous_force="${LUMEN_SELF_UPDATE_FORCE:-}"
    LUMEN_SELF_UPDATE_FORCE=1
    lumenctl_sync_script_unit 0
    if [ -n "${previous_force}" ]; then
        LUMEN_SELF_UPDATE_FORCE="${previous_force}"
    else
        unset LUMEN_SELF_UPDATE_FORCE
    fi

    case " ${LUMEN_SELF_UPDATE_CHANGED:-} " in
        *" lumenctl.sh "*|*" lib.sh "*)
            log_info "[lumenctl] 引导脚本已更新，re-exec 后加载完整模块。"
            export LUMEN_LUMENCTL_SELF_UPDATED=1
            exec bash "${SCRIPT_DIR}/lumenctl.sh" "$@"
            ;;
    esac
    lumenctl_load_modules
}

lumenctl_require_modules() {
    if lumenctl_load_modules; then
        return 0
    fi
    if lumenctl_repair_missing_modules "$@"; then
        return 0
    fi
    log_error "lumenctl 模块不完整；缺少 scripts/lumenctl/*.sh。"
    log_error "请执行 update-lumen 或 bootstrap-scripts 恢复完整运维脚本。"
    return 1
}

# 把任何菜单动作包成"失败也回菜单"的 action：
#   - 失败时 log_warn rc 并暂停等用户按 Enter（让他看清上方错误）
#   - 成功时直接返回，无暂停（避免 annoying）
#   - Ctrl+C / 中断（rc=130/143）等同失败处理
#   - 无论 rc 多少，本函数总返回 0，不让 set -e 把菜单进程也带退出
menu_action() {
    local rc=0
    "$@" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        log_warn "命令以非零状态结束（rc=${rc}），返回主菜单。"
        if [ -r /dev/tty ]; then
            printf '\n按 Enter 返回菜单... ' >&2
            IFS= read -r _ </dev/tty 2>/dev/null || true
            printf '\n' >&2
        fi
    fi
    return 0
}

show_menu() {
    while :; do
        cat <<EOF

Lumen 一键运维菜单
（菜单分组：运行/维护/网络/⚠ 危险）

  ── 运行（read-only）──
  1) 查看运行状态（compose ps + 健康检查）
  2) 跟随 API 日志（compose logs -f api）
  3) 查看 Lumen 版本

  ── 维护（compose 操作）──
  4) 启动 api/worker/web（compose up -d --wait）
  5) 重启 api/worker/web（compose up -d --force-recreate）
  6) 停止 api/worker/web/tgbot（compose stop）
  7) 执行 DB migrate（compose --profile migrate run --rm migrate）
  8) 立即触发一次 backup（pg + redis）

  ── 网络（nginx）──
  9) 扫描 nginx 配置
  10) nginx 反代优化向导

  ── ⚠ 危险（影响数据/在线服务）──
  11) 安装 Lumen
  12) 更新 Lumen
  13) Restore from backup（drop DB + 覆盖 redis）
  14) Rollback 到上一版本
  15) 安装 image-job
  16) 卸载 image-job
  17) 卸载 Lumen

  0) 退出

EOF
        local choice
        choice="$(read_or_default '请选择' '0')"
        case "${choice}" in
            1)  menu_action lumen_compose_status ;;
            2)  menu_action lumen_compose_logs api ;;
            3)  menu_action lumen_compose_version ;;
            4)  menu_action lumen_compose_start ;;
            5)  menu_action lumen_compose_restart ;;
            6)  menu_action lumen_compose_stop ;;
            7)  menu_action lumen_compose_migrate ;;
            8)  menu_action run_lumen_script backup.sh ;;
            9)  menu_action nginx_scan ;;
            10) menu_action nginx_optimize ;;
            11) menu_action run_lumen_install_script ;;
            12) menu_action run_lumen_script update.sh ;;
            13)
                local ts
                ts="$(read_or_default '请输入 backup timestamp（YYYYMMDD-HHMMSS）' '')"
                if [ -n "${ts}" ]; then
                    menu_action lumen_compose_restore "${ts}"
                else
                    log_warn "未输入 timestamp，已取消。"
                fi
                ;;
            14) menu_action lumen_compose_rollback ;;
            15) menu_action install_image_job ;;
            16) menu_action uninstall_image_job ;;
            17) menu_action run_lumen_script uninstall.sh ;;
            0)  exit 0 ;;
            *)  log_warn "无效选项：${choice}" ;;
        esac
    done
}

# 哪些子命令值得在执行前 self-update：
#   - 写命令 / 维护命令 / 菜单：是
#   - 纯查询（status/logs/version/help）：否，避免完全无副作用的查询都打外网
lumenctl_command_needs_self_update() {
    # 触发 self-update 的命令：实际会调用本地 scripts/* 或会写入持久数据的命令。
    # migrate / start / stop / restart / status / logs 是纯 docker compose 操作，
    # 跟 scripts 无关，不触发避免无意义打外网。
    case "$1" in
        menu|install-lumen|update-lumen|uninstall-lumen|rollback|backup|restore|bootstrap|migrate-env|migrate-env-apply|bootstrap-scripts)
            return 0
            ;;
    esac
    return 1
}

# lumenctl 入口处的 self-update：先把 GitHub branch 固定为 commit，再更新 SCRIPT_DIR；
# 走 TTL 缓存（默认 600s）避免菜单反复打开就反复拉；网络/校验失败 → WARN 继续。
# lumenctl.sh 或 lib.sh 自己变了 → re-exec lumenctl 让函数定义/逻辑生效。
lumenctl_maybe_self_update() {
    # source 模式（测试 / interactive shell 用 `. lumenctl.sh; main ...` 调用）跳过：
    # 否则 source 后调 main 会真去拉 GitHub + re-exec 替换当前 bash 进程，破坏测试。
    # 直接执行时 BASH_SOURCE[0] == "$0"（同为 lumenctl.sh 路径），source 时 $0 是 caller。
    if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
        return 0
    fi
    if [ "${LUMEN_LUMENCTL_SELF_UPDATED:-0}" = "1" ]; then
        return 0
    fi
    if [ "${LUMEN_LUMENCTL_SELF_UPDATE:-1}" = "0" ]; then
        return 0
    fi

    # 让用户看到自更新行为（之前完全静默，本地未提交改动被覆盖时用户感知是
    # "我刚改的代码神奇消失"）。需禁用：LUMEN_LUMENCTL_SELF_UPDATE=0。
    log_info "[self-update] 检查远端 scripts/ 更新（branch=${LUMEN_SELF_UPDATE_BRANCH:-main}, TTL=${LUMEN_SELF_UPDATE_TTL:-600}s）..."
    log_info "[self-update] 跳过本次更新：LUMEN_LUMENCTL_SELF_UPDATE=0 bash scripts/lumenctl.sh ..."

    lumenctl_sync_script_unit "${LUMEN_SELF_UPDATE_TTL:-600}"

    case " ${LUMEN_SELF_UPDATE_CHANGED:-} " in
        *" lumenctl.sh "*|*" lib.sh "*)
            log_info "[lumenctl] 核心脚本已更新（${LUMEN_SELF_UPDATE_CHANGED}），re-exec 新版。"
            export LUMEN_LUMENCTL_SELF_UPDATED=1
            exec bash "${SCRIPT_DIR}/lumenctl.sh" "$@"
            ;;
    esac
}

main() {
    local command="${1:-menu}"

    # 在 dispatch 之前拉一次最新脚本（带 TTL）。命令路由用原始 args，不要 shift。
    if lumenctl_command_needs_self_update "${command}"; then
        lumenctl_maybe_self_update "$@"
    fi

    case "${command}" in
        help|-h|--help|bootstrap-scripts|install-lumen|update-lumen|uninstall-lumen)
            ;;
        *)
            lumenctl_require_modules "$@"
            ;;
    esac

    shift || true
    case "${command}" in
        menu) show_menu ;;
        # Lifecycle：透传额外 args 给底层脚本，install.sh / update.sh 可识别 --flag
        install-lumen) run_lumen_install_script "$@" ;;
        update-lumen) run_lumen_script update.sh "$@" ;;
        uninstall-lumen) run_lumen_script uninstall.sh "$@" ;;
        rollback) lumen_compose_rollback "$@" ;;
        version) lumen_compose_version ;;
        # 应急：突破 TTL 强拉 scripts/（"我刚改了 scripts，想立刻生效"）
        bootstrap-scripts)
            LUMEN_SELF_UPDATE_FORCE=1 lumenctl_sync_script_unit 0
            case "${LUMEN_SELF_UPDATE_RESULT:-}" in
                ok)
                    if [ -n "${LUMEN_SELF_UPDATE_CHANGED:-}" ]; then
                        log_info "[bootstrap-scripts] 已更新：${LUMEN_SELF_UPDATE_CHANGED}（备份 *.bak.${LUMEN_SELF_UPDATE_BACKUP_TS}）。"
                    else
                        log_info "[bootstrap-scripts] 远端与本地一致，无需替换。"
                    fi
                    ;;
                failed)   log_error "[bootstrap-scripts] 拉取失败，详见上方 WARN。"; exit 1 ;;
                disabled) log_warn "[bootstrap-scripts] 已通过 LUMEN_SELF_UPDATE=0 全局关闭。" ;;
                *)        log_info "[bootstrap-scripts] 跳过（${LUMEN_SELF_UPDATE_RESULT:-unknown}）。" ;;
            esac
            ;;
        # Docker compose runtime
        status) lumen_compose_status ;;
        logs) lumen_compose_logs "${1:-api}" ;;
        start) lumen_compose_start ;;
        stop) lumen_compose_stop ;;
        restart) lumen_compose_restart ;;
        migrate) lumen_compose_migrate ;;
        bootstrap) lumen_compose_bootstrap ;;
        migrate-env) lumen_env_migrate_file --dry-run "$@" ;;
        migrate-env-apply) lumen_env_migrate_file --apply "$@" ;;
        backup) lumen_compose_backup "$@" ;;
        restore) lumen_compose_restore "$@" ;;
        # Auxiliary（保留，不再适用 docker 时由内部函数自行报错）
        install-storage-units) install_storage_units ;;
        install-image-job) install_image_job ;;
        uninstall-image-job) uninstall_image_job ;;
        nginx-scan) nginx_scan ;;
        nginx-optimize) nginx_optimize ;;
        nginx-lumen) nginx_lumen_proxy ;;
        nginx-sub2api) nginx_sub2api_proxy ;;
        nginx-sub2api-inner) nginx_sub2api_inner_proxy ;;
        nginx-sub2api-outer) nginx_sub2api_outer_proxy ;;
        nginx-image-job) nginx_image_job_locations ;;
        help|-h|--help) usage ;;
        *)
            usage
            log_error "未知命令：${command}"
            exit 1
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
else
    # Sourcing remains a supported testing/interactive API. Do not force a
    # network repair here because that would mutate the caller's shell.
    lumenctl_load_modules || \
        log_warn "[lumenctl] source 模式未加载拆分模块；仅引导命令可用。"
fi
