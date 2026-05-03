#!/usr/bin/env bash
# Lumen 卸载脚本（Docker compose 版）
# 用法：
#   bash scripts/uninstall.sh                  # 交互式：停容器、保留数据
#   bash scripts/uninstall.sh --purge          # 同时删除持久化数据目录
#   bash scripts/uninstall.sh --disable-systemd  # 自动 disable 旧 lumen-* systemd 服务
#   LUMEN_UNINSTALL_NONINTERACTIVE=1 bash scripts/uninstall.sh  # 跳过所有确认
#   LUMEN_UNINSTALL_PURGE=1 bash scripts/uninstall.sh           # 同 --purge
#
# 行为（与 docker-full-stack-cutover-plan.md §17.9 / §24 对齐）：
#   1. 安全确认（NONINTERACTIVE=1 跳过）
#   2. docker compose down --remove-orphans（含 tgbot profile）
#   3. 询问/通过 flag 决定是否 down -v + 删持久化数据目录（purge）
#   4. 检测到旧 lumen-* systemd unit 仍 enabled 时给出提示；可选 --disable-systemd 自动禁用
#   5. purge 时清理 .update.path / .update-runner 单元（仅当显式 purge）
#   6. 输出汇总
#
# 源代码本身不会被删除，可随时重新安装。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

trap 'log_error "卸载脚本失败：第 ${LINENO} 行返回非零状态。手动检查后再重试。"' ERR

ROOT="$(lumen_resolve_repo_root "${SCRIPT_DIR}")"

# ---------------------------------------------------------------------------
# Args / env（在 acquire_lock 之前解析，避免 --help 也要等锁）
# ---------------------------------------------------------------------------
LUMEN_UNINSTALL_PURGE="${LUMEN_UNINSTALL_PURGE:-0}"
LUMEN_UNINSTALL_NONINTERACTIVE="${LUMEN_UNINSTALL_NONINTERACTIVE:-0}"
LUMEN_UNINSTALL_DISABLE_SYSTEMD="${LUMEN_UNINSTALL_DISABLE_SYSTEMD:-0}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --purge)
            LUMEN_UNINSTALL_PURGE=1
            ;;
        --disable-systemd)
            LUMEN_UNINSTALL_DISABLE_SYSTEMD=1
            ;;
        --noninteractive|--non-interactive|--yes|-y)
            LUMEN_UNINSTALL_NONINTERACTIVE=1
            ;;
        -h|--help)
            cat <<USAGE
Lumen 卸载脚本

  bash scripts/uninstall.sh [选项]

选项：
  --purge                同时删除数据卷与持久化数据目录
  --disable-systemd      自动 disable 旧 lumen-* systemd unit
  --yes / --noninteractive  跳过所有交互确认
  -h / --help            显示本帮助

环境变量：
  LUMEN_UNINSTALL_NONINTERACTIVE=1  同 --yes
  LUMEN_UNINSTALL_PURGE=1           同 --purge
  LUMEN_DEPLOY_ROOT=/opt/lumen      compose 工作目录的备选路径
  LUMEN_DATA_ROOT=/opt/lumendata    文件/备份父目录
  LUMEN_DB_ROOT=\$LUMEN_DATA_ROOT     PostgreSQL/Redis 父目录
USAGE
            exit 0
            ;;
        *)
            log_warn "未知参数：$1（已忽略）"
            ;;
    esac
    shift
done

lumen_install_signal_handlers
lumen_acquire_lock "${ROOT}" "uninstall.sh"
cd "${ROOT}"
log_info "项目根目录：${ROOT}"

LUMEN_DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
LUMEN_DATA_ROOT="${LUMEN_DATA_ROOT:-/opt/lumendata}"
LUMEN_DB_ROOT="${LUMEN_DB_ROOT:-${LUMEN_DATA_ROOT}}"
LUMEN_COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-lumen}"
export COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT}"

# 旧 systemd unit 列表：仅用于检测/提示（除非 --disable-systemd），不再自动删除单元文件。
LUMEN_LEGACY_SYSTEMD_UNITS=(
    lumen-api.service
    lumen-worker.service
    lumen-web.service
    lumen-tgbot.service
)
# 与一键更新相关的 path/runner unit；仅在 --purge 时清理。
LUMEN_LEGACY_UPDATE_UNITS=(
    lumen-update.path
    lumen-update-runner.service
    lumen-health-watchdog.timer
    lumen-health-watchdog.service
    lumen-backup.timer
    lumen-backup.service
)

