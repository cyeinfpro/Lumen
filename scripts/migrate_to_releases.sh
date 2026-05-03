#!/usr/bin/env bash
# 一次性迁移脚本：把 in-place 部署的 /opt/lumen/<apps,scripts,...>
# 转换为 Capistrano 风格 release + symlink 布局。
#
# 用法：
#   sudo bash scripts/migrate_to_releases.sh
#
# 行为：
#   1. 幂等：检测 current 是否已经是 symlink，是则直接退出 0
#   2. systemctl stop lumen-tgbot lumen-web lumen-worker lumen-api
#   3. 用 /opt/lumen.tmp 作为中转，把 /opt/lumen 当前内容（除 .env）平移到
#      /opt/lumen.tmp/releases/initial/ 下，再 mv 回 /opt/lumen
#   4. 在 /opt/lumen/current 建立指向 releases/initial 的软链
#   5. 把 .env.local / worker var / .next/cache 移入 shared/ 并回链
#   6. 复制最新 systemd unit 到 /etc/systemd/system/，daemon-reload
#   7. systemctl start lumen-api lumen-worker lumen-web lumen-tgbot
#
# 输出普通 echo 信息，不使用 ::lumen-step:: 协议（迁移由人工执行，不进 SSE）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

ROOT="${LUMEN_ROOT:-/opt/lumen}"
TMP_ROOT="${ROOT}.tmp"
INITIAL_ID="${LUMEN_MIGRATION_INITIAL_ID:-initial}"

# 必须 root（或 ROOT 已经是当前用户写得动的）。
require_root_or_writable() {
    if [ "${EUID:-$(id -u)}" -eq 0 ]; then
        return 0
    fi
    if [ -w "${ROOT}" ] && [ -w "$(dirname "${ROOT}")" ]; then
        return 0
    fi
    log_error "需要 root 权限或对 ${ROOT} 及其父目录的写入权限。请用 sudo 重跑。"
    exit 1
}

# 幂等：current 已经是 symlink 就直接退出。
check_idempotent() {
    if [ -L "${ROOT}/current" ]; then
        log_info "${ROOT}/current 已经是 symlink，迁移已完成；幂等退出。"
        exit 0
    fi
    if [ ! -d "${ROOT}" ]; then
        log_error "${ROOT} 不存在或不是目录，无法迁移。"
        exit 1
    fi
    if [ -e "${TMP_ROOT}" ]; then
        log_error "中转目录 ${TMP_ROOT} 已存在；上次迁移可能未完成。请手动检查后删除该目录再重跑。"
        exit 1
    fi
}

stop_services() {
    log_info "停止 lumen 服务（lumen-tgbot, lumen-web, lumen-worker, lumen-api）"
    if command -v systemctl >/dev/null 2>&1; then
        # 顺序无要求，stop 全部即可；忽略未安装的 unit。
        for u in lumen-tgbot.service lumen-web.service lumen-worker.service lumen-api.service; do
            if systemctl list-unit-files "${u}" --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "${u}"; then
                systemctl stop "${u}" 2>/dev/null || log_warn "stop ${u} 失败（忽略，继续迁移）"
            fi
        done
    else
        log_warn "未发现 systemctl，跳过 stop 步骤（请确认服务未在运行）。"
    fi
}

move_to_release() {
    log_info "创建中转目录：${TMP_ROOT}"
    mkdir -p "${TMP_ROOT}/releases" "${TMP_ROOT}/shared"

    log_info "把 ${ROOT} 当前内容（除 .env）移到 ${TMP_ROOT}/releases/${INITIAL_ID}/"
    mkdir -p "${TMP_ROOT}/releases/${INITIAL_ID}"

    # 用 find -mindepth 1 -maxdepth 1 列出所有顶层条目（含点开头），逐个移动。
    # 跳过 .env（共享配置，迁移后挂在 ${ROOT}/.env）。
    local entry name
    while IFS= read -r entry; do
        name="$(basename "${entry}")"
        case "${name}" in
            ''|'.'|'..'|'.env') continue ;;
        esac
        mv "${entry}" "${TMP_ROOT}/releases/${INITIAL_ID}/${name}"
    done < <(find "${ROOT}" -mindepth 1 -maxdepth 1 \( -type d -o -type f -o -type l \) 2>/dev/null)

    # 把 .env 单独搬到 ${TMP_ROOT}/.env
    if [ -f "${ROOT}/.env" ]; then
        mv "${ROOT}/.env" "${TMP_ROOT}/.env"
    fi

    log_info "把中转目录内容回填到 ${ROOT}"
    # 此时 ${ROOT} 已空。把 ${TMP_ROOT} 下的 releases / shared / .env 移过来。
    mv "${TMP_ROOT}/releases" "${ROOT}/releases"
    mv "${TMP_ROOT}/shared" "${ROOT}/shared"
    if [ -f "${TMP_ROOT}/.env" ]; then
        mv "${TMP_ROOT}/.env" "${ROOT}/.env"
    fi
    rmdir "${TMP_ROOT}" 2>/dev/null || rm -rf "${TMP_ROOT}" 2>/dev/null || true
}

