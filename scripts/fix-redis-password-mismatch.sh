#!/usr/bin/env bash
# Lumen Redis 密码漂移修复脚本
#
# 用途：当 update.sh 报 "redis ping failed before BGSAVE" 或 backup.sh 报
# "AUTH failed: WRONGPASS" 时使用。根因是 shared/.env 里 REDIS_PASSWORD 那一行
# 跟 REDIS_URL 嵌入的密码漂移；容器实际 requirepass = REDIS_URL 嵌入密码
# （因为 api/worker 用它且工作正常）。本脚本把 REDIS_PASSWORD 对齐 REDIS_URL
# 嵌入密码，不动容器、不动数据。
#
# 一键调用：
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/fix-redis-password-mismatch.sh)"
#
# 干跑（只检查不修改）：
#   sudo DRY_RUN=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/fix-redis-password-mismatch.sh)"
#
# 自动探测 .env 路径：docker compose label / systemd EnvironmentFile / 常见硬编码位置。
#
# 环境变量：
#   LUMEN_SHARED_ENV   显式 .env 路径，覆盖自动探测
#   REDIS_CONTAINER    redis 容器名（默认 lumen-redis）
#   DRY_RUN=1          只验证不写入

set -euo pipefail

REDIS_CONTAINER="${REDIS_CONTAINER:-lumen-redis}"
DRY_RUN="${DRY_RUN:-0}"