LUMEN_NGINX_ACTIVE_DIRS=(
    /etc/nginx/sites-enabled
    /etc/nginx/conf.d
    /www/server/panel/vhost/nginx
    /usr/local/etc/nginx/servers
    /usr/local/etc/nginx/conf.d
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# lumen_uninstall_compose_workdir：定位 docker-compose.yml 所在目录。
# 优先 ${ROOT}/current（release 布局），否则 ${LUMEN_DEPLOY_ROOT}/current，否则 ROOT。
# lib.sh 已提供 lumen_compose / lumen_compose_in（同 wave 加入）；这里只负责挑目录。
lumen_uninstall_compose_workdir() {
    if [ -d "${ROOT}/current" ]; then
        printf '%s' "${ROOT}/current"
        return 0
    fi
    if [ -d "${LUMEN_DEPLOY_ROOT}/current" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}/current"
        return 0
    fi
    if [ -f "${ROOT}/docker-compose.yml" ]; then
        printf '%s' "${ROOT}"
        return 0
    fi
    if [ -f "${LUMEN_DEPLOY_ROOT}/docker-compose.yml" ]; then
        printf '%s' "${LUMEN_DEPLOY_ROOT}"
        return 0
    fi
    return 1
}

# 给本脚本的本地包装：找到 workdir 后调 lib.sh 的 lumen_compose_in。
lumen_uninstall_compose() {
    local workdir
    if ! workdir="$(lumen_uninstall_compose_workdir)"; then
        log_error "找不到 docker-compose.yml；预期位置：${ROOT}/current 或 ${LUMEN_DEPLOY_ROOT}/current"
        return 1
    fi
    lumen_compose_in "${workdir}" "$@"
}

lumen_uninstall_join() {
    local sep="${1:- }"
    shift || true
    local out="" item
    for item in "$@"; do
        if [ -z "${out}" ]; then
            out="${item}"
        else
            out="${out}${sep}${item}"
        fi
    done
    printf '%s' "${out}"
}

lumen_uninstall_safe_name() {
    local value="$1"
    value="${value//\//_}"
    value="$(printf '%s' "${value}" | tr '[:space:]' '_' | tr -c 'A-Za-z0-9_.-' '-')"
    printf '%s' "${value:-nginx.conf}"
}

lumen_uninstall_collect_nginx_candidates() {
    local dir file seen=" "
    for dir in "${LUMEN_NGINX_ACTIVE_DIRS[@]}"; do
        [ -d "${dir}" ] || continue
        while IFS= read -r file; do
            [ -n "${file}" ] || continue
            case "${seen}" in
                *" ${file} "*) continue ;;
            esac
            seen="${seen}${file} "
            if grep -Iq . "${file}" 2>/dev/null \
                && grep -Eq 'Managed by scripts/lumenctl\.sh|Lumen reverse proxy|upstream[[:space:]]+lumen_web|lumen_web_|Lumen 反代|proxy_pass[[:space:]]+http://(127\.0\.0\.1|localhost):3000|server[[:space:]]+(127\.0\.0\.1|localhost):3000' "${file}" 2>/dev/null; then
                printf '%s\n' "${file}"
            fi
        done < <(find "${dir}" -maxdepth 1 \( -type f -o -type l \) -print 2>/dev/null | sort)
    done
}