create_current_symlink() {
    log_info "创建 ${ROOT}/current -> releases/${INITIAL_ID}"
    ln -s "releases/${INITIAL_ID}" "${ROOT}/current"
}

# 把 release 内的 .env.local / worker/var / web/.next/cache 搬到 shared/，再在 release 内回链。
# 三条独立处理，缺失的源直接跳过。
extract_to_shared() {
    local rdir="${ROOT}/releases/${INITIAL_ID}"

    # apps/web/.env.local -> shared/web-env/.env.local
    if [ -f "${rdir}/apps/web/.env.local" ]; then
        log_info "把 apps/web/.env.local 移入 shared/web-env/"
        mkdir -p "${ROOT}/shared/web-env"
        mv "${rdir}/apps/web/.env.local" "${ROOT}/shared/web-env/.env.local"
        ln -s "${ROOT}/shared/web-env/.env.local" "${rdir}/apps/web/.env.local"
    else
        log_warn "${rdir}/apps/web/.env.local 不存在，跳过（部署时再写入 shared/web-env/）"
        mkdir -p "${ROOT}/shared/web-env"
    fi

    # apps/worker/var -> shared/worker-var
    if [ -d "${rdir}/apps/worker/var" ]; then
        log_info "把 apps/worker/var 移入 shared/worker-var/"
        # shared/worker-var 必须先不存在或为空，否则 mv 不会原子合并目录
        if [ -d "${ROOT}/shared/worker-var" ]; then
            # 已存在（理论上不应该）：保留现有 shared，把 release 内的备份起来。
            log_warn "shared/worker-var 已存在，备份 release 内的目录到 .pre-migrate"
            mv "${rdir}/apps/worker/var" "${rdir}/apps/worker/var.pre-migrate.$(date -u +%Y%m%d%H%M%S)" || true
        else
            mv "${rdir}/apps/worker/var" "${ROOT}/shared/worker-var"
        fi
        ln -s "${ROOT}/shared/worker-var" "${rdir}/apps/worker/var"
    else
        log_warn "${rdir}/apps/worker/var 不存在，创建空 shared/worker-var/ 并回链"
        mkdir -p "${ROOT}/shared/worker-var"
        mkdir -p "${rdir}/apps/worker"
        ln -s "${ROOT}/shared/worker-var" "${rdir}/apps/worker/var"
    fi

    # apps/web/.next/cache -> shared/web-next-cache
    if [ -d "${rdir}/apps/web/.next/cache" ]; then
        log_info "把 apps/web/.next/cache 移入 shared/web-next-cache/"
        if [ -d "${ROOT}/shared/web-next-cache" ]; then
            log_warn "shared/web-next-cache 已存在，备份 release 内的目录到 .pre-migrate"
            mv "${rdir}/apps/web/.next/cache" "${rdir}/apps/web/.next/cache.pre-migrate.$(date -u +%Y%m%d%H%M%S)" || true
        else
            mv "${rdir}/apps/web/.next/cache" "${ROOT}/shared/web-next-cache"
        fi
        ln -s "${ROOT}/shared/web-next-cache" "${rdir}/apps/web/.next/cache"
    else
        log_warn "${rdir}/apps/web/.next/cache 不存在，创建空 shared/web-next-cache/ 并回链"
        mkdir -p "${ROOT}/shared/web-next-cache"
        mkdir -p "${rdir}/apps/web/.next"
        ln -s "${ROOT}/shared/web-next-cache" "${rdir}/apps/web/.next/cache"
    fi

    # docker compose 用的根 .env：搬到 shared/.env，并在 ROOT 留软链兜底。
    # 这样 update.sh 的 link_shared 能把它链入新 release（compose 在 release
    # 目录下找 .env 时不会再缺 DB_USER/REDIS_PASSWORD）。
    if [ -f "${ROOT}/.env" ] && [ ! -L "${ROOT}/.env" ]; then
        log_info "把 ${ROOT}/.env 移入 shared/.env 并在 ROOT 保留软链"
        if [ -f "${ROOT}/shared/.env" ]; then
            log_warn "shared/.env 已存在，备份 ROOT/.env 到 .pre-migrate"
            mv "${ROOT}/.env" "${ROOT}/.env.pre-migrate.$(date -u +%Y%m%d%H%M%S)" || true
        else
            mv "${ROOT}/.env" "${ROOT}/shared/.env"
        fi
        ln -s "shared/.env" "${ROOT}/.env"
    elif [ ! -e "${ROOT}/shared/.env" ] && [ -L "${ROOT}/.env" ]; then
        log_warn "${ROOT}/.env 是软链但 shared/.env 不存在；请人工检查 .env 配置。"
    fi
}

