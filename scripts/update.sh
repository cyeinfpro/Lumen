#!/usr/bin/env bash
# Lumen 更新脚本
# 用法：  bash scripts/update.sh
# 行为：git pull（可选）-> 起容器 -> uv sync --all-packages -> alembic upgrade
#       -> npm ci -> 可选 rebuild。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

trap 'log_error "更新失败：第 ${LINENO} 行返回非零状态。修复后重跑本脚本即可。"' ERR

ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
lumen_install_signal_handlers
lumen_acquire_lock "${ROOT}" "update.sh"
cd "${ROOT}"
log_info "项目根目录：${ROOT}"

LUMEN_UPDATE_SYSTEMD_RUNTIME=0
LUMEN_UPDATE_RUN_USER="$(id -un 2>/dev/null || echo "${USER:-root}")"
LUMEN_UPDATE_RUN_GROUP="$(id -gn 2>/dev/null || echo "${LUMEN_UPDATE_RUN_USER}")"

if lumen_systemd_has_any_units lumen-api.service lumen-worker.service lumen-web.service; then
    LUMEN_UPDATE_SYSTEMD_RUNTIME=1
    LUMEN_UPDATE_RUN_USER="$(lumen_runtime_service_user)"
    LUMEN_UPDATE_RUN_GROUP="$(lumen_runtime_service_group "${LUMEN_UPDATE_RUN_USER}")"
    log_info "检测到 systemd 部署，依赖/迁移/构建将以运行用户执行：${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}"
fi

lumen_update_as_runtime_user() {
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
        lumen_run_as_user "${LUMEN_UPDATE_RUN_USER}" "$@"
    else
        "$@"
    fi
}

lumen_update_prepare_project_permissions() {
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" != "1" ]; then
        return 0
    fi
    if [ "$(id -un 2>/dev/null || true)" = "${LUMEN_UPDATE_RUN_USER}" ]; then
        return 0
    fi
    log_info "确保项目目录归运行用户所有：${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}"
    if ! lumen_run_as_root chown -R "${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP}" "${ROOT}"; then
        log_error "无法修正 ${ROOT} 所有权。请用 root/sudo 执行 chown 后重跑："
        log_error "  chown -R ${LUMEN_UPDATE_RUN_USER}:${LUMEN_UPDATE_RUN_GROUP} ${ROOT}"
        return 1
    fi
}

lumen_update_runtime_command_path() {
    local cmd="$1"
    local path=""
    if [ "${LUMEN_UPDATE_SYSTEMD_RUNTIME}" = "1" ]; then
        path="$(lumen_run_as_user "${LUMEN_UPDATE_RUN_USER}" sh -lc "command -v ${cmd}" 2>/dev/null || true)"
    else
        path="$(command -v "${cmd}" 2>/dev/null || true)"
    fi
    [ -n "${path}" ] || return 1
    printf '%s' "${path}"
}

lumen_update_require_runtime_cmd() {
    local cmd="$1"
    local hint="$2"
    local path=""
    if ! path="$(lumen_update_runtime_command_path "${cmd}")"; then
        log_error "缺少 ${cmd}，或运行用户 ${LUMEN_UPDATE_RUN_USER} 无法访问。"
        log_error "${hint}"
        return 1
    fi
    printf '%s' "${path}"
}

lumen_update_decision() {
    local env_name="$1"
    local prompt="$2"
    local raw="${!env_name:-}"
    case "${raw}" in
        1|true|TRUE|yes|YES|y|Y|on|ON)
            log_info "${env_name}=1，自动确认：${prompt}"
            return 0
            ;;
        0|false|FALSE|no|NO|n|N|off|OFF)
            log_info "${env_name}=0，自动跳过：${prompt}"
            return 1
            ;;
    esac
    if [ "${LUMEN_UPDATE_NONINTERACTIVE:-0}" = "1" ]; then
        log_info "非交互更新未设置 ${env_name}，默认跳过：${prompt}"
        return 1
    fi
    confirm "${prompt}"
}