lumen_uninstall_disable_nginx_configs() {
    if ! command -v nginx >/dev/null 2>&1; then
        KEPT+=("nginx 未检测到，未处理反代配置")
        return 0
    fi

    local candidates=()
    local file
    while IFS= read -r file; do
        [ -n "${file}" ] && candidates+=("${file}")
    done < <(lumen_uninstall_collect_nginx_candidates)

    if [ "${#candidates[@]}" -eq 0 ]; then
        KEPT+=("未发现启用中的 Lumen nginx 配置")
        return 0
    fi

    log_step "nginx 反代配置"
    log_warn "发现疑似仍在生效的 Lumen nginx 配置："
    for file in "${candidates[@]}"; do
        printf '         %s\n' "${file}"
    done
    log_warn "卸载会自动禁用这些配置；原文件会先移入备份目录，避免域名继续指向 Lumen。"

    if ! lumen_run_as_root nginx -t >/dev/null 2>&1; then
        log_error "当前 nginx -t 未通过。为避免扩大故障，跳过自动禁用 nginx 配置。"
        log_error "请先修复 nginx 配置，或手动移除上方 Lumen 配置后 reload nginx。"
        KEPT+=("nginx 反代配置未改动（nginx -t 当前不通过）")
        return 0
    fi

    local timestamp backup_dir dest
    timestamp="$(date -u +%Y%m%d%H%M%S 2>/dev/null || date +%s)"
    backup_dir="${LUMEN_NGINX_DISABLED_DIR:-/var/backups/lumenctl/nginx-disabled}/${timestamp}"
    if ! lumen_run_as_root mkdir -p "${backup_dir}"; then
        log_error "无法创建 nginx 禁用备份目录：${backup_dir}"
        KEPT+=("nginx 反代配置未改动（无法创建备份目录）")
        return 0
    fi

    local moved_src=()
    local moved_dst=()
    for file in "${candidates[@]}"; do
        [ -e "${file}" ] || [ -L "${file}" ] || continue
        dest="${backup_dir}/$(lumen_uninstall_safe_name "${file}")"
        if lumen_run_as_root mv -f "${file}" "${dest}"; then
            log_info "已移出 nginx 配置 ${file} -> ${dest}"
            moved_src+=("${file}")
            moved_dst+=("${dest}")
        else
            log_warn "无法移动 ${file}，已跳过。"
        fi
    done

    if [ "${#moved_dst[@]}" -eq 0 ]; then
        KEPT+=("nginx 反代配置未改动（没有文件被移动）")
        return 0
    fi

    if ! lumen_run_as_root nginx -t >/dev/null 2>&1; then
        log_error "禁用后 nginx -t 未通过，正在回滚 nginx 配置。"
        local i
        for i in "${!moved_dst[@]}"; do
            lumen_run_as_root mv -f "${moved_dst[$i]}" "${moved_src[$i]}" || true
        done
        lumen_run_as_root nginx -t >/dev/null 2>&1 || true
        KEPT+=("nginx 反代配置已回滚（禁用后 nginx -t 未通过）")
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if lumen_systemctl reload nginx; then
            DONE+=("已禁用 Lumen nginx 配置并 reload nginx（备份：${backup_dir}）")
        else
            log_warn "nginx 配置已禁用且 nginx -t 通过，但 reload 失败。请手动执行：sudo systemctl reload nginx"
            DONE+=("已禁用 Lumen nginx 配置（备份：${backup_dir}，reload 需手动确认）")
        fi
    elif lumen_run_as_root nginx -s reload; then
        DONE+=("已禁用 Lumen nginx 配置并 reload nginx（备份：${backup_dir}）")
    else
        log_warn "nginx 配置已禁用且 nginx -t 通过，但 reload 失败。请手动执行：sudo nginx -s reload"
        DONE+=("已禁用 Lumen nginx 配置（备份：${backup_dir}，reload 需手动确认）")
    fi
}

lumen_uninstall_compose_down() {
    if [ "${DOCKER_AVAILABLE}" -ne 1 ]; then
        log_warn "未检测到 docker / docker compose v2，跳过 compose down。"
        KEPT+=("容器状态未变（无 docker 命令）")
        return 0
    fi

    log_step "停止 Docker 栈（docker compose down --remove-orphans）"
    log_info "compose project：${LUMEN_COMPOSE_PROJECT}"
    if lumen_uninstall_compose down --remove-orphans; then
        DONE+=("已 docker compose down 主栈（含 --remove-orphans）")
    else
        log_warn "docker compose down 失败，稍后会强删残留容器。"
        KEPT+=("docker compose down 失败（详见上方日志）")
    fi

    # tgbot 走 profile，单独 down 一次确保即便没启动也不会留下编排状态。
    if lumen_uninstall_compose --profile tgbot down --remove-orphans >/dev/null 2>&1; then
        DONE+=("已 docker compose --profile tgbot down")
    fi
}

