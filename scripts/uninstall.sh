#!/usr/bin/env bash
# Lumen 卸载脚本
# 用法：  bash scripts/uninstall.sh
# 行为：分步交互，仅停止容器（默认）；用户明确同意才删数据 / 配置 / 缓存。
# 源代码本身不会被删除，可随时重新安装。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "${SCRIPT_DIR}/lib.sh"

trap 'log_error "卸载脚本失败：第 ${LINENO} 行返回非零状态。手动检查后再重试。"' ERR

ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
lumen_install_signal_handlers
lumen_acquire_lock "${ROOT}" "uninstall.sh"
cd "${ROOT}"
log_info "项目根目录：${ROOT}"

# ---------------------------------------------------------------------------
# 1. 二次确认
# ---------------------------------------------------------------------------
log_step "Lumen 卸载向导"
cat <<EOF

  本向导将分步进行：
    1) 停止 install.sh 拉起的 API / Worker / Web 后台进程（释放 8000/3000）
    2) 停止 PostgreSQL / Redis 容器（释放 5432/6379）
    3) 询问是否删除数据卷（不可恢复）
    4) 询问是否删除 .env 配置
    5) 询问是否删除本地图片存储
    6) 询问是否删除 /opt/lumendata/backup 备份
    7) 询问是否删除前端 node_modules / .next
    8) 询问是否删除 .venv（uv 虚拟环境）

  源代码与 docker-compose.yml 不会被删除；删除项目目录请手动 rm。
  每个删除步骤默认 "N"（保留），需明确输入 y/yes 才会执行。

EOF

if ! confirm "确认开始卸载？"; then
    log_info "用户取消，未做任何修改。"
    exit 0
fi

# 跟踪做了什么/没做什么，最后汇总
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
# 2. 停 install.sh 拉起的本地运行时（uvicorn / arq / next-server）
#    install.sh 把 API/Worker/Web 后台 & 起来，PID 仅在 install.sh 进程内；
#    如果不在这里清掉，下次 install 检测到 8000/3000 已被占用就会卡死。
# ---------------------------------------------------------------------------
log_step "停止 Lumen 本地运行时（API / Worker / Web）"
RUNTIME_FREED=0
RUNTIME_BUSY=0
lumen_stop_persisted_runtime "${ROOT}" 15 || true
for PORT in 8000 3000; do
    if ! lumen_process_listening_on_port "${PORT}"; then
        continue
    fi
    if lumen_release_port_if_lumen "${PORT}" "端口 ${PORT}"; then
        RUNTIME_FREED=1
    else
        log_warn "端口 ${PORT} 仍被外部进程占用，跳过强杀。请在确认无误后手动停掉。"
        RUNTIME_BUSY=1
    fi
done
if [ "${RUNTIME_FREED}" -eq 1 ]; then
    DONE+=("已停止本地运行时进程并释放 8000/3000")
elif [ "${RUNTIME_BUSY}" -eq 1 ]; then
    KEPT+=("8000/3000 仍被非 Lumen 进程占用（请自行确认）")
else
    KEPT+=("本地运行时本来就没在跑")
fi

# ---------------------------------------------------------------------------
# 3. docker compose down（停容器）；失败兜底用 docker rm -f 强删 lumen-pg/lumen-redis。
# ---------------------------------------------------------------------------
log_step "停止容器（docker compose down）"
if [ "${DOCKER_AVAILABLE}" -eq 1 ]; then
    if lumen_docker compose down; then
        DONE+=("已停止 lumen-pg / lumen-redis 容器")
    else
        log_warn "docker compose down 返回非零，尝试强删残留容器以释放 5432/6379。"
        STRAY=0
        for CNAME in lumen-pg lumen-redis; do
            if lumen_docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${CNAME}"; then
                if lumen_docker rm -f "${CNAME}" >/dev/null 2>&1; then
                    log_info "已强删容器 ${CNAME}"
                    STRAY=1
                else
                    log_error "无法删除 ${CNAME}，请手动 'docker rm -f ${CNAME}'。"
                fi
            fi
        done
        if [ "${STRAY}" -eq 1 ]; then
            DONE+=("已强删残留 lumen-pg / lumen-redis 容器")
        else
            KEPT+=("容器状态未确认（docker compose down 失败）")
        fi
    fi
else
    log_warn "未检测到 docker / docker compose v2，跳过停容器步骤。"
    KEPT+=("容器状态未变（无 docker 命令）")
fi

# ---------------------------------------------------------------------------
# 3. 删数据卷
# ---------------------------------------------------------------------------
log_step "数据卷"
log_warn "数据卷包含所有用户、对话、生成图记录。删除后无法恢复。"
if confirm "删除 PG / Redis 数据卷（docker compose down -v）？"; then
    if [ "${DOCKER_AVAILABLE}" -ne 1 ]; then
        log_error "未检测到可用的 docker / docker compose v2，无法删除数据卷。"
        KEPT+=("数据卷未删除（无可用 docker 命令）")
    elif lumen_docker compose down -v; then
        # compose 删卷时若 volume 仍被其它容器挂载会沉默跳过，主动 ls 一次校验。
        if lumen_docker volume ls --format '{{.Name}}' 2>/dev/null | grep -qE '(^|_)lumen_(pg|redis)_data$'; then
            log_warn "卷未完全删除（可能仍被其它容器挂载，或 compose project 名不一致）。"
            log_warn "请运行 '$(lumen_docker_command_label) volume ls | grep lumen' 排查，必要时手动 '$(lumen_docker_command_label) volume rm <name>'."
            DONE+=("尝试删除数据卷（部分卷未清，详见上方提示）")
        else
            DONE+=("已删除数据卷 lumen_pg_data / lumen_redis_data")
        fi
    else
        log_error "docker compose down -v 失败。请手动检查。"
    fi