# ---------------------------------------------------------------------------
# 1. 依赖快查（更新阶段假设 install 已经做过完整检查，这里只确认必备工具仍在）
# ---------------------------------------------------------------------------
lumen_require_docker_access
lumen_update_prepare_project_permissions
UV_BIN="$(lumen_update_require_runtime_cmd uv "curl -LsSf https://astral.sh/uv/install.sh | sh")"
NPM_BIN="$(lumen_update_require_runtime_cmd npm "请安装 Node.js >= 20")"
GIT_BIN="$(lumen_update_runtime_command_path git || true)"

# ---------------------------------------------------------------------------
# 2. git pull（可选）
# ---------------------------------------------------------------------------
log_step "检查代码仓库状态"
if [ -d "${ROOT}/.git" ] && [ -n "${GIT_BIN}" ]; then
    CURRENT_COMMIT="$(lumen_update_as_runtime_user "${GIT_BIN}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    BRANCH="$(lumen_update_as_runtime_user "${GIT_BIN}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    log_info "当前分支：${BRANCH}    commit：${CURRENT_COMMIT}"

    if lumen_update_decision LUMEN_UPDATE_GIT_PULL "是否执行 git pull 拉取最新代码？"; then
        log_info "执行 git pull（rebase=merges + autostash）..."
        # 使用 --rebase=merges 保留 merge 拓扑；--autostash 自动 stash/pop 本地未提交改动，
        # 比 --ff-only 友好：只要无冲突就能继续，否则给出清晰提示让用户处理。
        if ! lumen_update_as_runtime_user "${GIT_BIN}" pull --rebase=merges --autostash; then
            log_warn "git pull 未成功（可能存在合并冲突或本地未提交改动无法自动 stash）。"
            log_warn "建议：先 'git status' 查看；'git stash push -u' 暂存后重跑本脚本，或手动 commit 再来。"
            if ! confirm "是否继续后续步骤（仅同步依赖与迁移）？"; then
                log_info "用户中止。"
                exit 0
            fi
        else
            NEW_COMMIT="$(lumen_update_as_runtime_user "${GIT_BIN}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
            log_info "已更新到 commit ${NEW_COMMIT}"
        fi
    else
        log_info "跳过 git pull。"
    fi
else
    log_info "非 git 仓库（或无 git 命令），跳过 pull 步骤。"
fi

# ---------------------------------------------------------------------------
# 3. 确保容器在跑
# ---------------------------------------------------------------------------
log_step "确保 PostgreSQL / Redis 容器在运行并就绪（docker compose up -d --wait）"
if ! lumen_docker compose up -d --wait; then
    log_error "容器启动或健康检查失败。请运行 '$(lumen_docker_command_label) compose logs' 排查。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 4. uv sync 拉新依赖
# ---------------------------------------------------------------------------
log_step "同步 Python 依赖（uv sync --frozen --all-packages）"
if ! lumen_update_as_runtime_user "${UV_BIN}" sync --frozen --all-packages; then
    log_error "uv sync --frozen --all-packages 失败。如果是因为 lock 已过期，请改跑 'uv sync --all-packages' 重新解析依赖。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 5. alembic upgrade head
# ---------------------------------------------------------------------------
log_step "应用新的数据库迁移（alembic upgrade head）"
(
    cd "${ROOT}/apps/api"
    if ! lumen_update_as_runtime_user "${UV_BIN}" run alembic upgrade head; then
        log_error "数据库迁移失败。请检查容器与 DATABASE_URL，可用 '$(lumen_docker_command_label) compose logs postgres' 排查。"
        exit 1
    fi
)

# ---------------------------------------------------------------------------
# 6. npm ci
# ---------------------------------------------------------------------------
log_step "同步前端依赖（npm ci）"
(
    cd "${ROOT}/apps/web"
    lumen_update_as_runtime_user "${NPM_BIN}" ci
)