lumen_uninstall_force_remove_containers() {
    [ "${DOCKER_AVAILABLE}" -eq 1 ] || return 0
    local cnames=(lumen-api lumen-worker lumen-web lumen-tgbot lumen-pg lumen-redis)
    local removed=()
    local cn
    for cn in "${cnames[@]}"; do
        if lumen_docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${cn}"; then
            if lumen_docker rm -f "${cn}" >/dev/null 2>&1; then
                removed+=("${cn}")
                log_info "已强删残留容器 ${cn}"
            else
                log_warn "无法删除残留容器 ${cn}，请手动 'docker rm -f ${cn}'"
            fi
        fi
    done
    if [ "${#removed[@]}" -gt 0 ]; then
        DONE+=("已强删残留容器：$(lumen_uninstall_join ', ' "${removed[@]}")")
    fi
}

lumen_uninstall_purge_data_volumes() {
    if [ "${DOCKER_AVAILABLE}" -ne 1 ]; then
        log_warn "未检测到 docker，跳过 down -v。"
        KEPT+=("数据卷未删除（无可用 docker 命令）")
        return 0
    fi
    log_step "删除 Docker 数据卷（docker compose down -v）"
    if lumen_uninstall_compose down -v --remove-orphans; then
        DONE+=("已执行 docker compose down -v")
    else
        log_warn "docker compose down -v 失败，请手动检查 'docker volume ls | grep lumen'"
        KEPT+=("docker compose down -v 失败（请手动 docker volume rm <name>）")
    fi
}

lumen_uninstall_purge_data_dirs() {
    log_step "删除持久化数据目录"
    local targets=()
    local seen=" "
    local candidate
    for candidate in "${LUMEN_DATA_ROOT}" "${LUMEN_DB_ROOT}"; do
        [ -n "${candidate}" ] || continue
        case "${seen}" in
            *" ${candidate} "*) continue ;;
        esac
        seen="${seen}${candidate} "
        targets+=("${candidate}")
    done
    local d
    for d in "${targets[@]}"; do
        if [ -e "${d}" ] || [ -L "${d}" ]; then
            log_warn "即将删除 ${d}（持久化数据目录，操作不可恢复）"
            # lumen_safe_rm_rf_as_root 内部用 lumen_path_safe_for_rm 拦截 / /usr /opt 等系统目录
            if lumen_safe_rm_rf_as_root "${d}"; then
                log_info "已删除 ${d}"
                DONE+=("已删除 ${d}")
            else
                log_warn "无法删除 ${d}（路径校验失败或 rm 失败），请手动确认。"
                KEPT+=("${d} 删除失败")
            fi
        fi
    done
}

# 清理 release 布局产物。union(ROOT, LUMEN_DEPLOY_ROOT) — 应对从源仓库或部署目录运行的差异。
# 源仓库执行（ROOT=/home/x/Lumen 但 LUMEN_DEPLOY_ROOT=/opt/lumen）时，两边都要清理才彻底。
lumen_uninstall_purge_deploy_dirs() {
    log_step "清理仓库 deploy 目录（release 布局：releases / current / shared）"
    local roots=()
    local seen=" "
    local r
    for r in "${ROOT}" "${LUMEN_DEPLOY_ROOT}"; do
        [ -n "${r}" ] || continue
        case "${seen}" in
            *" ${r} "*) continue ;;
        esac
        seen="${seen}${r} "
        # 只处理"看起来像 release 布局"的目录，避免误清理无关源仓库
        if [ -L "${r}/current" ] || [ -d "${r}/releases" ] || [ -d "${r}/shared" ]; then
            roots+=("${r}")
        fi
    done

    if [ "${#roots[@]}" -eq 0 ]; then
        log_info "未检测到 release 布局，跳过清理。"
        return 0
    fi

    local target d
    for r in "${roots[@]}"; do
        # current / previous symlink
        for target in "${r}/current" "${r}/previous"; do
            if [ -L "${target}" ]; then
                if rm -f "${target}" 2>/dev/null \
                        || lumen_run_as_root rm -f "${target}" 2>/dev/null; then
                    DONE+=("已删除 ${target} symlink")
                else
                    log_warn "无法删除 symlink ${target}"
                    KEPT+=("${target} 删除失败")
                fi
            fi
        done
        # 目录类（releases / shared / 锁目录）
        for d in "${r}/releases" "${r}/shared" "${r}/.lumen-maintenance.lock.d" "${r}/.lumen-maintenance.lock"; do
            if [ -e "${d}" ] || [ -L "${d}" ]; then
                if lumen_safe_rm_rf_as_root "${d}"; then
                    log_info "已删除 ${d}"
                    DONE+=("已删除 ${d}")
                else
                    log_warn "无法删除 ${d}（路径校验失败或权限不足），请手动 sudo rm -rf '${d}'"
                    KEPT+=("${d} 删除失败")
                fi
            fi
        done
    done
}