else
    KEPT+=("数据卷保留（可下次 install 直接复用）")
fi

# ---------------------------------------------------------------------------
# 4. 删 .env
# ---------------------------------------------------------------------------
log_step ".env 配置文件"
ENV_TARGETS=(
    "${ROOT}/.env"
    "${ROOT}/apps/api/.env"
    "${ROOT}/apps/worker/.env"
    "${ROOT}/apps/web/.env.local"
)
EXISTING_ENV=()
for f in "${ENV_TARGETS[@]}"; do
    [ -f "${f}" ] && EXISTING_ENV+=("${f}")
done

if [ "${#EXISTING_ENV[@]}" -eq 0 ]; then
    log_info "未发现 .env 文件。"
    KEPT+=(".env 文件本来就不存在")
else
    log_warn "将影响以下文件："
    for f in "${EXISTING_ENV[@]}"; do
        printf '         %s\n' "${f}"
    done
    if confirm "删除上述 .env 配置文件？"; then
        for f in "${EXISTING_ENV[@]}"; do
            rm -f "${f}"
            log_info "已删除 ${f}"
        done
        DONE+=("已删除 ${#EXISTING_ENV[@]} 个 .env 配置文件")
    else
        KEPT+=(".env 配置文件保留")
    fi
fi

# ---------------------------------------------------------------------------
# 5. 本地图片存储
# ---------------------------------------------------------------------------
log_step "本地图片存储"
STORAGE_TARGETS=(
    "/opt/lumendata/storage"
    "${ROOT}/var/storage"
    "${ROOT}/apps/api/var/storage"
)
EXISTING_STORAGE=()
for d in "${STORAGE_TARGETS[@]}"; do
    [ -d "${d}" ] && EXISTING_STORAGE+=("${d}")
done

if [ "${#EXISTING_STORAGE[@]}" -eq 0 ]; then
    log_info "未发现 var/storage 目录。"
    KEPT+=("var/storage 本来就不存在")
else
    log_warn "将影响以下目录（含所有生成图原文件）："
    for d in "${EXISTING_STORAGE[@]}"; do
        printf '         %s\n' "${d}"
    done
    if confirm "删除上述存储目录（不可恢复）？"; then
        for d in "${EXISTING_STORAGE[@]}"; do
            rm -rf "${d}"
            log_info "已删除 ${d}"
        done
        DONE+=("已删除 ${#EXISTING_STORAGE[@]} 个本地图片存储目录")
    else
        KEPT+=("本地存储目录保留")
    fi
fi

# ---------------------------------------------------------------------------
# 6. 本地备份
# ---------------------------------------------------------------------------
log_step "本地备份 /opt/lumendata/backup"
BACKUP_TARGETS=(
    "/opt/lumendata/backup"
)
EXISTING_BACKUP=()
for d in "${BACKUP_TARGETS[@]}"; do
    [ -d "${d}" ] && EXISTING_BACKUP+=("${d}")
done

if [ "${#EXISTING_BACKUP[@]}" -eq 0 ]; then
    log_info "未发现本地备份目录。"
    KEPT+=("本地备份本来就不存在")
else
    log_warn "备份目录包含 PostgreSQL / Redis 备份，删除后无法用于恢复。"
    log_warn "将影响以下目录："
    for d in "${EXISTING_BACKUP[@]}"; do
        printf '         %s\n' "${d}"
    done
    if confirm "删除上述备份目录（不可恢复）？"; then
        for d in "${EXISTING_BACKUP[@]}"; do
            rm -rf "${d}"
            log_info "已删除 ${d}"
        done
        DONE+=("已删除 ${#EXISTING_BACKUP[@]} 个本地备份目录")
    else
        KEPT+=("本地备份目录保留")
    fi
fi

# ---------------------------------------------------------------------------
# 7. 前端 node_modules / .next
# ---------------------------------------------------------------------------
log_step "前端缓存（node_modules / .next）"
WEB_NODE="${ROOT}/apps/web/node_modules"
WEB_NEXT="${ROOT}/apps/web/.next"
WEB_TARGETS=()
[ -d "${WEB_NODE}" ] && WEB_TARGETS+=("${WEB_NODE}")
[ -d "${WEB_NEXT}" ] && WEB_TARGETS+=("${WEB_NEXT}")

if [ "${#WEB_TARGETS[@]}" -eq 0 ]; then
    log_info "未发现前端缓存目录。"
    KEPT+=("前端缓存本来就不存在")
else
    log_warn "将删除："
    for d in "${WEB_TARGETS[@]}"; do
        printf '         %s\n' "${d}"
    done
    if confirm "删除前端 node_modules 与 .next？"; then
        for d in "${WEB_TARGETS[@]}"; do
            rm -rf "${d}"
            log_info "已删除 ${d}"
        done
        DONE+=("已清理前端 node_modules / .next")
    else
        KEPT+=("前端缓存保留")
    fi
fi

# ---------------------------------------------------------------------------
# 8. .venv（uv 虚拟环境）
# ---------------------------------------------------------------------------
log_step "Python 虚拟环境 .venv"
VENV="${ROOT}/.venv"
if [ -d "${VENV}" ]; then
    log_warn "将删除 ${VENV}"
    if confirm "删除 .venv？"; then
        rm -rf "${VENV}"
        log_info "已删除 ${VENV}"
        DONE+=("已删除 .venv")
    else
        KEPT+=(".venv 保留")
    fi
else
    log_info "未发现 .venv。"
    KEPT+=(".venv 本来就不存在")
fi

# ---------------------------------------------------------------------------
# 9. 总结
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
  重新安装：bash scripts/install.sh

EOF

trap - ERR
exit 0