log()  { printf '[fix-redis %s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
fail() { printf '[fix-redis ERROR] %s\n' "$*" >&2; exit 1; }

env_file_value() {
    local key="$1" file="$2" line value first last
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$file" | head -n1 || true)"
    [ -n "$line" ] || return 0
    value="${line#*=}"
    # Strip surrounding whitespace and one layer of standard shell quotes.
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [ "${#value}" -ge 2 ]; then
        first="${value:0:1}"
        last="${value: -1}"
        if { [ "$first" = "'" ] && [ "$last" = "'" ]; } || { [ "$first" = '"' ] && [ "$last" = '"' ]; }; then
            value="${value:1:${#value}-2}"
        fi
    fi
    printf '%s' "$value"
}

# 0. 前置检查
command -v docker >/dev/null 2>&1 || fail "docker 不可用"
command -v awk    >/dev/null 2>&1 || fail "awk 不可用"
[ "$(id -u)" -eq 0 ]       || fail "需要 root 权限（请用 sudo 执行）"

# 1. 自动探测 shared/.env 路径
#    优先级（先命中先用）：
#      a. LUMEN_SHARED_ENV / LUMEN_ENV_FILE 环境变量
#      b. LUMEN_DEPLOY_ROOT/shared/.env
#      c. 已运行容器的 com.docker.compose.project.working_dir label（最可靠）
#      d. systemd unit 的 EnvironmentFile=
#      e. 常见硬编码位置（root/opt 部署）
SHARED=""
candidates=()

[ -n "${LUMEN_SHARED_ENV:-}" ] && candidates+=("$LUMEN_SHARED_ENV")
[ -n "${LUMEN_ENV_FILE:-}"   ] && candidates+=("$LUMEN_ENV_FILE")
[ -n "${LUMEN_DEPLOY_ROOT:-}" ] && candidates+=("${LUMEN_DEPLOY_ROOT}/shared/.env")

# 从 docker compose label 抽 project working dir：v2 compose 会给容器设
# com.docker.compose.project.working_dir = docker-compose.yml 所在目录。
for c in lumen-api lumen-worker lumen-redis lumen-web lumen-tgbot; do
    wd="$(docker inspect "$c" --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' 2>/dev/null || true)"
    if [ -n "$wd" ] && [ "$wd" != "<no value>" ]; then
        candidates+=("$wd/shared/.env" "$wd/.env" "$wd/../shared/.env")
    fi
done

# 从 systemd EnvironmentFile= 抽（lumen-api 有就够，多个 unit 都指向同一个 .env）
if command -v systemctl >/dev/null 2>&1; then
    for unit in lumen-api lumen-worker lumen-tgbot; do
        while IFS= read -r ef; do
            ef="${ef#-}"  # 去掉可选前缀 -（EnvironmentFile=-/path）
            [ -n "$ef" ] && candidates+=("$ef")
        done < <(systemctl cat "$unit" 2>/dev/null | sed -nE 's|^EnvironmentFile=(.+)$|\1|p')
    done
fi

candidates+=(
    "/root/Lumen/shared/.env"
    "/opt/lumen/shared/.env"
    "/opt/Lumen/shared/.env"
    "/srv/lumen/shared/.env"
)

for c in "${candidates[@]}"; do
    [ -z "$c" ] && continue
    if [ -f "$c" ]; then
        SHARED="$c"
        break
    fi
done

if [ -z "$SHARED" ]; then
    fail "找不到 shared/.env，尝试过的位置：
$(printf '  - %s\n' "${candidates[@]}")
请显式指定：LUMEN_SHARED_ENV=/path/to/.env sudo -E bash -c \"\$(curl -fsSL ...)\""
fi
log "使用 .env: $SHARED"

# 1. 从 REDIS_URL 解析嵌入密码
REDIS_URL_VALUE="$(env_file_value REDIS_URL "$SHARED")"
if [ "$REDIS_URL_VALUE" = "${REDIS_URL_VALUE#redis://:}" ]; then
    P_URL=""
else
    P_URL="${REDIS_URL_VALUE#redis://:}"
    P_URL="${P_URL%%@*}"
fi
[ -n "$P_URL" ] || fail "无法从 REDIS_URL 解析嵌入密码；期望格式 REDIS_URL=redis://:<pwd>@host:port/db"

# 2. 当前 REDIS_PASSWORD
P_NOW="$(env_file_value REDIS_PASSWORD "$SHARED")"

# 3. 已一致 → noop
if [ "$P_URL" = "$P_NOW" ]; then
    log "REDIS_PASSWORD 已经跟 REDIS_URL 嵌入密码一致，无需修复（exit 0）"
    exit 0
fi
log "检测到漂移：REDIS_URL 嵌入密码 (len=${#P_URL}) ≠ REDIS_PASSWORD (len=${#P_NOW})"

# 4. 用 P_URL 验证可连容器（不通说明根因不只是 .env 漂移）
log "用 REDIS_URL 嵌入密码 ping ${REDIS_CONTAINER} ..."
ping_out="$(REDISCLI_AUTH="$P_URL" docker exec -e REDISCLI_AUTH "$REDIS_CONTAINER" \
    redis-cli --no-auth-warning PING 2>&1)" || ping_out="<docker exec failed>"
if [ "$ping_out" != "PONG" ]; then
    fail "容器 ping 失败：${ping_out}
REDIS_URL 嵌入密码连不上 ${REDIS_CONTAINER}。容器实际 requirepass 跟 .env 两处都不一致 —
不是单纯 .env 漂移。请贴本输出寻求人工支持；或重建容器（注意会清空 redis 内存数据）：
  docker compose up -d --force-recreate redis"
fi
log "ping ok — 容器密码 == REDIS_URL 嵌入密码"

# 5. 干跑模式
if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN=1 — 不修改 ${SHARED}；如需写入请去掉 DRY_RUN 重跑"
    exit 0
fi

# 6. 备份 + awk 精确改写 REDIS_PASSWORD 那一行
ts="$(date -u +%Y%m%d-%H%M%S)"
bak="${SHARED}.bak.${ts}"
cp -a "$SHARED" "$bak"
log "备份：$bak"

tmp="$(mktemp)"
awk -v v="$P_URL" '
    /^[[:space:]]*(export[[:space:]]+)?REDIS_PASSWORD[[:space:]]*=/ {
        prefix = ($0 ~ /^[[:space:]]*export[[:space:]]+/) ? "export " : ""
        print prefix "REDIS_PASSWORD=" v
        next
    }
    { print }
' "$SHARED" > "$tmp"
mv "$tmp" "$SHARED"
chmod 600 "$SHARED"

# 7. 校验
P_VERIFY="$(env_file_value REDIS_PASSWORD "$SHARED")"
[ "$P_VERIFY" = "$P_URL" ] || fail "写入未生效（请回滚：cp ${bak} ${SHARED}）"

log "OK: REDIS_PASSWORD 已对齐 REDIS_URL 嵌入密码"
log ""
log "⚠ 重要：脚本只改 .env 文件，未重启容器。"
log "   api / worker / tgbot 进程仍持有旧的 REDIS_PASSWORD env，必须重启才生效。"
log "   下一步选其一："
log "     1) 触发 admin 一键更新（推荐，含完整 health check）"
log "     2) bash /root/Lumen/current/scripts/lumenctl.sh restart"
log "     3) cd /root/Lumen/current && COMPOSE_PROJECT_NAME=lumen docker compose up -d --force-recreate api worker tgbot"
log "如需回滚：sudo cp ${bak} ${SHARED}"