lumen_uninstall_systemd_compat() {
    if ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi

    local enabled=()
    local unit state
    for unit in "${LUMEN_LEGACY_SYSTEMD_UNITS[@]}"; do
        # 不强求 has_unit；list-unit-files 直接看是否 enabled。
        state="$(systemctl is-enabled "${unit}" 2>/dev/null || true)"
        case "${state}" in
            enabled|enabled-runtime|alias|static|linked|linked-runtime) enabled+=("${unit}") ;;
        esac
    done

    if [ "${#enabled[@]}" -eq 0 ]; then
        return 0
    fi

    log_step "检测到旧 lumen-* systemd 服务"
    log_warn "以下 unit 仍处于启用状态：$(lumen_uninstall_join ', ' "${enabled[@]}")"

    if [ "${LUMEN_UNINSTALL_DISABLE_SYSTEMD}" = "1" ]; then
        log_info "按 --disable-systemd 自动禁用上述 unit。"
        if lumen_systemctl disable --now "${enabled[@]}" >/dev/null 2>&1; then
            DONE+=("已 disable --now 旧 systemd unit：$(lumen_uninstall_join ', ' "${enabled[@]}")")
        else
            log_warn "disable --now 返回非零，请手动确认。"
            KEPT+=("旧 systemd unit disable 失败（请手动处理）")
        fi
    else
        cat <<TIP

  检测到旧 systemd 服务仍 enabled。如确认不再回滚到 systemd 部署，可执行：
    sudo systemctl disable --now $(lumen_uninstall_join ' ' "${enabled[@]}")

  当前默认保留 systemd unit 文件以便回滚（参考 cutover plan §18.2）。
  如要本脚本自动 disable，可加 --disable-systemd 后重跑。

TIP
        KEPT+=("旧 systemd unit 仍 enabled（提示用户手动处理）：$(lumen_uninstall_join ', ' "${enabled[@]}")")
    fi
}

lumen_uninstall_purge_update_units() {
    if ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    local unit removed=()
    for unit in "${LUMEN_LEGACY_UPDATE_UNITS[@]}"; do
        if lumen_systemd_has_unit "${unit}"; then
            lumen_systemctl disable --now "${unit}" >/dev/null 2>&1 || true
            removed+=("${unit}")
        fi
        local path="/etc/systemd/system/${unit}"
        if [ -e "${path}" ] || [ -L "${path}" ]; then
            lumen_run_as_root rm -f "${path}" || log_warn "无法删除 ${path}"
        fi
    done
    if [ "${#removed[@]}" -gt 0 ]; then
        lumen_systemctl daemon-reload >/dev/null 2>&1 || true
        DONE+=("已 disable 并清理一键更新 unit：$(lumen_uninstall_join ', ' "${removed[@]}")")
    fi
}

# ---------------------------------------------------------------------------
# Phase A：安全确认
# ---------------------------------------------------------------------------
log_step "Lumen 卸载向导"
cat <<EOF

  本向导将分步进行：
    1) docker compose down --remove-orphans（同时停 tgbot profile）
    2) 检测旧 systemd 部署并给出提示（--disable-systemd 时自动禁用）
    3) 默认保留 storage/backup=${LUMEN_DATA_ROOT} 与 postgres/redis=${LUMEN_DB_ROOT}
       传入 --purge 或 LUMEN_UNINSTALL_PURGE=1 时才会删除
    4) --purge 时同步清理仓库 release 目录与一键更新 systemd unit

  源代码与 docker-compose.yml 不会被删除；删除项目目录请手动 rm。