# 复制最新 systemd unit 到 /etc/systemd/system/ 并 daemon-reload。
# 注意：unit 路径已经改为 /opt/lumen/current/...，这是迁移完成后才生效的路径。
deploy_systemd_units() {
    if ! command -v systemctl >/dev/null 2>&1; then
        log_warn "未检测到 systemctl，跳过 systemd unit 安装。请手动复制 deploy/systemd/*.service。"
        return 0
    fi
    local src_dir="${ROOT}/current/deploy/systemd"
    if [ ! -d "${src_dir}" ]; then
        log_warn "找不到 ${src_dir}，无法复制 systemd unit。请手工部署。"
        return 0
    fi
    log_info "复制最新 systemd unit 到 /etc/systemd/system/"
    local f
    for f in lumen-api.service lumen-web.service lumen-worker.service \
             lumen-tgbot.service lumen-update-runner.service \
             lumen-update.path lumen-backup.service lumen-backup.timer \
             lumen-health-watchdog.service lumen-health-watchdog.timer; do
        if [ -f "${src_dir}/${f}" ]; then
            cp -f "${src_dir}/${f}" "/etc/systemd/system/${f}"
        fi
    done
    systemctl daemon-reload
    log_info "systemctl daemon-reload 完成"
}

# 修正 ownership：所有迁移产物归 lumen:lumen（如该用户存在）。
fix_ownership() {
    if id lumen >/dev/null 2>&1; then
        log_info "修正 ${ROOT} ownership 为 lumen:lumen"
        chown -R lumen:lumen "${ROOT}/releases" "${ROOT}/shared" 2>/dev/null || true
        # current 软链本身的 owner（lchown）
        chown -h lumen:lumen "${ROOT}/current" 2>/dev/null || true
        # .env 单独处理
        if [ -f "${ROOT}/.env" ]; then
            chown lumen:lumen "${ROOT}/.env" 2>/dev/null || true
        fi
    else
        log_warn "未找到 lumen 用户，跳过 chown 修正。请按需手动调整。"
    fi
}

# 写 ${ROOT}/releases/${INITIAL_ID}/.lumen_release.json，让 admin_release 列表与回滚 UI
# 看到 sha / branch / alembic head。失败的字段留空，后端按 None 渲染。
write_initial_release_metadata() {
    local rdir="${ROOT}/releases/${INITIAL_ID}"
    local meta="${rdir}/.lumen_release.json"
    if [ -f "${meta}" ]; then
        log_info ".lumen_release.json 已存在，跳过覆盖"
        return 0
    fi

    local sha="" branch="" head="" created_at
    created_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [ -d "${rdir}/.git" ] && command -v git >/dev/null 2>&1; then
        sha="$(cd "${rdir}" && git rev-parse HEAD 2>/dev/null || true)"
        branch="$(cd "${rdir}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    fi

    # alembic head best-effort：用 release 内的 .venv 跑 `alembic heads`，
    # 失败（venv 不存在 / 命令缺失 / 模块路径不对）就留空。
    if [ -x "${rdir}/.venv/bin/alembic" ] && [ -d "${rdir}/apps/api" ]; then
        head="$(cd "${rdir}/apps/api" \
            && "${rdir}/.venv/bin/alembic" heads 2>/dev/null \
            | awk 'NR==1{print $1}' || true)"
    fi

    log_info "写入 ${meta} (sha=${sha:-<unknown>} branch=${branch:-<unknown>} head=${head:-<unknown>})"
    cat > "${meta}" <<JSON
{
  "id": "${INITIAL_ID}",
  "sha": "${sha}",
  "branch": "${branch}",
  "created_at": "${created_at}",
  "alembic_head_expected": "${head}",
  "alembic_head_applied": "${head}"
}
JSON
}

start_services() {
    if ! command -v systemctl >/dev/null 2>&1; then
        log_warn "未发现 systemctl，跳过启动服务。请手动启动。"
        return 0
    fi
    log_info "启动 lumen 服务（lumen-api, lumen-worker, lumen-web, lumen-tgbot）"
    for u in lumen-api.service lumen-worker.service lumen-web.service lumen-tgbot.service; do
        if systemctl list-unit-files "${u}" --no-legend 2>/dev/null | awk '{print $1}' | grep -Fxq "${u}"; then
            if ! systemctl start "${u}"; then
                log_error "启动 ${u} 失败，请检查 journalctl -u ${u}"
            fi
        fi
    done
}

main() {
    require_root_or_writable
    check_idempotent
    log_info "开始把 ${ROOT} 迁移为 release + symlink 布局"
    stop_services
    move_to_release
    create_current_symlink
    extract_to_shared
    write_initial_release_metadata
    fix_ownership
    deploy_systemd_units
    start_services
    log_info "迁移完成"
    log_info "  ROOT:        ${ROOT}"
    log_info "  current ->   releases/${INITIAL_ID}"
    log_info "  shared:      ${ROOT}/shared"
    log_info "可以通过 scripts/update.sh 触发后续 release 切换。"
}

main "$@"
