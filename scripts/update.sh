#!/usr/bin/env bash
# Lumen 更新脚本
# 用法：  bash scripts/update.sh
# 行为：git pull（可选）-> 起容器 -> uv sync -> alembic upgrade
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

# ---------------------------------------------------------------------------
# 1. 依赖快查（更新阶段假设 install 已经做过完整检查，这里只确认必备工具仍在）
# ---------------------------------------------------------------------------
ensure_cmd docker "请安装 Docker 后重试"
if ! docker compose version >/dev/null 2>&1; then
    log_error "未检测到 docker compose v2。请升级 Docker。"
    exit 1
fi
if ! docker info >/dev/null 2>&1; then
    log_error "Docker daemon 未运行。请先启动 Docker Desktop 或 'sudo systemctl start docker' 后重试。"
    exit 1
fi
ensure_cmd uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
ensure_cmd npm "请安装 Node.js >= 20"

# ---------------------------------------------------------------------------
# 2. git pull（可选）
# ---------------------------------------------------------------------------
log_step "检查代码仓库状态"
if [ -d "${ROOT}/.git" ] && command -v git >/dev/null 2>&1; then
    CURRENT_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
    log_info "当前分支：${BRANCH}    commit：${CURRENT_COMMIT}"

    if confirm "是否执行 git pull 拉取最新代码？"; then
        log_info "执行 git pull（rebase=merges + autostash）..."
        # 使用 --rebase=merges 保留 merge 拓扑；--autostash 自动 stash/pop 本地未提交改动，
        # 比 --ff-only 友好：只要无冲突就能继续，否则给出清晰提示让用户处理。
        if ! git pull --rebase=merges --autostash; then
            log_warn "git pull 未成功（可能存在合并冲突或本地未提交改动无法自动 stash）。"
            log_warn "建议：先 'git status' 查看；'git stash push -u' 暂存后重跑本脚本，或手动 commit 再来。"
            if ! confirm "是否继续后续步骤（仅同步依赖与迁移）？"; then
                log_info "用户中止。"
                exit 0
            fi
        else
            NEW_COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
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
if ! docker compose up -d --wait; then
    log_error "容器启动或健康检查失败。请运行 'docker compose logs' 排查。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 4. uv sync 拉新依赖
# ---------------------------------------------------------------------------
log_step "同步 Python 依赖（uv sync --frozen）"
if ! uv sync --frozen; then
    log_error "uv sync --frozen 失败。如果是因为 lock 已过期，请改跑 'uv sync' 重新解析依赖。"
    exit 1
fi

# ---------------------------------------------------------------------------
# 5. alembic upgrade head
# ---------------------------------------------------------------------------
log_step "应用新的数据库迁移（alembic upgrade head）"
(
    cd "${ROOT}/apps/api"
    if ! uv run alembic upgrade head; then
        log_error "数据库迁移失败。请检查容器与 DATABASE_URL，可用 'docker compose logs postgres' 排查。"
        exit 1
    fi
)

# ---------------------------------------------------------------------------
# 6. npm ci
# ---------------------------------------------------------------------------
log_step "同步前端依赖（npm ci）"
(
    cd "${ROOT}/apps/web"
    npm ci
)

# ---------------------------------------------------------------------------
# 7. 可选 rebuild
# ---------------------------------------------------------------------------
if confirm "是否重新构建前端生产包（npm run build）？"; then
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
        npm run build
    )
fi

# ---------------------------------------------------------------------------
# 8. 收尾
# ---------------------------------------------------------------------------
log_step "更新完成"
cat <<EOF

  Update complete. 请重启 api / worker / web 三个进程：

    1) API:    cd ${ROOT}/apps/api && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
    2) Worker: cd ${ROOT}/apps/worker && uv run python -m arq app.main.WorkerSettings
    3) Web:    cd ${ROOT}/apps/web && npm run dev    （或 npm run start 如已 build）

EOF

trap - ERR
exit 0