EOF

if [ "${LUMEN_UNINSTALL_NONINTERACTIVE}" != "1" ]; then
    if ! confirm "确认开始卸载？"; then
        log_info "用户取消，未做任何修改。"
        exit 0
    fi
fi

# 跟踪做了什么/没做什么，最后汇总。
declare -a DONE=()
declare -a KEPT=()
DOCKER_AVAILABLE=0
if lumen_detect_docker_access; then
    DOCKER_AVAILABLE=1
    if [ "${LUMEN_DOCKER_USE_SUDO:-0}" = "1" ]; then
        log_warn "当前用户无法直接访问 Docker，本次将自动使用 sudo docker。"
    fi
fi

# ---------------------------------------------------------------------------
# Phase B：docker compose down
# ---------------------------------------------------------------------------
lumen_uninstall_compose_down
lumen_uninstall_force_remove_containers

# ---------------------------------------------------------------------------
# Phase D：旧 systemd 兼容提示（详见 §17.9 / §18.2）
#   - 默认仅提示，不自动 disable，方便回滚 systemd 部署
#   - --disable-systemd / LUMEN_UNINSTALL_DISABLE_SYSTEMD=1 才会执行 disable --now
# ---------------------------------------------------------------------------
lumen_uninstall_systemd_compat
lumen_uninstall_disable_nginx_configs

# ---------------------------------------------------------------------------
# Phase C：是否删数据（purge）
# ---------------------------------------------------------------------------
PURGE_DECIDED=0
if [ "${LUMEN_UNINSTALL_PURGE}" = "1" ]; then
    PURGE_DECIDED=1
elif [ "${LUMEN_UNINSTALL_NONINTERACTIVE}" = "1" ]; then
    PURGE_DECIDED=0
elif confirm "是否同时删除数据卷与持久化数据目录（不可恢复）？"; then
    PURGE_DECIDED=1
fi

if [ "${PURGE_DECIDED}" = "1" ]; then
    # purge 必须二次确认，除非 NONINTERACTIVE+PURGE 同时设置（CI / 自动化场景）
    PURGE_OK=1
    if [ "${LUMEN_UNINSTALL_NONINTERACTIVE}" != "1" ]; then
        log_warn "purge 将删除：所有 docker 卷、storage/backup=${LUMEN_DATA_ROOT}、postgres/redis=${LUMEN_DB_ROOT}"
        if ! confirm "再次确认 purge？此操作不可恢复"; then
            PURGE_OK=0
            log_info "已取消 purge，仅停止容器、保留数据。"
            KEPT+=("用户取消 purge：保留 ${LUMEN_DATA_ROOT} / ${LUMEN_DB_ROOT} / 数据卷")
        fi
    fi

    if [ "${PURGE_OK}" = "1" ]; then
        lumen_uninstall_purge_data_volumes
        lumen_uninstall_purge_data_dirs
        lumen_uninstall_purge_deploy_dirs
        lumen_uninstall_purge_update_units
    fi
else
    KEPT+=("数据卷与 ${LUMEN_DATA_ROOT} / ${LUMEN_DB_ROOT} 保留（下次 install 直接复用）")
    KEPT+=("一键更新 systemd unit 保留（仅 --purge 时清理）")
fi

# ---------------------------------------------------------------------------
# Phase F：汇总
# ---------------------------------------------------------------------------
log_step "卸载总结"
printf '\n  已执行：\n'
if [ "${#DONE[@]}" -eq 0 ]; then
    printf '    （无）\n'
else
    for item in "${DONE[@]}"; do
        printf '    - %s\n' "${item}"
    done
fi

printf '\n  已保留：\n'
if [ "${#KEPT[@]}" -eq 0 ]; then
    printf '    （无）\n'
else
    for item in "${KEPT[@]}"; do
        printf '    - %s\n' "${item}"
    done
fi

cat <<EOF

  源代码仍在 ${ROOT}，docker-compose.yml / pyproject.toml 等配置未删。
  如需彻底移除：手动 'rm -rf ${ROOT}'（请先确认无未保存的数据）。

  重新安装：
    bash scripts/lumenctl.sh install-lumen
    # 或
    bash scripts/install.sh

EOF

trap - ERR
exit 0
