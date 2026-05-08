#!/usr/bin/env bash
# Lumen one-shot repair: migrate stale-project lumen-* containers to project=lumen.
#
# 当 docker 视角里运行的 lumen-* 容器跑在某个非 "lumen" 的 compose project 时
# (例如曾手工 `cd /opt/lumen/current && docker compose up` 起过，project 取 cwd
# basename = "current")，update.sh 在新 release 目录跑 docker compose up 用的
# 是 project=lumen，会撞容器名（lumen-redis 已被 stale project 占用），
# --force-recreate 跨 project 不生效。本脚本 detect 并 down 掉 stale project，
# 再用 project=lumen 重新 up，让 update.sh / install.sh 后续路径恢复正常。
#
# 用法（root）：
#   bash <(curl -sSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/repair-compose-project.sh)
#
# 或者本地：
#   sudo bash /opt/lumen/current/scripts/repair-compose-project.sh
#
# 数据安全：volumes 是 bind mount (/opt/lumendata/postgres, redis, storage)，
# docker compose down 不会删 bind volume，仅删容器和 docker network。

set -Eeuo pipefail

TARGET_PROJECT="${LUMEN_COMPOSE_PROJECT:-lumen}"
DEPLOY_ROOT="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
COMPOSE_DIR="${DEPLOY_ROOT}/current"
COMPOSE_FILE="${COMPOSE_DIR}/docker-compose.yml"

c_red()   { printf '\033[31m%s\033[0m' "$*"; }
c_green() { printf '\033[32m%s\033[0m' "$*"; }
c_cyan()  { printf '\033[36m%s\033[0m' "$*"; }
log_info() { printf '%s %s\n' "$(c_cyan '[INFO]')" "$*"; }
log_warn() { printf '%s %s\n' "$(c_red '[WARN]')" "$*"; }
log_err()  { printf '%s %s\n' "$(c_red '[ERROR]')" "$*" >&2; }

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || { log_err "missing command: $1"; exit 2; }
}

need_cmd docker
docker compose version >/dev/null 2>&1 || { log_err "docker compose v2 required"; exit 2; }

if [ ! -f "${COMPOSE_FILE}" ]; then
    log_err "compose file not found: ${COMPOSE_FILE}"
    log_err "请确认 LUMEN_DEPLOY_ROOT (default /opt/lumen) 正确，且 current symlink 已建。"
    exit 2
fi

# 列出所有 name 形如 lumen-* 容器的 compose project label，去重
# .Labels 是 string slice 不能 index；用单数 {{.Label "key"}} 取单个 label。
mapfile -t all_projects < <(docker ps -a \
    --filter 'name=^lumen-' \
    --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
    | sort -u | grep -v '^$' || true)

stale=()
for p in "${all_projects[@]+"${all_projects[@]}"}"; do
    if [ "${p}" != "${TARGET_PROJECT}" ]; then
        stale+=("${p}")
    fi
done

if [ "${#stale[@]}" -eq 0 ]; then
    log_info "$(c_green 'no stale lumen-* containers detected'); 无需修复。"
    log_info "当前 stack (project=${TARGET_PROJECT}):"
    ( cd "${COMPOSE_DIR}" && COMPOSE_PROJECT_NAME="${TARGET_PROJECT}" docker compose ps ) || true
    exit 0
fi

log_info "检测到 lumen-* 容器跑在非 ${TARGET_PROJECT} 的 project："
for p in "${stale[@]}"; do
    log_info "  - project=$(c_red "${p}")"
done
log_info "volumes 是 bind mount (${DEPLOY_ROOT%/*}data/* 之类)，不会被 docker compose down 删。"

if [ -t 0 ] && [ "${LUMEN_REPAIR_NONINTERACTIVE:-0}" != "1" ]; then
    printf '继续？这会有 ~30 秒服务中断 [y/N] '
    IFS= read -r ans
    case "${ans}" in
        y|Y) ;;
        *) log_warn "已取消。"; exit 0 ;;
    esac
else
    log_info "non-interactive 模式 (LUMEN_REPAIR_NONINTERACTIVE=1 或 stdin 非 tty)，自动继续。"
fi

for p in "${stale[@]}"; do
    log_info "[step] docker compose -p '${p}' down --remove-orphans"
    if ! docker compose -p "${p}" down --remove-orphans 2>&1 | tail -10; then
        log_warn "down -p '${p}' 失败；将依赖后续 up --force-recreate 覆盖。"
    fi
done

log_info "[step] 在 ${COMPOSE_DIR} 用 project=${TARGET_PROJECT} 重新 up"
cd "${COMPOSE_DIR}"
if ! COMPOSE_PROJECT_NAME="${TARGET_PROJECT}" docker compose up -d --wait; then
    log_err "docker compose up 失败；请 docker compose ps / docker logs 检查。"
    exit 1
fi

log_info "$(c_green '[done] project unified')"
log_info "stack ps:"
COMPOSE_PROJECT_NAME="${TARGET_PROJECT}" docker compose ps
log_info "下一步：从 admin 触发"一键更新"应可以正常完成。"