# ---------------------------------------------------------------------------
# 7. 可选 rebuild
# ---------------------------------------------------------------------------
BUILD_DONE=0
if lumen_update_decision LUMEN_UPDATE_BUILD "是否重新构建前端生产包（npm run build）？"; then
    log_step "重建前端（npm run build）"
    WEB_ENV="${ROOT}/apps/web/.env.local"
    NEXT_PUBLIC_API_BASE_VALUE=""
    if [ -f "${WEB_ENV}" ] && grep -qE "^NEXT_PUBLIC_API_BASE=.+" "${WEB_ENV}"; then
        NEXT_PUBLIC_API_BASE_VALUE="$(sed -n 's/^NEXT_PUBLIC_API_BASE=//p' "${WEB_ENV}" | head -n1)"
    fi
    (
        cd "${ROOT}/apps/web"
        # 默认不导出 NEXT_PUBLIC_API_BASE：浏览器使用同源 /api，由 Next.js rewrite 到后端。
        # 只有跨域部署显式配置了 NEXT_PUBLIC_API_BASE 时才注入前端 bundle。
        if [ -n "${NEXT_PUBLIC_API_BASE_VALUE}" ]; then
            export NEXT_PUBLIC_API_BASE="${NEXT_PUBLIC_API_BASE_VALUE}"
        else
            unset NEXT_PUBLIC_API_BASE
        fi
        if [ -n "${NEXT_PUBLIC_API_BASE_VALUE}" ]; then
            lumen_update_as_runtime_user env NEXT_PUBLIC_API_BASE="${NEXT_PUBLIC_API_BASE_VALUE}" "${NPM_BIN}" run build
        else
            lumen_update_as_runtime_user "${NPM_BIN}" run build
        fi
    )
    BUILD_DONE=1
fi

# ---------------------------------------------------------------------------
# 8. 重启服务并执行健康检查
# ---------------------------------------------------------------------------
log_step "更新后运行时检查"
lumen_ensure_runtime_dirs "${ROOT}/.env"

if [ "${BUILD_DONE}" -eq 1 ]; then
    WEB_NPM_SCRIPT="start"
else
    WEB_NPM_SCRIPT="dev"
fi

RUNTIME_STARTED=0
if lumen_systemd_has_any_units lumen-api.service lumen-worker.service lumen-web.service; then
    lumen_restart_systemd_units lumen-api.service lumen-worker.service lumen-web.service
    if lumen_systemd_has_unit lumen-tgbot.service; then
        lumen_restart_systemd_units lumen-tgbot.service
    fi
    lumen_check_runtime_health
else
    if lumen_process_listening_on_port 8000 || lumen_process_listening_on_port 3000; then
        log_error "未发现 Lumen systemd unit，但 8000/3000 端口已有进程监听，无法安全重启并确认新版本。"
        log_error "请停止旧 API/Web 进程后重跑，或按 deploy/systemd 模板安装 lumen-api/lumen-web/lumen-worker 服务。"
        exit 1
    fi
    log_warn "未发现 Lumen systemd unit，将在当前终端后台启动 API / Worker / Web 并执行健康检查。"
    lumen_start_local_runtime "${ROOT}" "${WEB_NPM_SCRIPT}"
    RUNTIME_STARTED=1
fi

# ---------------------------------------------------------------------------
# 9. 收尾
# ---------------------------------------------------------------------------
log_step "更新完成"
if [ "${RUNTIME_STARTED}" -eq 1 ]; then
    cat <<EOF

  Update complete. 已启动 API / Worker / Web，并通过健康检查。
  运行日志目录：${LUMEN_LOCAL_RUNTIME_LOG_DIR}

EOF
else
    cat <<EOF

  Update complete. 已重启 systemd 服务并通过健康检查：
    API:    ${LUMEN_API_HEALTH_URL:-http://127.0.0.1:8000/healthz}
    Web:    ${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/}
    Worker: lumen-worker.service active

EOF
fi

trap - ERR
exit 0
